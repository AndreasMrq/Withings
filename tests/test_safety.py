"""Safety fixes: demo data loss, stale sleep stages, CSV/insights edge cases."""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, main, sync, withings
from tests.test_sync import mock_api  # noqa: F401  (fixture reuse)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_connection(tmp_path / "t.db")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(withings, "connected", lambda: False)
    return TestClient(main.app)


def test_clear_demo_requires_demo_mode(client):
    with db.tx() as c:
        c.execute("INSERT INTO journal(ts,note) VALUES (1,'mine')")
    assert client.delete("/api/demo").status_code == 400
    assert db.q1("SELECT COUNT(*) FROM journal")[0] == 1


def test_load_demo_refuses_real_data(client):
    with db.tx() as c:
        c.execute("INSERT INTO journal(ts,note) VALUES (1,'mine')")
    assert client.post("/api/demo?days=5").status_code == 400
    assert db.q1("SELECT COUNT(*) FROM journal")[0] == 1


def test_load_demo_fresh_and_regenerate_and_clear(client):
    assert client.post("/api/demo?days=20").status_code == 200
    assert db.kv_get("demo")
    assert client.post("/api/demo?days=20").status_code == 200
    assert client.delete("/api/demo").status_code == 200
    assert db.q1("SELECT COUNT(*) FROM journal")[0] == 0


def test_resync_removes_stale_sleep_states(mock_api):  # noqa: F811
    sync.run_sync(full=True)
    s0, e0 = db.q1("SELECT start, end FROM sleep_sessions")
    with db.tx() as c:
        c.execute("INSERT INTO sleep_states(start,end,state) VALUES (?,?,2)", (s0 + 7, s0 + 100, ))
        c.execute("INSERT INTO sleep_series(ts,rr) VALUES (?,9)", (s0 + 7,))
        c.execute("INSERT INTO sleep_states(start,end,state) VALUES (?,?,2)", (e0 + 5, e0 + 50))
    hr_before = db.q1("SELECT COUNT(*) FROM hr")[0]
    sync.run_sync(full=True)
    assert db.q1("SELECT COUNT(*) FROM sleep_states WHERE start=?", (s0 + 7,))[0] == 0
    assert db.q1("SELECT COUNT(*) FROM sleep_series WHERE ts=?", (s0 + 7,))[0] == 0
    assert db.q1("SELECT COUNT(*) FROM sleep_states WHERE start=?", (s0,))[0] == 1
    assert db.q1("SELECT COUNT(*) FROM sleep_states WHERE start=?", (e0 + 5,))[0] == 1  # outside range
    assert db.q1("SELECT COUNT(*) FROM hr")[0] >= hr_before


def test_insights_min_n_one(client):
    assert client.get("/api/insights?min_n=1").status_code == 200


def test_export_daily_empty(client):
    r = client.get("/api/export/daily.csv")
    assert r.status_code == 200 and r.text.strip() == "date"


def test_journal_update_keeps_ts(client):
    with db.tx() as c:
        c.execute("INSERT INTO journal(ts,note) VALUES (12345,'a')")
    eid = db.q1("SELECT id FROM journal")[0]
    r = client.put(f"/api/journal/{eid}", json={"note": "b"})
    assert r.status_code == 200 and r.json()["ts"] == 12345 and r.json()["note"] == "b"
    assert client.put("/api/journal/999", json={"note": "x"}).status_code == 404
