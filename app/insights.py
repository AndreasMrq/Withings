"""Journal storage and pattern discovery between journal tags/mood and sensor metrics."""
from __future__ import annotations

import datetime as dt
import json
import math

from . import db, metrics

DAY_ROLLOVER_H = 4  # entries before 04:00 count towards the previous day (late drinks etc.)


# --------------------------------------------------------------------------------------
# Journal CRUD
# --------------------------------------------------------------------------------------

def _row(r) -> dict:
    return {"id": r["id"], "ts": r["ts"], "mood": r["mood"], "energy": r["energy"],
            "tags": json.loads(r["tags"]), "note": r["note"]}


def add_entry(ts: int, mood, energy, tags: list[str], note: str) -> dict:
    tags = sorted({t.strip().lower() for t in tags if t and t.strip()})
    with db.tx() as c:
        cur = c.execute("INSERT INTO journal(ts,mood,energy,tags,note) VALUES (?,?,?,?,?)",
                        (ts, mood, energy, json.dumps(tags), note or ""))
        for t in tags:  # new tags become quick-pick chips
            c.execute("INSERT OR IGNORE INTO tags(name, emoji, sort) VALUES (?, '', 999)", (t,))
        rid = cur.lastrowid
    return _row(db.q1("SELECT * FROM journal WHERE id=?", (rid,)))


def update_entry(eid: int, ts: int, mood, energy, tags: list[str], note: str) -> dict | None:
    tags = sorted({t.strip().lower() for t in tags if t and t.strip()})
    with db.tx() as c:
        c.execute("UPDATE journal SET ts=?, mood=?, energy=?, tags=?, note=? WHERE id=?",
                  (ts, mood, energy, json.dumps(tags), note or "", eid))
        for t in tags:
            c.execute("INSERT OR IGNORE INTO tags(name, emoji, sort) VALUES (?, '', 999)", (t,))
    r = db.q1("SELECT * FROM journal WHERE id=?", (eid,))
    return _row(r) if r else None


