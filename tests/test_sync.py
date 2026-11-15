"""Sync parsing against mocked Withings responses shaped like the public API docs."""
import time

import pytest

from app import db, metrics, sync, withings


@pytest.fixture()
def mock_api(monkeypatch, tmp_path):
    db.reset_connection(tmp_path / "s.db")
    now = int(time.time())
    night_start, night_end = now - 10 * 3600, now - 2 * 3600

    def fake_call(path, action, **p):
        if action == "getmeas":
            return {"measuregrps": [{"grpid": 1, "date": now - 3600, "category": 1,
                                     "measures": [{"value": 78350, "type": 1, "unit": -3},
                                                  {"value": 482, "type": 123, "unit": -1}]}], "more": 0}
        if action == "getactivity":
            return {"activities": [{"date": time.strftime("%Y-%m-%d"), "steps": 8123, "distance": 6000,
                                    "moderate": 1800, "intense": 600, "totalcalories": 2300}], "more": False}
        if action == "getworkouts":
            return {"series": [{"id": 9, "category": 2, "startdate": now - 5000, "enddate": now - 2000,
                                "date": time.strftime("%Y-%m-%d"), "data": {"hr_average": 150, "calories": 400}}],
                    "more": False}
        if action == "getsummary":
            return {"series": [{"id": 5, "timezone": "Europe/Berlin", "date": time.strftime("%Y-%m-%d"),
                                "startdate": night_start, "enddate": night_end,
                                "data": {"total_sleep_time": 7 * 3600, "deepsleepduration": 5400,
                                         "remsleepduration": 6000, "lightsleepduration": 13800,
                                         "wakeupcount": 2, "sleep_score": 81, "rr_average": 15}}], "more": False}
        if action == "get":
            hr = {str(t): 52 + (t // 60) % 5 for t in range(night_start, night_end, 60)}
            rmssd = {str(t): 45 for t in range(night_start, night_end, 300)}
            return {"series": [{"startdate": night_start, "enddate": night_end, "state": 1,
                                "hr": hr, "rmssd": rmssd, "rr": {str(night_start): 14}}]}
        if action == "getintradayactivity":
            s = p["startdate"]
            return {"series": {str(t): {"heart_rate": 70, "steps": 30, "duration": 60}
                               for t in range(max(s, night_end), p["enddate"], 600)}}
        raise AssertionError(action)

    monkeypatch.setattr(withings, "call", fake_call)
    monkeypatch.setattr(withings, "access_token", lambda: "x")
    return now


def test_full_sync(mock_api):
    res = sync.run_sync(full=True)
    assert res["ok"], res
    assert db.q1("SELECT value FROM measures WHERE type=1")[0] == pytest.approx(78.35)
    assert db.q1("SELECT COUNT(*) FROM hr WHERE src='sleep'")[0] > 400
    today = metrics.daily()[-1]
    assert today["vo2max"] == pytest.approx(48.2) and today["vo2max_source"] == "withings"
    assert today["steps"] == 8123
    assert today["hrv"] == 45
    assert today["sleep_score_withings"] == 81 and today["sleep_score"] is not None
    assert 50 <= today["rhr"] <= 56
