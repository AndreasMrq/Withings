"""Generate realistic synthetic Withings-like data + journal entries.

Hidden patterns are baked in (alcohol -> lower HRV / worse sleep, late caffeine -> less deep
sleep, little sleep -> headache/tired, ...) so the Insights page has something to discover.

    python -m app.demo [days]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys

from . import db, metrics

CATEGORY_RUN, CATEGORY_BIKE, CATEGORY_WEIGHTS = 2, 6, 16


def generate(days: int = 150, seed: int = 7) -> None:
    rnd = random.Random(seed)
    zone = metrics.tz()
    today = dt.datetime.now(zone).date()
    start_day = today - dt.timedelta(days=days)

    hr, steps, spo2, series, states, sessions, acts, wos, meas, journal = ([] for _ in range(10))
    fitness = 0.0  # slowly improving
    weight = 79.5
    sick_left = 0
    prev = {"alcohol": False, "short": False}
    wid = 1

    def ts_of(d, h, m=0):
        return int(dt.datetime.combine(d, dt.time(0), zone).timestamp() + h * 3600 + m * 60)

    for i in range(days + 1):
        d = start_day + dt.timedelta(days=i)
        wd = d.weekday()
        weekend = wd >= 5
        fitness += 0.02
        if sick_left == 0 and rnd.random() < 0.012:
            sick_left = rnd.randint(2, 4)
        sick = sick_left > 0
        sick_left = max(0, sick_left - 1)

        # ---- today's events (affect the coming night) --------------------------------
        alcohol = rnd.random() < (0.35 if wd in (4, 5) else 0.07) and not sick
        caffeine = rnd.random() < 0.15
        stressed = rnd.random() < (0.22 if wd < 5 else 0.06)
        screen = rnd.random() < 0.2
        late_meal = rnd.random() < 0.12
        workout = None
        if not sick and (wd in (0, 2, 4) or (wd == 6 and rnd.random() < 0.7)) and rnd.random() < 0.85:
            workout = rnd.choice([CATEGORY_RUN, CATEGORY_RUN, CATEGORY_BIKE, CATEGORY_WEIGHTS])
        relaxed = not stressed and rnd.random() < 0.25

        rhr_base = 55 - fitness * 0.03
        hrv_base = 52 + fitness * 0.05

        # ---- night (sleep ending on day d) ----------------------------------------------
        if i > 0:
            pd = d - dt.timedelta(days=1)
            hrv_f, rhr_add, deep_f, wake_add, lat_add = 1.0, 0.0, 1.0, 0, 0
            pe = prev
            if pe["alcohol"]:
                hrv_f *= 0.72; rhr_add += 5; deep_f *= 0.7; wake_add += 2
            if pe["caffeine"]:
                deep_f *= 0.8; lat_add += 25
            if pe["stressed"]:
                hrv_f *= 0.9; rhr_add += 1.5
            if pe["workout"] in (CATEGORY_RUN, CATEGORY_BIKE):
                hrv_f *= 0.93; rhr_add += 1.5; deep_f *= 1.1
            if pe["late_meal"]:
                rhr_add += 2.5; hrv_f *= 0.94
            if pe["relaxed"]:
                hrv_f *= 1.06
            if sick:
                hrv_f *= 0.68; rhr_add += 8; wake_add += 2
            bed_h = 23.0 + rnd.gauss(0, 0.55) + (0.9 if pe["screen"] else 0) + (0.6 if pe["alcohol"] else 0) \
                + (0.5 if pe["weekend_eve"] else 0)
            wake_h = (6.6 if not weekend else 8.0) + rnd.gauss(0, 0.35)
            s0 = ts_of(pd, bed_h)
            s1 = ts_of(d, wake_h)
            hrv_night = max(15, rnd.gauss(hrv_base * hrv_f, 4))
            rhr_night = rnd.gauss(rhr_base + rhr_add, 1.2)
            # build stage segments in ~90 min cycles
            t = s0
            seg = [(t, t + (12 + lat_add) * 60, 0)]
            t += (12 + lat_add) * 60
            cyc = 0
            wakes_left = rnd.randint(0, 2) + wake_add
            while t < s1 - 600:
                frac = (t - s0) / (s1 - s0)
                deep_len = max(4, rnd.gauss(30 * (1 - frac) * deep_f + 4, 5))
                rem_len = max(4, rnd.gauss(8 + 25 * frac, 4))
                light_len = max(10, rnd.gauss(40, 8))
                for st, ln in ((1, light_len * 0.6), (2, deep_len), (1, light_len * 0.4), (3, rem_len)):
                    e = min(s1, t + int(ln * 60))
                    seg.append((t, e, st))
                    t = e
                    if t >= s1:
                        break
                if wakes_left and rnd.random() < 0.5 and t < s1 - 900:
                    e = t + rnd.randint(3, 12) * 60
                    seg.append((t, e, 0))
                    t = e
                    wakes_left -= 1
                cyc += 1
            states.extend(seg)
            dur = {0: 0, 1: 0, 2: 0, 3: 0}
            for a, b, st in seg:
                dur[st] += b - a
                for tt in range(a, b, 120):
                    frac = (tt - s0) / (s1 - s0)
                    dip = -3 * math.sin(math.pi * min(1, frac * 1.3))
                    off = {0: 7, 1: 1, 2: -2, 3: 4}[st]
                    hr.append((tt, int(round(rhr_night + 3 + dip + off + rnd.gauss(0, 1.5))), "sleep"))
                for tt in range(a - a % 300, b, 300):
                    if tt < a:
                        continue
                    fac = {0: 0.7, 1: 1.0, 2: 1.25, 3: 0.85}[st]
                    series.append((tt, round(rnd.gauss(14.5, 0.8), 1), 0,
                                   round(max(8, rnd.gauss(hrv_night * fac, hrv_night * 0.12)), 1),
                                   round(max(10, rnd.gauss(hrv_night * fac * 1.3, 6)), 1), 0))
            tst = dur[1] + dur[2] + dur[3]
            nwake = sum(1 for s in seg[1:] if s[2] == 0)
            sessions.append({
                "id": 100000 + i, "date": d.isoformat(), "start": s0, "end": s1, "tz": str(zone),
                "data": {
                    "deepsleepduration": dur[2], "lightsleepduration": dur[1], "remsleepduration": dur[3],
                    "wakeupduration": dur[0], "wakeupcount": nwake, "total_sleep_time": tst,
                    "total_timeinbed": s1 - s0, "sleep_efficiency": round(tst / (s1 - s0), 3),
                    "sleep_latency": (12 + lat_add) * 60, "waso": dur[0] - (12 + lat_add) * 60,
                    "hr_average": round(rhr_night + 3), "hr_min": round(rhr_night - 2),
                    "rr_average": 14, "sleep_score": None, "snoring": rnd.randint(0, 900),
                },
            })
            short = tst < 6.3 * 3600
            for tt in range(s0 + 3600, s1, 3600 * 2):
                spo2.append((tt, round(min(100, rnd.gauss(96.5, 0.8)), 1)))
        else:
            short = False

        # ---- day -----------------------------------------------------------------------
        wake = ts_of(d, 7.0 if not weekend else 8.2)
        bed_next = ts_of(d, 22.75)
        day_steps = 0
        work_s = work_e = None
        if workout:
            wh = rnd.choice([7.5, 12.3, 18.0]) if not weekend else rnd.choice([10.0, 16.0])
            work_s = ts_of(d, wh)
            work_e = work_s + rnd.randint(40, 75) * 60
        stress_win = (ts_of(d, rnd.uniform(9, 14)), None)
        stress_win = (stress_win[0], stress_win[0] + int(rnd.uniform(2, 5) * 3600)) if stressed else (0, 0)
        t = wake + 600
        while t < bed_next:
            if work_s and work_s <= t < work_e:
                t = work_e
                continue
            walking = rnd.random() < 0.09
            st_n = rnd.randint(250, 900) if walking else rnd.randint(0, 40)
            steps.append((t, st_n, 600, st_n * 0.04, st_n * 0.75, 0))
            day_steps += st_n
            base = rhr_base + 14 + (8 if sick else 0)
            if walking:
                base += 22
            elif stress_win[0] <= t < stress_win[1]:
                base += rnd.uniform(9, 20)
            elif relaxed:
                base -= 4
            if alcohol and t > ts_of(d, 19):
                base += 8
            hr.append((t, int(round(base + rnd.gauss(0, 3))), "intraday"))
            t += 600
        if workout:
            cat = workout
            peak = {CATEGORY_RUN: 158, CATEGORY_BIKE: 145, CATEGORY_WEIGHTS: 118}[cat] + rnd.gauss(0, 6)
            tt = work_s
            hrs = []
            while tt < work_e:
                frac = (tt - work_s) / (work_e - work_s)
                ramp = min(1, frac * 6) * (1 if frac < 0.9 else (1 - frac) * 6 + 0.4)
                b = int(round(85 + (peak - 85) * ramp + rnd.gauss(0, 4)))
                hrs.append(b)
                hr.append((tt, b, "intraday"))
                if cat == CATEGORY_RUN:
                    steps.append((tt, rnd.randint(150, 175), 60, 12, 160, 0))
                    day_steps += 160
                tt += 60
            wos.append({"id": wid, "category": cat, "start": work_s, "end": work_e, "date": d.isoformat(),
                        "data": {"calories": int((work_e - work_s) / 60 * 10), "hr_average": int(sum(hrs) / len(hrs)),
                                 "hr_max": max(hrs), "hr_min": min(hrs),
                                 "distance": int((work_e - work_s) / 60 * (180 if cat == CATEGORY_RUN else 400 if cat == CATEGORY_BIKE else 0))}})
            wid += 1
        acts.append((d.isoformat(), {
            "date": d.isoformat(), "steps": day_steps, "distance": day_steps * 0.75,
            "calories": int(day_steps * 0.04 + (500 if workout else 0)),
            "totalcalories": int(1750 + day_steps * 0.04 + (500 if workout else 0)),
            "soft": rnd.randint(3000, 9000), "moderate": rnd.randint(600, 2400) + (1500 if workout else 0),
            "intense": (rnd.randint(900, 2400) if workout in (CATEGORY_RUN, CATEGORY_BIKE) else 0),
            "elevation": rnd.randint(2, 20),
        }))
        if wd == 0 or rnd.random() < 0.1:
            weight += rnd.gauss(-0.03, 0.25)
            meas.append((wake + 900, 1, round(weight, 2)))
            meas.append((wake + 900, 6, round(18 - fitness * 0.01 + rnd.gauss(0, 0.4), 1)))

        # ---- journal (logged on ~85% of days) ------------------------------------------
        if d <= today and rnd.random() < 0.85:
            tags = []
            if alcohol:
                tags.append("alcohol")
            if caffeine:
                tags.append("late caffeine")
            if stressed:
                tags.append("stressed")
            if screen and rnd.random() < 0.6:
                tags.append("screen late")
            if late_meal:
                tags.append("late meal")
            if sick:
                tags.append("sick")
            if relaxed and rnd.random() < 0.5:
                tags.append("relaxed")
            if workout in (CATEGORY_RUN, CATEGORY_BIKE) and rnd.random() < 0.4:
                tags.append("great workout")
            if short and rnd.random() < 0.8:
                tags.append("little sleep")
            tired_p = 0.1 + (0.5 if short else 0) + (0.3 if prev["alcohol"] else 0) + (0.3 if sick else 0)
            if rnd.random() < tired_p:
                tags.append("tired")
            head_p = 0.04 + (0.35 if prev["alcohol"] else 0) + (0.2 if short else 0)
            if rnd.random() < head_p:
                tags.append("headache")
            mood = 3.6 + rnd.gauss(0, 0.6) - (0.8 if short else 0) - (0.6 if prev["alcohol"] else 0) \
                - (0.7 if stressed else 0) - (1.2 if sick else 0) + (0.4 if relaxed else 0) \
                + (0.3 if workout else 0) + (0.3 if weekend else 0)
            energy = mood + rnd.gauss(0, 0.5) - (0.5 if short else 0)
            note = ""
            if "headache" in tags:
                note = rnd.choice(["Headache since morning", "Dull headache, drank water", ""])
            elif sick:
                note = "Cold symptoms"
            journal.append((ts_of(d, rnd.uniform(19.5, 22.5)), int(max(1, min(5, round(mood)))),
                            int(max(1, min(5, round(energy)))), json.dumps(sorted(tags)), note))

        prev = {"alcohol": alcohol, "caffeine": caffeine, "stressed": stressed, "workout": workout,
                "screen": screen, "late_meal": late_meal, "relaxed": relaxed, "short": short,
                "weekend_eve": wd in (4, 5)}

    now = int(dt.datetime.now().timestamp())
    with db.tx() as c:
        for t in ("hr", "steps", "spo2", "sleep_series", "sleep_states", "sleep_sessions",
                  "activity_daily", "workouts", "measures", "journal", "daily_metrics", "intraday_derived"):
            c.execute(f"DELETE FROM {t}")
        c.executemany("INSERT OR REPLACE INTO hr(ts,bpm,src) VALUES (?,?,?)", [h for h in hr if h[0] <= now])
        c.executemany("INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?)", [s for s in steps if s[0] <= now])
        c.executemany("INSERT OR REPLACE INTO spo2 VALUES (?,?)", spo2)
        c.executemany("INSERT OR REPLACE INTO sleep_series VALUES (?,?,?,?,?,?)", series)
        c.executemany("INSERT OR REPLACE INTO sleep_states VALUES (?,?,?)", states)
        c.executemany("INSERT INTO sleep_sessions VALUES (?,?,?,?,?,?)",
                      [(s["id"], s["date"], s["start"], s["end"], s["tz"], json.dumps(s["data"])) for s in sessions])
        c.executemany("INSERT INTO activity_daily VALUES (?,?)", [(d, json.dumps(a)) for d, a in acts])
        c.executemany("INSERT INTO workouts VALUES (?,?,?,?,?,?)",
                      [(w["id"], w["category"], w["start"], w["end"], w["date"], json.dumps(w["data"]))
                       for w in wos if w["start"] <= now])
        c.executemany("INSERT INTO measures VALUES (?,?,?,?)",
                      [(k, m[1], m[0], m[2]) for k, m in enumerate(meas) if m[0] <= now])
        c.executemany("INSERT INTO journal(ts,mood,energy,tags,note) VALUES (?,?,?,?,?)",
                      [j for j in journal if j[0] <= now])
    db.kv_set("demo", True)
    metrics.recompute_all()


if __name__ == "__main__":
    generate(int(sys.argv[1]) if len(sys.argv) > 1 else 150)
    print("demo data generated")