def delete_entry(eid: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM journal WHERE id=?", (eid,))


def entries(start_ts: int = 0, end_ts: int = 2**40, limit: int = 500) -> list[dict]:
    return [_row(r) for r in db.q(
        "SELECT * FROM journal WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT ?", (start_ts, end_ts, limit))]


def tags() -> list[dict]:
    used = {}
    for r in db.q("SELECT tags FROM journal"):
        for t in json.loads(r[0]):
            used[t] = used.get(t, 0) + 1
    return [{"name": r["name"], "emoji": r["emoji"] or "", "count": used.get(r["name"], 0)}
            for r in db.q("SELECT * FROM tags ORDER BY sort, name")]


def set_tag(name: str, emoji: str = "") -> None:
    with db.tx() as c:
        c.execute("INSERT INTO tags(name, emoji, sort) VALUES (?,?,999) "
                  "ON CONFLICT(name) DO UPDATE SET emoji=excluded.emoji", (name.strip().lower(), emoji))


def delete_tag(name: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM tags WHERE name=?", (name,))


def journal_day(ts: int, zone) -> dt.date:
    return (dt.datetime.fromtimestamp(ts, zone) - dt.timedelta(hours=DAY_ROLLOVER_H)).date()


# --------------------------------------------------------------------------------------
# Statistics helpers (no numpy/scipy needed)
# --------------------------------------------------------------------------------------

def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-30 else 1e-30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-30 else 1e-30
        de = d * c
        h *= de
        if abs(de - 1) < 3e-12:
            break
    return h


def _betai(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    bt = math.exp(lbt)
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def t_pvalue(t, df):
    """Two-sided p-value of Student's t."""
    if df <= 0 or not math.isfinite(t):
        return 1.0
    return _betai(df / 2, 0.5, df / (df + t * t))


def welch(x, y):
    """Returns (diff, cohen_d, p) comparing mean(x) - mean(y)."""
    nx, ny = len(x), len(y)
    mx, my = sum(x) / nx, sum(y) / ny
    vx = sum((v - mx) ** 2 for v in x) / (nx - 1)
    vy = sum((v - my) ** 2 for v in y) / (ny - 1)
    se2 = vx / nx + vy / ny
    pooled = math.sqrt(((nx - 1) * vx + (ny - 1) * vy) / (nx + ny - 2)) or 1e-9
    d = (mx - my) / pooled
    if se2 <= 0:
        return mx - my, d, 1.0 if mx == my else 0.0
    t = (mx - my) / math.sqrt(se2)
    df = se2 ** 2 / ((vx / nx) ** 2 / max(1, nx - 1) + (vy / ny) ** 2 / max(1, ny - 1))
    return mx - my, d, t_pvalue(t, df)


def _ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x, y):
    n = len(x)
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if sx == 0 or sy == 0:
        return 0.0, 1.0
    r = cov / (sx * sy)
    r = max(-0.999999, min(0.999999, r))
    t = r * math.sqrt((n - 2) / (1 - r * r))
    return r, t_pvalue(t, n - 2)


# --------------------------------------------------------------------------------------
# Pattern discovery
# --------------------------------------------------------------------------------------

# (key, label, offset in days relative to the tag day, higher_is_better, unit)
# Metrics of day D describe the night *before* D (sleep, HRV, readiness) or day D itself.
EFFECTS = [
    ("sleep_score", "Sleep score that night", 1, True, ""),
    ("sleep_total_h", "Sleep duration that night", 1, True, "h"),
    ("sleep_deep_pct", "Deep sleep that night", 1, True, "%"),
    ("hrv", "HRV that night", 1, True, "ms"),
    ("rhr", "Resting HR next day", 1, False, "bpm"),
    ("readiness", "Readiness next morning", 1, True, ""),
    ("energy_wake", "Body energy at wake-up next day", 1, True, ""),
    ("mood", "Mood next day", 1, True, "/5"),
    ("stress_avg", "Stress same day", 0, False, ""),
    ("mood", "Mood same day", 0, True, "/5"),
    ("steps", "Steps same day", 0, True, ""),
    ("sleep_score", "Sleep score the night before", 0, True, ""),
    ("hrv", "HRV the night before", 0, True, "ms"),
    ("readiness", "Readiness that morning", 0, True, ""),
]

MOOD_DRIVERS = [
    ("sleep_score", "Sleep score (last night)"), ("sleep_total_h", "Sleep duration (last night)"),
    ("sleep_deep_pct", "Deep sleep % (last night)"), ("hrv", "HRV (last night)"),
    ("rhr", "Resting HR"), ("readiness", "Readiness"), ("energy_wake", "Body energy at wake"),
    ("stress_avg", "Avg stress"), ("steps", "Steps"), ("training_load", "Training load"),
    ("load_acute", "Acute load (7d)"),
]


def daily_table() -> dict[str, dict]:
    zone = metrics.tz()
    rows = {m["date"]: m for m in metrics.daily()}
    for m in rows.values():
        if m.get("sleep_total_s"):
            m["sleep_total_h"] = m["sleep_total_s"] / 3600
            m["sleep_deep_pct"] = 100 * (m.get("sleep_deep_s") or 0) / m["sleep_total_s"]
        m["_tags"] = set()
        m["_moods"], m["_energies"] = [], []
    for e in entries(limit=100000):
        d = journal_day(e["ts"], zone).isoformat()
        m = rows.setdefault(d, {"date": d, "_tags": set(), "_moods": [], "_energies": []})
        m["_tags"].update(e["tags"])
        if e["mood"]:
            m["_moods"].append(e["mood"])
        if e["energy"]:
            m["_energies"].append(e["energy"])
    for m in rows.values():
        m["mood"] = sum(m["_moods"]) / len(m["_moods"]) if m["_moods"] else None
        m["self_energy"] = sum(m["_energies"]) / len(m["_energies"]) if m["_energies"] else None
    return rows


def _fmt(v, unit):
    if unit == "h":
        return f"{v:.1f} h"
    if unit in ("%",):
        return f"{v:.0f}%"
    if unit == "/5":
        return f"{v:.1f}/5"
    if unit:
        return f"{v:.0f} {unit}"
    return f"{v:,.0f}"


def analyze(min_n: int = 3) -> dict:
    rows = daily_table()
    dates = sorted(rows)
    journal_days = [d for d in dates if rows[d]["_tags"] or rows[d]["mood"] is not None]
    if not journal_days:
        return {"tag_effects": [], "mood_drivers": [], "journal_days": 0, "tags": []}
    first_journal = journal_days[0]
    days = [d for d in dates if d >= first_journal]  # compare only within the journaling period

    def shifted(d, off):
        return (dt.date.fromisoformat(d) + dt.timedelta(days=off)).isoformat()

    all_tags = sorted({t for d in days for t in rows[d]["_tags"]})
    findings, per_tag = [], []
    for tag in all_tags:
        tagged = [d for d in days if tag in rows[d]["_tags"]]
        if len(tagged) < min_n:
            per_tag.append({"tag": tag, "n": len(tagged), "effects": []})
            continue
        effs = []
        for key, label, off, higher_better, unit in EFFECTS:
            x, y = [], []
            for d in days:
                v = rows.get(shifted(d, off), {}).get(key)
                if v is None:
                    continue
                (x if tag in rows[d]["_tags"] else y).append(float(v))
            if len(x) < min_n or len(y) < min_n:
                continue
            diff, dsz, p = welch(x, y)
            my = sum(y) / len(y)
            pct = 100 * diff / my if my else None
            good = (diff > 0) == higher_better
            e = {
                "tag": tag, "metric": key, "label": label, "offset": off, "unit": unit,
                "with": round(sum(x) / len(x), 2), "without": round(my, 2),
                "diff": round(diff, 2), "pct": round(pct, 1) if pct is not None else None,
                "d": round(dsz, 2), "p": round(p, 4), "n_with": len(x), "n_without": len(y),
                "good": good,
                "strength": "strong" if p < 0.05 and abs(dsz) >= 0.5 else
                            "likely" if p < 0.1 else "possible" if p < 0.25 and abs(dsz) >= 0.3 else "none",
            }
            direction = "higher" if diff > 0 else "lower"
            e["sentence"] = (
                f"{label} was {abs(e['pct'] or 0):.0f}% {direction} on '{tag}' days "
                f"({_fmt(e['with'], unit)} vs {_fmt(e['without'], unit)}, n={len(x)})."
            )
            effs.append(e)
            if e["strength"] != "none":
                findings.append(e)
        per_tag.append({"tag": tag, "n": len(tagged), "effects": effs})
    findings.sort(key=lambda e: (e["p"], -abs(e["d"])))

    drivers = []
    for target, tlabel in (("mood", "Mood"), ("self_energy", "Felt energy")):
        for key, label in MOOD_DRIVERS:
            pairs = [(rows[d][key], rows[d][target]) for d in days
                     if rows[d].get(key) is not None and rows[d].get(target) is not None]
            if len(pairs) < 8:
                continue
            r, p = spearman([a for a, _ in pairs], [b for _, b in pairs])
            drivers.append({"target": target, "target_label": tlabel, "metric": key, "label": label,
                            "r": round(r, 2), "p": round(p, 4), "n": len(pairs)})
    drivers.sort(key=lambda x: -abs(x["r"]))

    tag_counts = sorted(((t, sum(1 for d in days if t in rows[d]["_tags"])) for t in all_tags),
                        key=lambda x: -x[1])
    return {
        "journal_days": len(journal_days),
        "period_days": len(days),
        "findings": findings[:25],
        "tag_effects": per_tag,
        "mood_drivers": drivers,
        "tags": [{"tag": t, "n": n} for t, n in tag_counts],
        "mood_series": [{"date": d, "mood": rows[d]["mood"], "self_energy": rows[d]["self_energy"],
                         "readiness": rows[d].get("readiness"), "sleep_score": rows[d].get("sleep_score"),
                         "tags": sorted(rows[d]["_tags"])} for d in days],
    }
