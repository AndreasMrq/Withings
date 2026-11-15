"""Derived health metrics computed from the raw Withings sensor data.

Everything here is computed locally and transparently from heart rate, HRV (RMSSD), sleep
stages, steps and breathing data. The algorithms are inspired by what Garmin/Firstbeat and
the sports-science literature use, but they are *estimates*, not medical values:

* Resting HR      - lowest 30-minute rolling average HR of the day (incl. the night before).
* HRV status      - nightly mean RMSSD, 7-day average vs. your personal 28-day baseline range.
* Sleep score     - duration vs. target, deep/REM share, efficiency and restlessness.
* Stress          - awake, non-active heart rate elevation above resting HR (0-100).
* Body Energy     - a battery (5-100) charged by sleep (scaled by HRV recovery) and rest,
                    drained by stress and physical activity.
* Training load   - Banister TRIMP per day; acute (7d) / chronic (28d) EWMA and their ratio.
* Recovery time   - hours until fully recovered from recent load, sped up by good sleep.
* Readiness       - weighted mix of sleep, HRV, resting HR, recovery and load balance.
* VO2max          - from Withings (Cardio Fitness) if present, else the Uth et al. estimate.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import statistics
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import config, db

STEP = 300  # body-energy simulation resolution (seconds)
STAGE_NAMES = {0: "awake", 1: "light", 2: "deep", 3: "rem"}

DEFAULT_PROFILE = {
    "birth_year": 1990,
    "sex": "m",
    "max_hr": None,
    "sleep_target_h": 8.0,
    "tz": None,
    "step_goal": 10000,
}


def profile() -> dict:
    p = dict(DEFAULT_PROFILE)
    p.update(db.kv_get("profile", {}) or {})
    return p


def tz() -> ZoneInfo:
    name = profile().get("tz") or config.DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r1(x, nd=1):
    return None if x is None else round(x, nd)


# --------------------------------------------------------------------------------------
# Scoring functions (pure, unit-tested)
# --------------------------------------------------------------------------------------

def sleep_score(tst_s, deep_s, rem_s, eff, wakeups, waso_s, target_h=8.0):
    """0-100 sleep quality score. Returns (score, components dict)."""
    if not tst_s:
        return None, {}
    h = tst_s / 3600
    full = target_h - 0.25
    dur = 40 if h >= full else 40 * clamp((h - 4) / (full - 4), 0, 1) ** 1.3
    if h > target_h + 1.5:
        dur -= min(8, (h - target_h - 1.5) * 4)
    deep_pct = (deep_s or 0) / tst_s
    rem_pct = (rem_s or 0) / tst_s
    deep = 15 * clamp((deep_pct - 0.06) / (0.20 - 0.06), 0, 1)
    rem = 15 * clamp((rem_pct - 0.10) / (0.23 - 0.10), 0, 1)
    effp = 15 * clamp(((eff if eff is not None else 0.88) - 0.75) / (0.95 - 0.75), 0, 1)
    wk = wakeups or 0
    rest = 15 - 3 * max(0, wk - 1) - min(8, (waso_s or 0) / 300)
    rest = clamp(rest, 0, 15)
    comps = {"duration": dur, "deep": deep, "rem": rem, "efficiency": effp, "restfulness": rest}
    return round(clamp(sum(comps.values()), 0, 100)), {k: round(v, 1) for k, v in comps.items()}


def stress_value(hr, rhr, hrmax):
    """Stress 0-100 from HR elevation above resting while not physically active."""
    span = max(20.0, 0.45 * (hrmax - rhr))
    return int(round(clamp(100 * (hr - rhr) / span, 0, 100)))


def stress_bucket(s):
    return "rest" if s <= 25 else "low" if s <= 50 else "medium" if s <= 75 else "high"


def trimp(samples, rhr, hrmax, sex="m"):
    """Banister TRIMP from [(hr, minutes)] - only counts HR above 50 % of max."""
    k, b = (0.64, 1.92) if sex != "f" else (0.86, 1.67)
    total = 0.0
    for hr, minutes in samples:
        if hr < 0.5 * hrmax:
            continue
        x = clamp((hr - rhr) / max(1, hrmax - rhr), 0, 1.1)
        total += minutes * x * k * math.exp(b * x)
    return total


def hr_zone(hr, hrmax):
    """Zone 0-5 by % of max HR (Garmin default: 50/60/70/80/90 %)."""
    p = hr / hrmax
    return 0 if p < 0.5 else 1 if p < 0.6 else 2 if p < 0.7 else 3 if p < 0.8 else 4 if p < 0.9 else 5


def readiness_score(sleep_sc, hrv_ratio, rhr_delta, recovery_h, acwr, sleep_hist=None):
    comps = {}
    if sleep_sc is not None:
        comps["sleep"] = (0.25, sleep_sc)
    if sleep_hist is not None:
        comps["sleep_history"] = (0.10, sleep_hist)
    if hrv_ratio is not None:
        comps["hrv"] = (0.25, clamp(85 + (hrv_ratio - 1) * 250, 0, 100))
    if rhr_delta is not None:
        comps["resting_hr"] = (0.10, clamp(85 - rhr_delta * 10, 0, 100))
    if recovery_h is not None:
        comps["recovery"] = (0.15, clamp(100 - recovery_h * 1.67, 0, 100))
    if acwr is not None:
        comps["load_balance"] = (0.15, clamp(100 - max(0, acwr - 1.0) * 200, 0, 100))
    if not comps or "sleep" not in comps and "hrv" not in comps:
        return None, {}
    w = sum(c[0] for c in comps.values())
    score = sum(c[0] * c[1] for c in comps.values()) / w
    return round(score), {k: round(v[1]) for k, v in comps.items()}


def readiness_label(s):
    if s is None:
        return None
    return "Prime" if s >= 95 else "High" if s >= 75 else "Moderate" if s >= 50 else "Low" if s >= 25 else "Poor"


def hrv_status(week_avg, base_vals):
    """Garmin-like HRV status from 7-day avg vs. the personal baseline range."""
    if week_avg is None or len(base_vals) < 14:
        return "Building baseline", None, None
    m = statistics.mean(base_vals)
    sd = statistics.pstdev(base_vals) or 1.0
    lo, hi = m - 0.8 * sd, m + 0.8 * sd
    if week_avg < m - 2 * sd:
        st = "Low"
    elif week_avg < lo:
        st = "Unbalanced (low)"
    elif week_avg > hi:
        st = "Unbalanced (high)"
    else:
        st = "Balanced"
    return st, round(lo, 1), round(hi, 1)


def load_status(acute, chronic, chronic_14d_ago, readiness):
    if chronic is None or chronic < 30:
        return "Building" if (acute or 0) > 0 else "No load"
    ratio = acute / chronic
    trend = (chronic - chronic_14d_ago) / chronic_14d_ago if chronic_14d_ago else 0
    if ratio > 1.5:
        return "Overreaching"
    if readiness is not None and readiness < 40 and ratio > 1.1:
        return "Strained"
    if ratio > 1.3:
        return "High load"
    if ratio < 0.8:
        return "Detraining" if trend < -0.1 else "Recovery"
    return "Productive" if trend > 0.05 else "Maintaining"


# strain (illness / overload early warning): signal -> (direction that is "worse", scale floor)
STRAIN_SIGNALS = {"rhr": (1, 1.5), "hrv": (-1, 3.0), "respiration": (1, 0.4), "skin_temp": (1, 0.15), "spo2_night": (-1, 0.5)}


def robust_z(value, base_vals, floor, min_n=10):
    """Robust z-score vs. a baseline (median / 1.4826*MAD, scale floored). Returns (z, median) or None."""
    base = [x for x in base_vals if x is not None]
    if value is None or len(base) < min_n:
        return None
    med = statistics.median(base)
    mad = statistics.median([abs(x - med) for x in base])
    return (value - med) / max(1.4826 * mad, floor), med


def strain_assess(zs: dict):
    """zs: signal -> z where positive = worse. Returns (score 0-100 | None, level | None)."""
    zs = {k: v for k, v in zs.items() if v is not None}
    if len(zs) < 2:
        return None, None
    v = list(zs.values())
    score = round(clamp(100 * (sum(max(0.0, z) for z in v) / len(v)) / 2.5, 0, 100))
    n15, n25 = sum(z >= 1.5 for z in v), sum(z >= 2.5 for z in v)
    level = "alert" if n15 >= 3 or n25 >= 2 else "watch" if n15 >= 2 else "normal"
    return score, level


def _clock_min(ts, zone, ref=0):
    """Local clock time of an epoch in minutes, relative to `ref` minutes after midnight (wraps mod 1440)."""
    t = dt.datetime.fromtimestamp(ts, zone)
    return (t.hour * 60 + t.minute + t.second / 60 - ref) % 1440


def circ_sd_min(mins):
    """Circular standard deviation (minutes) of clock times given in minutes of a 24 h day."""
    if len(mins) < 2:
        return None
    ang = [x / 1440 * 2 * math.pi for x in mins]
    R = math.hypot(sum(map(math.cos, ang)), sum(map(math.sin, ang))) / len(ang)
    return math.sqrt(-2 * math.log(min(1.0, max(R, 1e-9)))) * 1440 / (2 * math.pi)


def sleep_regularity(bed_sd, wake_sd):
    """0-100: 100 when the mean SD <= 15 min, linearly down to 0 at 120 min."""
    if bed_sd is None or wake_sd is None:
        return None
    return round(clamp(100 * (120 - (bed_sd + wake_sd) / 2) / 105, 0, 100))


def social_jetlag(free_mids, work_mids):
    """|mean midpoint free days - mean midpoint work days| in minutes (midpoints relative to noon)."""
    if len(free_mids) < 2 or len(work_mids) < 2:
        return None
    return abs(statistics.mean(free_mids) - statistics.mean(work_mids))


def sleep_debt(hours, target_h, min_n=4):
    """Sum of shortfalls vs. target over nights with data (no banking); None if too few nights."""
    hs = [h for h in hours if h is not None]
    return None if len(hs) < min_n else round(sum(max(0.0, target_h - h) for h in hs), 1)


def vo2max_uth(hrmax, rhr):
    return 15.3 * hrmax / rhr if rhr else None


def fitness_age(vo2, sex="m"):
    """Rough inverse of age-normalised VO2max (HUNT study trend ~ -0.4 ml/kg/min per year)."""
    if not vo2:
        return None
    ref20 = 54.0 if sex != "f" else 46.0
    return int(round(clamp(20 + (ref20 - vo2) / 0.42, 15, 90)))


# --------------------------------------------------------------------------------------
# Recompute pipeline
# --------------------------------------------------------------------------------------

@dataclass
class Series:
    ts: list = field(default_factory=list)
    v: list = field(default_factory=list)

    def window(self, a, b):
        i, j = bisect.bisect_left(self.ts, a), bisect.bisect_left(self.ts, b)
        return self.ts[i:j], self.v[i:j]

    def nearest(self, t, tol):
        i = bisect.bisect_left(self.ts, t)
        best = None
        for k in (i - 1, i):
            if 0 <= k < len(self.ts) and abs(self.ts[k] - t) <= tol:
                if best is None or abs(self.ts[k] - t) < abs(self.ts[best] - t):
                    best = k
        return None if best is None else self.v[best]


def _load(sql, cols=2):
    s = Series()
    for row in db.q(sql):
        s.ts.append(row[0])
        s.v.append(row[1] if cols == 2 else tuple(row[1:]))
    return s


def rolling_min_mean(ts, vals, window=1800, min_n=3):
    best = None
    j, tot = 0, 0.0
    for i in range(len(ts)):
        tot += vals[i]
        while ts[i] - ts[j] > window:
            tot -= vals[j]
            j += 1
        if i - j + 1 >= min_n:
            m = tot / (i - j + 1)
            best = m if best is None or m < best else best
    return best


def _local_date(ts, zone):
    return dt.datetime.fromtimestamp(ts, zone).date()


def _day_bounds(d: dt.date, zone):
    a = dt.datetime.combine(d, dt.time(), zone)
    b = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), zone)
    return int(a.timestamp()), int(b.timestamp())


def recompute_all() -> int:
    zone = tz()
    prof = profile()
    hr = _load("SELECT ts, bpm FROM hr ORDER BY ts")
    stp = _load("SELECT ts, steps FROM steps ORDER BY ts")
    ser = _load("SELECT ts, rmssd, rr, snoring FROM sleep_series ORDER BY ts", cols=4)
    states = _load("SELECT start, end, state FROM sleep_states ORDER BY start", cols=3)
    spo2 = _load("SELECT ts, value FROM spo2 ORDER BY ts")
    sessions = [dict(r) for r in db.q("SELECT * FROM sleep_sessions ORDER BY start")]
    for s in sessions:
        s["data"] = json.loads(s["data"] or "{}")
    activity = {r["date"]: json.loads(r["data"]) for r in db.q("SELECT * FROM activity_daily")}
    workouts = [dict(r) for r in db.q("SELECT * FROM workouts ORDER BY start")]
    meas = {}
    for r in db.q("SELECT type, ts, value FROM measures ORDER BY ts"):
        meas.setdefault(r["type"], Series()).ts.append(r["ts"])
        meas[r["type"]].v.append(r["value"])

    # date range
    candidates = [x for x in (hr.ts[:1] + stp.ts[:1] + [s["start"] for s in sessions[:1]]) if x]
    if not candidates:
        with db.tx() as c:
            c.execute("DELETE FROM daily_metrics")
            c.execute("DELETE FROM intraday_derived")
        return 0
    first = _local_date(min(candidates), zone)
    last = max(_local_date(max(hr.ts[-1:] + stp.ts[-1:] + [s["end"] for s in sessions[-1:]]), zone),
               dt.datetime.now(zone).date())
    last = min(last, dt.datetime.now(zone).date())

    # max HR: profile override, else age formula (Tanaka/HUNT), raised by robust observed peak
    age = dt.date.today().year - int(prof.get("birth_year") or 1990)
    hrmax = float(prof["max_hr"]) if prof.get("max_hr") else 211 - 0.64 * age
    if not prof.get("max_hr") and len(hr.v) > 200:
        observed = sorted(hr.v)[int(len(hr.v) * 0.9995)]
        hrmax = max(hrmax, float(observed))
    sex = prof.get("sex", "m")
    target_h = float(prof.get("sleep_target_h") or 8)

    # sleep windows by wake-up date; main = longest sleep
    by_date: dict[dt.date, list] = {}
    for s in sessions:
        by_date.setdefault(_local_date(s["end"], zone), []).append(s)
    windows = sorted((s["start"], s["end"]) for s in sessions)
    wstarts = [w[0] for w in windows]

    def in_sleep(t):
        i = bisect.bisect_right(wstarts, t) - 1
        return i >= 0 and windows[i][0] <= t < windows[i][1]

    def stage_at(t):
        i = bisect.bisect_right(states.ts, t) - 1
        if i >= 0:
            end, st = states.v[i]
            if t < end:
                return st
        return 1

    out_days, intraday = [], {}
    hist = {"hrv": [], "rhr": [], "sleep": [], "chronic": []}
    acute_e = chronic_e = None
    sig_hist = {k: [] for k in STRAIN_SIGNALS}
    nights = {}  # date -> (sleep_start, sleep_end, hours incl. naps)
    strain_streak = 0
    energy = 60.0
    recovery = 0.0

    d = first
    while d <= last:
        a, b = _day_bounds(d, zone)
        m: dict = {"date": d.isoformat()}

        # ---- sleep (night ending this day) -------------------------------------------
        sl = sorted(by_date.get(d, []), key=lambda s: s["end"] - s["start"], reverse=True)
        main = sl[0] if sl else None
        if main:
            sd = main["data"]
            st_ts, st_v = states.window(main["start"], main["end"])
            dur = {0: 0, 1: 0, 2: 0, 3: 0}
            for t0, (t1, stg) in zip(st_ts, st_v):
                dur[stg] = dur.get(stg, 0) + (min(t1, main["end"]) - t0)
            deep = sd.get("deepsleepduration", dur[2])
            light = sd.get("lightsleepduration", dur[1])
            rem = sd.get("remsleepduration", dur[3])
            tst = sd.get("total_sleep_time") or (deep + light + rem)
            inbed = sd.get("total_timeinbed") or (main["end"] - main["start"])
            eff = sd.get("sleep_efficiency") or (tst / inbed if inbed else None)
            wakeups = sd.get("wakeupcount")
            waso = sd.get("waso", sd.get("wakeupduration", dur[0]))
            sc, comps = sleep_score(tst, deep, rem, eff, wakeups, waso, target_h)
            n_ts, n_hr = hr.window(main["start"], main["end"])
            n_ser = ser.window(main["start"], main["end"])[1]
            rmssd = [x[0] for x in n_ser if x[0] and 5 <= x[0] <= 250]
            rr = [x[1] for x in n_ser if x[1]]
            m.update({
                "sleep_start": main["start"], "sleep_end": main["end"],
                "sleep_total_s": tst, "sleep_deep_s": deep, "sleep_light_s": light,
                "sleep_rem_s": rem, "sleep_awake_s": waso, "sleep_inbed_s": inbed,
                "sleep_efficiency": r1(eff, 3), "sleep_wakeups": wakeups,
                "sleep_latency_s": sd.get("sleep_latency", sd.get("durationtosleep")),
                "sleep_score": sc, "sleep_score_parts": comps,
                "sleep_score_withings": sd.get("sleep_score"),
                "sleep_hr_avg": r1(mean(n_hr) or sd.get("hr_average")),
                "sleep_hr_min": min(n_hr) if n_hr else sd.get("hr_min"),
                "respiration": r1(mean(rr) or sd.get("rr_average")),
                "snoring_s": sd.get("snoring"), "ahi": sd.get("apnea_hypopnea_index"),
                "breathing_disturbances": sd.get("breathing_disturbances_intensity"),
                "hrv": r1(mean(rmssd)),
                "naps_s": sum(s["data"].get("total_sleep_time") or (s["end"] - s["start"]) for s in sl[1:]),
            })
            sp = spo2.window(main["start"], main["end"])[1]
            if sp:
                m["spo2_night"] = r1(mean(sp))

        # ---- resting HR ------------------------------------------------------------
        lo = min(a, main["start"]) if main else a
        d_ts, d_hr = hr.window(lo, b)
        rhr = rolling_min_mean(d_ts, d_hr)
        if rhr is None and main:
            rhr = main["data"].get("hr_min")
        m["rhr"] = r1(rhr, 0) if rhr else None
        rhr_base = mean(hist["rhr"][-28:]) if hist["rhr"] else None
        rhr_ref = mean(hist["rhr"][-7:] + ([rhr] if rhr else [])) or rhr or 60

        # ---- HRV baseline/status --------------------------------------------------------
        if m.get("hrv"):
            hist["hrv"].append(m["hrv"])
        hrv_week = mean(hist["hrv"][-7:]) if m.get("hrv") else None
        base = hist["hrv"][-35:-7] if len(hist["hrv"]) > 21 else hist["hrv"][-28:]
        st, blo, bhi = hrv_status(hrv_week, base)
        hrv_base_mean = statistics.mean(base) if len(base) >= 7 else None
        m.update({"hrv_7d": r1(hrv_week), "hrv_status": st if m.get("hrv") else None,
                  "hrv_base_low": blo, "hrv_base_high": bhi})
        hrv_ratio = (m["hrv"] / hrv_base_mean) if m.get("hrv") and hrv_base_mean else None

        # ---- awake HR: zones, TRIMP, stress ---------------------------------------------
        a_ts, a_hr = hr.window(a, b)
        zones = [0.0] * 6
        trimp_samples, stress_pts, active_min = [], [], 0.0
        for i, (t, h) in enumerate(zip(a_ts, a_hr)):
            if in_sleep(t):
                continue
            nxt = a_ts[i + 1] if i + 1 < len(a_ts) else t + 60
            minutes = min(nxt - t, 600) / 60
            z = hr_zone(h, hrmax)
            zones[z] += minutes
            trimp_samples.append((h, minutes))
            s_ts, s_v = stp.window(t - 300, t + 300)
            moving = sum(s_v) > 60 or h >= 0.6 * hrmax
            if moving:
                active_min += minutes if z >= 2 else 0
                intraday.setdefault(t, {})["stress"] = -1  # active
            else:
                s = stress_value(h, rhr_ref, hrmax)
                stress_pts.append((s, minutes))
                intraday.setdefault(t, {})["stress"] = s
        load = trimp(trimp_samples, rhr_ref, hrmax, sex)
        buckets = {"rest": 0.0, "low": 0.0, "medium": 0.0, "high": 0.0}
        for s, mins in stress_pts:
            buckets[stress_bucket(s)] += mins
        tot_min = sum(x[1] for x in stress_pts)
        m.update({
            "hr_zones_min": [round(z) for z in zones],
            "training_load": round(load),
            "stress_avg": round(sum(s * w for s, w in stress_pts) / tot_min) if tot_min > 30 else None,
            "stress_min": {k: round(v) for k, v in buckets.items()},
            "hr_avg_awake": r1(mean([h for t, h in zip(a_ts, a_hr) if not in_sleep(t)])),
            "hr_max": max(a_hr) if a_hr else None,
        })

        # ---- steps / activity ------------------------------------------------------
        act = activity.get(d.isoformat(), {})
        steps_day = sum(stp.window(a, b)[1])
        m["steps"] = act.get("steps", steps_day) or steps_day or None
        m["distance_m"] = act.get("distance")
        m["calories_active"] = act.get("calories")
        m["calories_total"] = act.get("totalcalories")
        m["floors"] = act.get("elevation")
        if act.get("moderate") is not None or act.get("intense") is not None:
            mod, vig = (act.get("moderate") or 0) / 60, (act.get("intense") or 0) / 60
        else:
            mod, vig = zones[2] + zones[3], zones[4] + zones[5]
        m["intensity_min"] = round(mod + 2 * vig)
        m["workouts"] = sum(1 for w in workouts if a <= w["start"] < b)

        # ---- training load EWMA ---------------------------------------------------------
        # acwr as of the end of the previous day: morning readiness must not see today's training
        acwr_prev = acute_e / chronic_e if acute_e is not None and chronic_e and chronic_e * 7 >= 30 else None
        acute_e = load if acute_e is None else acute_e + (load - acute_e) * (2 / 8)
        chronic_e = load if chronic_e is None else chronic_e + (load - chronic_e) * (2 / 29)
        acute, chronic = acute_e * 7, chronic_e * 7
        hist["chronic"].append(chronic)
        chronic_14 = hist["chronic"][-15] if len(hist["chronic"]) >= 15 else None
        acwr = acute / chronic if chronic and chronic >= 30 else None

        # ---- readiness (morning) --------------------------------------------------------
        rec_morning = max(0.0, recovery - 10)
        sleep_hist = mean(hist["sleep"][-3:]) if hist["sleep"] else None
        rd, rd_parts = readiness_score(
            m.get("sleep_score"), hrv_ratio,
            (rhr - rhr_base) if rhr and rhr_base else None,
            rec_morning, acwr_prev, sleep_hist)
        m.update({"readiness": rd, "readiness_label": readiness_label(rd), "readiness_parts": rd_parts})

        speed = 0.7 + 0.5 * ((m.get("sleep_score") or 70) / 100)
        recovery = clamp(max(0.0, recovery - 24 * speed) + load * 0.35, 0, 96)
        m.update({
            "load_acute": round(acute), "load_chronic": round(chronic),
            "acwr": r1(acwr, 2), "load_status": load_status(acute, chronic, chronic_14, rd),
            "recovery_h": round(recovery),
        })

        # ---- strain: overnight signals vs. own trailing 28-day baseline (excl. today) -------
        zs, ssig = {}, {}
        for k, (sign, floor) in STRAIN_SIGNALS.items():
            rz = robust_z(m.get(k), sig_hist[k][-28:], floor)
            if rz:
                zs[k] = sign * rz[0]
                ssig[k] = {"value": m[k], "baseline": r1(rz[1], 2), "z": round(zs[k], 1)}
        sscore, slevel = strain_assess(zs)
        strain_streak = strain_streak + 1 if slevel in ("watch", "alert") else 0
        m.update({"strain_signals": ssig, "strain_score": sscore, "strain_level": slevel,
                  "strain_streak": strain_streak})
        for k in STRAIN_SIGNALS:
            if m.get(k) is not None:
                sig_hist[k].append(m[k])

        # ---- sleep regularity / social jetlag / debt ----------------------------------------
        if main:
            nights[d] = (main["start"], main["end"], ((m.get("sleep_total_s") or 0) + (m.get("naps_s") or 0)) / 3600)
        w7 = [nights[d - dt.timedelta(days=i)] for i in range(7) if d - dt.timedelta(days=i) in nights]
        bsd = wsd = None
        if len(w7) >= 4:
            bsd = circ_sd_min([_clock_min(n[0], zone) for n in w7])
            wsd = circ_sd_min([_clock_min(n[1], zone) for n in w7])
        free, work = [], []
        for i in range(28):
            dd = d - dt.timedelta(days=i)
            if dd in nights:
                n = nights[dd]
                (free if dd.weekday() >= 5 else work).append(_clock_min((n[0] + n[1]) / 2, zone, 720))
        sjl = social_jetlag(free, work)
        m.update({
            "bedtime_sd_min": None if bsd is None else round(bsd), "waketime_sd_min": None if wsd is None else round(wsd),
            "sleep_midpoint_min": round(_clock_min((main["start"] + main["end"]) / 2, zone)) if main else None,
            "social_jetlag_min": None if sjl is None else round(sjl), "sleep_regularity": sleep_regularity(bsd, wsd),
            "sleep_debt_h": sleep_debt([nights[d - dt.timedelta(days=i)][2] for i in range(7)
                                        if d - dt.timedelta(days=i) in nights], target_h),
        })

        # ---- body energy simulation -----------------------------------------------------
        if hrv_ratio is not None:
            rec_f = clamp(hrv_ratio, 0.6, 1.25)
        else:
            rec_f = 1.0
        if rhr and rhr_base and rhr - rhr_base > 3:
            rec_f *= 0.85
        e_lo, e_hi, charged, drained, e_wake = energy, energy, 0.0, 0.0, None
        for t in range(a, b, STEP):
            if t > dt.datetime.now().timestamp():
                break
            before = energy
            h_step = STEP / 3600
            if in_sleep(t):
                rate = {0: 1, 1: 10, 2: 14, 3: 8}.get(stage_at(t), 9)
                energy += rate * rec_f * (1 - energy / 112) * h_step
            else:
                h = hr.nearest(t, 600)
                if h is None:
                    energy -= 2.5 * h_step
                else:
                    s_v = stp.window(t - 300, t + 300)[1]
                    if sum(s_v) > 60 or h >= 0.6 * hrmax:
                        x = clamp((h - rhr_ref) / max(1, hrmax - rhr_ref), 0, 1)
                        energy -= (4 + 25 * x * x) * h_step
                    else:
                        s = stress_value(h, rhr_ref, hrmax)
                        energy += (1.5 if s <= 25 else -(1.0 + s * 0.1)) * h_step
            energy = clamp(energy, 5, 100)
            if main and e_wake is None and t >= main["end"]:
                e_wake = energy
            diff = energy - before
            charged += max(0, diff)
            drained += max(0, -diff)
            e_lo, e_hi = min(e_lo, energy), max(e_hi, energy)
            intraday.setdefault(t, {})["energy"] = round(energy, 1)
        m.update({
            "energy_wake": round(e_wake) if e_wake is not None else None,
            "energy_high": round(e_hi), "energy_low": round(e_lo), "energy_end": round(energy),
            "energy_charged": round(charged), "energy_drained": round(drained),
        })

        # ---- vitals / body -------------------------------------------------------------
        def latest(mtype, until=b, within=None):
            s = meas.get(mtype)
            if not s:
                return None
            i = bisect.bisect_left(s.ts, until) - 1
            if i < 0 or (within and until - s.ts[i] > within):
                return None
            return s.v[i]

        vo2_w = latest(123)
        vo2 = vo2_w or (vo2max_uth(hrmax, rhr_ref) if hist["rhr"] else None)
        m.update({
            "vo2max": r1(vo2), "vo2max_source": "withings" if vo2_w else "estimate",
            "fitness_age": fitness_age(vo2, sex),
            "weight": r1(latest(1), 2), "fat_ratio": r1(latest(6)),
            "muscle_mass": r1(latest(76)),
            "bp_sys": latest(10, within=86400), "bp_dia": latest(9, within=86400),
            "skin_temp": r1(latest(73, within=86400) or latest(71, within=86400), 2),
            "spo2": r1(mean(spo2.window(a, b)[1]) or latest(54, within=86400)),
            "hrmax_used": round(hrmax),
        })
        if not m.get("spo2"):
            m["spo2"] = m.get("spo2_night")

        if rhr:
            hist["rhr"].append(rhr)
        if m.get("sleep_score") is not None:
            hist["sleep"].append(m["sleep_score"])
        out_days.append(m)
        d += dt.timedelta(days=1)

    with db.tx() as c:
        c.execute("DELETE FROM daily_metrics")
        c.executemany("INSERT INTO daily_metrics(date,data) VALUES (?,?)",
                      [(m["date"], json.dumps(m)) for m in out_days])
        c.execute("DELETE FROM intraday_derived")
        c.executemany("INSERT INTO intraday_derived(ts,stress,energy) VALUES (?,?,?)",
                      [(t, v.get("stress"), v.get("energy")) for t, v in intraday.items()])
    db.kv_set("metrics_hrmax", round(hrmax))
    return len(out_days)


# --------------------------------------------------------------------------------------
# Read helpers for the API
# --------------------------------------------------------------------------------------

def daily(start: str | None = None, end: str | None = None) -> list[dict]:
    sql, args = "SELECT data FROM daily_metrics", []
    if start and end:
        sql += " WHERE date BETWEEN ? AND ?"
        args = [start, end]
    return [json.loads(r[0]) for r in db.q(sql + " ORDER BY date", args)]


def day_detail(date: str) -> dict:
    zone = tz()
    d = dt.date.fromisoformat(date)
    a, b = _day_bounds(d, zone)
    row = db.q1("SELECT data FROM daily_metrics WHERE date=?", (date,))
    m = json.loads(row[0]) if row else {"date": date}
    lo = m.get("sleep_start") or a
    hr_rows = db.q("SELECT ts, bpm FROM hr WHERE ts>=? AND ts<? ORDER BY ts", (min(lo, a), b))
    der = db.q("SELECT ts, stress, energy FROM intraday_derived WHERE ts>=? AND ts<? ORDER BY ts", (a, b))
    steps = db.q("SELECT ts, steps FROM steps WHERE ts>=? AND ts<? ORDER BY ts", (a, b))
    out = {
        "metrics": m,
        "hr": [[r[0], r[1]] for r in hr_rows],
        "stress": [[r[0], r[1]] for r in der if r[1] is not None],
        "energy": [[r[0], r[2]] for r in der if r[2] is not None],
        "steps": [[r[0], r[1]] for r in steps],
        "workouts": [dict(r) | {"data": json.loads(r["data"] or "{}")} for r in
                     db.q("SELECT * FROM workouts WHERE start>=? AND start<? ORDER BY start", (a, b))],
    }
    if m.get("sleep_start"):
        s0, s1 = m["sleep_start"], m["sleep_end"]
        out["sleep_states"] = [[r[0], r[1], r[2]] for r in db.q(
            "SELECT start, end, state FROM sleep_states WHERE start>=? AND start<? ORDER BY start", (s0, s1))]
        out["sleep_series"] = [[r[0], r[1], r[2]] for r in db.q(
            "SELECT ts, rmssd, rr FROM sleep_series WHERE ts>=? AND ts<? ORDER BY ts", (s0, s1))]
    # trailing week of key metrics for context
    week = daily((d - dt.timedelta(days=6)).isoformat(), date)
    out["week"] = [{k: x.get(k) for k in ("date", "readiness", "sleep_score", "hrv", "rhr",
                                         "stress_avg", "steps", "training_load")} for x in week]
    return out


def workouts(start_ts: int, end_ts: int) -> list[dict]:
    prof = profile()
    hrmax = db.kv_get("metrics_hrmax") or 190
    rows = db.q("SELECT * FROM workouts WHERE start>=? AND start<? ORDER BY start DESC", (start_ts, end_ts))
    out = []
    for r in rows:
        w = dict(r) | {"data": json.loads(r["data"] or "{}")}
        hrs = db.q("SELECT ts, bpm FROM hr WHERE ts>=? AND ts<=? ORDER BY ts", (r["start"], r["end"]))
        ts = [h[0] for h in hrs]
        samples = []
        zones = [0.0] * 6
        for i, (t, b) in enumerate(hrs):
            nxt = ts[i + 1] if i + 1 < len(ts) else r["end"]
            mins = min(nxt - t, 600) / 60
            samples.append((b, mins))
            zones[hr_zone(b, hrmax)] += mins
        day = db.q1("SELECT data FROM daily_metrics WHERE date=?",
                    (_local_date(r["start"], tz()).isoformat(),))
        rhr = (json.loads(day[0]).get("rhr") if day else None) or 60
        w["trimp"] = round(trimp(samples, rhr, hrmax, prof.get("sex", "m")))
        w["zones_min"] = [round(z, 1) for z in zones]
        w["hr_avg"] = round(mean([s[0] for s in samples])) if samples else w["data"].get("hr_average")
        w["hr_max"] = max((s[0] for s in samples), default=w["data"].get("hr_max"))
        out.append(w)
    return out
