import datetime as dt
import json
import os
import random
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import db, insights, metrics  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.reset_connection(tmp_path / "t.db")
    yield


START = dt.date(2025, 1, 1)


def _ds(i):
    return (START + dt.timedelta(days=i)).isoformat()


def _metric(i, **kw):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO daily_metrics(date,data) VALUES (?,?)",
                  (_ds(i), json.dumps({"date": _ds(i), **kw})))


def _entry(i, tags=(), mood=None, note=""):
    ts = int(dt.datetime.combine(START + dt.timedelta(days=i), dt.time(12), metrics.tz()).timestamp())
    insights.add_entry(ts, mood, None, list(tags), note)


def test_bh_adjust_hand_computed():
    q = insights.bh_adjust([0.01, 0.04, 0.03, 0.20])
    assert q == pytest.approx([0.04, 0.16 / 3, 0.16 / 3, 0.20])
    assert round(q[1], 4) == 0.0533
    assert insights.bh_adjust([]) == []
    # order preserved, monotone in p, capped at 1
    ps = [0.9, 0.001, 0.5, 0.04]
    qs = insights.bh_adjust(ps)
    assert qs[1] == min(qs) and all(v <= 1 for v in qs)
    assert [qs[i] for i in sorted(range(4), key=lambda i: ps[i])] == sorted(qs)


def test_welch_ci_contains_true_diff_and_symmetric():
    rng = random.Random(1)
    x = [10 + rng.gauss(0, 1) for _ in range(40)]
    y = [7 + rng.gauss(0, 1) for _ in range(40)]
    diff = sum(x) / 40 - sum(y) / 40
    lo, hi = insights.welch_ci(x, y)
    assert lo < 3 < hi and lo > 0
    assert (lo + hi) / 2 == pytest.approx(diff)
    assert insights.welch(x, y)[0] == pytest.approx(diff)  # welch signature unchanged (3-tuple)
    assert insights._t_crit(30) == pytest.approx(2.042, abs=0.002)


def test_unlogged_days_excluded_from_without_group():
    for i in range(30):
        _metric(i, sleep_score=70.0 + (i % 3))
    for i in range(0, 20, 2):  # logged days 0..18, even = tagged, odd = untagged
        _entry(i, ["alcohol"], mood=3)
    for i in range(1, 20, 2):
        _entry(i, [], note="just a note")  # note-only entry still counts as logged
    # days 20..29 have no entry at all
    res = insights.analyze()
    assert res["journal_days"] == 20
    eff = next(e for t in res["tag_effects"] if t["tag"] == "alcohol"
               for e in t["effects"] if e["metric"] == "sleep_score" and e["offset"] == 0)
    assert eff["n_with"] == 10 and eff["n_without"] == 10
    assert {"q", "ci_low", "ci_high", "p"} <= set(eff)



def test_min_n_clamped():
    for i in range(6):
        _metric(i, sleep_score=60.0 + i)
        _entry(i, ["a"] if i < 3 else [], mood=3)
    for n in (0, 1, -5):
        insights.analyze(min_n=n)  # must not raise ZeroDivisionError


def test_pct_none_sentence():
    for i in range(24):
        _entry(i, ["x"] if i % 2 else [], mood=3)
    # baseline (untagged) mean of steps is 0 -> pct None; give tagged days steps > 0
    for i in range(24):
        _metric(i, sleep_score=60.0, steps=500.0 + i if i % 2 else 0.0)
    res = insights.analyze()
    eff = next(e for t in res["tag_effects"] for e in t["effects"] if e["metric"] == "steps")
    assert eff["pct"] is None and "0%" not in eff["sentence"] and "higher" in eff["sentence"]


def test_noise_has_no_strong_but_planted_effect_found():
    rng = random.Random(7)
    n = 120
    tags = [f"t{k}" for k in range(15)]
    for i in range(n + 1):
        _metric(i, sleep_score=70 + rng.gauss(0, 5), hrv=50 + rng.gauss(0, 8),
                rhr=60 + rng.gauss(0, 3), steps=8000 + rng.gauss(0, 2000))
    planted = set()
    for i in range(n):
        day_tags = [t for t in tags if rng.random() < 0.3]
        if rng.random() < 0.3:
            day_tags.append("planted")
            planted.add(i)
        _entry(i, day_tags, mood=rng.randint(1, 5))
    noise = insights.analyze()
    assert not [f for f in noise["findings"] if f["strength"] == "strong" and f["tag"] != "planted"]
    # now plant a big effect: next-night HRV drops after 'planted' days
    for i in range(n):
        if i in planted:
            _metric(i + 1, sleep_score=70 + rng.gauss(0, 5), hrv=35 + rng.gauss(0, 8),
                    rhr=60 + rng.gauss(0, 3), steps=8000 + rng.gauss(0, 2000))
    res = insights.analyze()
    hit = [f for f in res["findings"] if f["tag"] == "planted" and f["metric"] == "hrv" and f["offset"] == 1]
    assert hit and hit[0]["strength"] == "strong" and hit[0]["q"] < 0.05 and hit[0]["diff"] < -8
    assert hit[0]["ci_low"] < hit[0]["diff"] < hit[0]["ci_high"]
    assert [(f["q"], -abs(f["d"])) for f in res["findings"]] == sorted((f["q"], -abs(f["d"])) for f in res["findings"])
    assert not [f for f in res["findings"] if f["strength"] == "strong" and f["tag"] != "planted"]
