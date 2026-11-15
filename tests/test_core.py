import datetime as dt
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TZ", "Europe/Berlin")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, insights, metrics  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def fresh_db(tmp_path_factory):
    db.reset_connection(tmp_path_factory.mktemp("d") / "t.db")
    yield


def test_sleep_score_bounds():
    good, _ = metrics.sleep_score(8 * 3600, 1.6 * 3600, 1.8 * 3600, 0.95, 0, 300)
    bad, _ = metrics.sleep_score(4.5 * 3600, 0.2 * 3600, 0.4 * 3600, 0.72, 6, 3600)
    assert good >= 90 and bad <= 35
    assert metrics.sleep_score(0, 0, 0, None, 0, 0)[0] is None


def test_stress_and_zones():
    assert metrics.stress_value(55, 55, 185) == 0
    assert metrics.stress_value(120, 55, 185) == 100
    assert metrics.stress_bucket(10) == "rest" and metrics.stress_bucket(80) == "high"
    assert metrics.hr_zone(90, 190) == 0 and metrics.hr_zone(175, 190) == 5


def test_trimp_monotonic():
    easy = metrics.trimp([(120, 60)], 55, 185)
    hard = metrics.trimp([(165, 60)], 55, 185)
    assert 0 < easy < hard
    assert metrics.trimp([(80, 600)], 55, 185) == 0  # resting HR never counts


def test_readiness_and_status():
    hi, _ = metrics.readiness_score(90, 1.05, -2, 0, 1.0, 88)
    lo, _ = metrics.readiness_score(45, 0.7, 7, 40, 1.6, 50)
    assert hi > 80 and lo < 40
    assert metrics.hrv_status(50, [50] * 10)[0] == "Building baseline"
    base = [50 + (i % 5) for i in range(28)]
    assert metrics.hrv_status(52, base)[0] == "Balanced"
    assert metrics.hrv_status(35, base)[0] == "Low"
    assert metrics.load_status(600, 300, 300, 80) == "Overreaching"


def test_stats():
    _, d, p = insights.welch([10, 11, 12, 13, 11, 12], [20, 21, 19, 22, 20, 21])
    assert p < 0.001 and d < -3
    _, _, p2 = insights.welch([1, 2, 3, 4], [1.5, 2.5, 3.5, 2])
    assert p2 > 0.3
    r, p = insights.spearman([1, 2, 3, 4, 5, 6, 7, 8], [2, 4, 5, 7, 9, 10, 12, 15])
    assert r > 0.99 and p < 0.001
    assert abs(insights.t_pvalue(2.0, 30) - 0.0546) < 0.002


def test_demo_end_to_end_finds_planted_patterns():
    from app import demo
    demo.generate(days=120)
    days = metrics.daily()
    assert len(days) >= 120
    last = days[-2]
    for k in ("readiness", "sleep_score", "hrv", "rhr", "energy_wake", "load_acute", "vo2max"):
        assert last.get(k) is not None, k
    res = insights.analyze()
    alc = [f for f in res["findings"] if f["tag"] == "alcohol" and f["metric"] == "hrv" and f["offset"] == 1]
    assert alc and not alc[0]["good"] and alc[0]["pct"] < -10


def test_api_journal_roundtrip():
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/status").status_code == 200
    r = c.post("/api/journal", json={"mood": 2, "tags": ["Headache", "brand new tag"], "note": "x"})
    assert r.status_code == 200 and r.json()["tags"] == ["brand new tag", "headache"]
    eid = r.json()["id"]
    assert any(t["name"] == "brand new tag" for t in c.get("/api/tags").json())
    assert c.put(f"/api/journal/{eid}", json={"mood": 4, "tags": []}).json()["mood"] == 4
    assert c.post("/api/journal", json={}).status_code == 400
    assert c.delete(f"/api/journal/{eid}").status_code == 200
    today = dt.date.today().isoformat()
    assert c.get(f"/api/day/{today}").status_code == 200
    assert c.get("/api/insights").status_code == 200
    assert c.get("/api/export/daily.csv").text.startswith("date,")
