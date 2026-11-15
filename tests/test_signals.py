import datetime as dt
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TZ", "Europe/Berlin")

from zoneinfo import ZoneInfo  # noqa: E402

from app import db, metrics  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def fresh_db(tmp_path_factory):
    db.reset_connection(tmp_path_factory.mktemp("d") / "t.db")
    yield


def test_robust_z():
    base = [60, 61, 59, 60, 62, 58, 60, 61, 59, 60]
    z, med = metrics.robust_z(66, base, 1.5)
    assert med == 60 and z > 3
    assert metrics.robust_z(66, base[:9], 1.5) is None  # too little baseline
    z2, _ = metrics.robust_z(61, [60] * 12, 1.5)  # constant baseline: floor keeps z small
    assert abs(z2 - 1 / 1.5) < 1e-9


def test_strain_assess():
    assert metrics.strain_assess({"rhr": 3.0}) == (None, None)
    assert metrics.strain_assess({"rhr": 3.0, "hrv": None})[1] is None
    assert metrics.strain_assess({"rhr": 0.2, "hrv": -1.0, "respiration": 0.5})[1] == "normal"
    assert metrics.strain_assess({"rhr": 1.6, "hrv": 1.5, "respiration": 0.0})[1] == "watch"
    assert metrics.strain_assess({"rhr": 2.5, "hrv": 2.6})[1] == "alert"
    assert metrics.strain_assess({"rhr": 1.5, "hrv": 1.6, "respiration": 1.5})[1] == "alert"
    assert metrics.strain_assess({"rhr": 2.5, "hrv": 2.5})[0] == 100
    assert metrics.strain_assess({"rhr": 5, "hrv": 5})[0] == 100  # clamped
    assert metrics.strain_assess({"rhr": -4, "hrv": -4})[0] == 0
    assert metrics.strain_assess({"rhr": 1.25, "hrv": 0.0})[0] == 25


def test_circular_sd_wraparound():
    assert metrics.circ_sd_min([23 * 60 + 30, 30]) == pytest.approx(30, abs=0.5)  # 23:30 and 00:30 are 60 apart
    assert metrics.circ_sd_min([1380, 1380, 1380]) == pytest.approx(0, abs=1e-6)
    z = ZoneInfo("UTC")
    t = int(dt.datetime(2024, 1, 3, 23, 30, tzinfo=z).timestamp())
    assert metrics._clock_min(t, z) == 1410 and metrics._clock_min(t, z, 720) == 690


def test_regularity_and_debt():
    assert metrics.sleep_regularity(0, 0) == 100
    assert metrics.sleep_regularity(15, 15) == 100
    assert metrics.sleep_regularity(120, 120) == 0
    assert metrics.sleep_regularity(60, 30) == round(100 * 75 / 105)
    assert metrics.sleep_regularity(None, 10) is None
    assert metrics.social_jetlag([10, 20], [0]) is None
    assert metrics.social_jetlag([100, 110], [10, 20]) == 90
    # shortfalls only: surplus nights do not repay debt
    assert metrics.sleep_debt([6, 9, 7.5, 8], 8) == 2.5
    assert metrics.sleep_debt([6, None, 7, 5], 8) is None  # fewer than 4 nights of data
    assert metrics.sleep_debt([7, 7, 7, 7, None, None, None], 8) == 4.0


def _day_with_hard_workout(extra):
    """Recompute demo data, optionally adding a hard evening workout on day D; return (rows for D and D+1)."""
    from app import demo
    demo.generate(days=60)
    days = metrics.daily()
    D = dt.date.fromisoformat(days[-8]["date"])
    base = metrics.daily(D.isoformat(), (D + dt.timedelta(days=1)).isoformat())
    if extra:
        t0 = int(dt.datetime.combine(D, dt.time(18, 0), metrics.tz()).timestamp())
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO hr(ts,bpm,src) VALUES (?,?,?)",
                          [(t0 + 60 * i, 175, "test") for i in range(90)])
        metrics.recompute_all()
        base = metrics.daily(D.isoformat(), (D + dt.timedelta(days=1)).isoformat())
    return base


def test_readiness_ignores_same_day_training():
    a = _day_with_hard_workout(False)
    b = _day_with_hard_workout(True)  # regenerates the same demo data, then adds the workout
    assert b[0]["training_load"] > a[0]["training_load"] + 100
    assert b[0]["load_acute"] > a[0]["load_acute"]  # end-of-day fields do include today's load
    assert b[0]["readiness"] == a[0]["readiness"]
    assert b[1]["readiness"] != a[1]["readiness"]


def test_demo_new_fields_plausible():
    from app import demo
    demo.generate(days=60)
    days = metrics.daily()[:-1]
    lv = [d["strain_level"] for d in days if d.get("strain_level")]
    assert lv and lv.count("normal") / len(lv) > 0.6
    assert all(0 <= d["strain_score"] <= 100 for d in days if d["strain_score"] is not None)
    reg = [d["sleep_regularity"] for d in days if d["sleep_regularity"] is not None]
    assert reg and all(0 <= r <= 100 for r in reg)
    assert any(d["sleep_debt_h"] is not None for d in days)
