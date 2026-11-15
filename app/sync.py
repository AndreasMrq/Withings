"""Pull raw data from Withings into the local database, then recompute derived metrics."""
import datetime as dt
import json
import logging
import threading
import time

from . import config, db, metrics, withings

log = logging.getLogger("sync")
_lock = threading.Lock()

# Body / vitals measure types (value = value * 10^unit)
MEAS_TYPES = [1, 5, 6, 8, 9, 10, 11, 12, 54, 71, 73, 76, 77, 88, 91, 123, 130, 135, 136, 137, 138, 155]

SLEEP_SUMMARY_FIELDS = ",".join([
    "breathing_disturbances_intensity", "deepsleepduration", "durationtosleep", "durationtowakeup",
    "hr_average", "hr_max", "hr_min", "lightsleepduration", "remsleepduration", "rr_average",
    "rr_max", "rr_min", "sleep_score", "snoring", "snoringepisodecount", "wakeupcount",
    "wakeupduration", "total_sleep_time", "total_timeinbed", "sleep_efficiency", "sleep_latency",
    "wakeup_latency", "waso", "nb_rem_episodes", "out_of_bed_count", "apnea_hypopnea_index",
])
ACTIVITY_FIELDS = ("steps,distance,elevation,soft,moderate,intense,active,calories,totalcalories,"
                   "hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3")
WORKOUT_FIELDS = ("calories,intensity,manual_distance,manual_calories,hr_average,hr_min,hr_max,"
                  "hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3,pause_duration,algo_pause_duration,"
                  "spo2_average,steps,distance,elevation,pool_laps,strokes,pool_length")
INTRADAY_FIELDS = "steps,elevation,calories,distance,duration,heart_rate,spo2_auto"
SLEEP_SERIES_FIELDS = "hr,rr,snoring,sdnn_1,rmssd,mvt_score"


def _ymd(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def status() -> dict:
    return {
        "running": _lock.locked(),
        "last_sync": db.kv_get("last_sync"),
        "last_error": db.kv_get("last_sync_error"),
    }


def run_sync(full: bool = False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "sync already running"}
    try:
        now = int(time.time())
        last = db.kv_get("last_sync_ts")
        if full or not last:
            start = now - config.HISTORY_DAYS * 86400
        else:
            start = int(last) - 2 * 86400  # overlap: late uploads from the watch
        counts = {
            "measures": sync_measures(start if full or not last else None),
            "activity": sync_activity(start, now),
            "workouts": sync_workouts(start, now),
            "sleep": sync_sleep(start, now),
            "intraday": sync_intraday(start, now),
        }
        metrics.recompute_all()
        db.kv_set("last_sync_ts", now)
        db.kv_set("last_sync", dt.datetime.now().isoformat(timespec="seconds"))
        db.kv_set("last_sync_error", None)
        log.info("sync done %s", counts)
        return {"ok": True, "counts": counts}
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed")
        db.kv_set("last_sync_error", f"{dt.datetime.now().isoformat(timespec='seconds')}: {e}")
        return {"ok": False, "message": str(e)}
    finally:
        _lock.release()


def sync_measures(startdate: int | None) -> int:
    lastupdate = None if startdate else db.kv_get("measures_lastupdate")
    params = {"meastypes": ",".join(map(str, MEAS_TYPES)), "category": 1}
    if lastupdate:
        params["lastupdate"] = lastupdate
    else:
        params["startdate"] = startdate or int(time.time()) - config.HISTORY_DAYS * 86400
        params["enddate"] = int(time.time())
    groups = withings.paged("/measure", "getmeas", "measuregrps", **params)
    rows = [
        (g["grpid"], m["type"], g["date"], m["value"] * 10 ** m["unit"])
        for g in groups for m in g.get("measures", [])
    ]
    with db.tx() as c:
        c.executemany("INSERT OR REPLACE INTO measures(grpid,type,ts,value) VALUES (?,?,?,?)", rows)
    db.kv_set("measures_lastupdate", int(time.time()))
    return len(rows)


def sync_activity(start: int, end: int) -> int:
    acts = withings.paged("/v2/measure", "getactivity", "activities",
                          startdateymd=_ymd(start), enddateymd=_ymd(end), data_fields=ACTIVITY_FIELDS)
    with db.tx() as c:
        c.executemany("INSERT OR REPLACE INTO activity_daily(date,data) VALUES (?,?)",
                      [(a["date"], json.dumps(a)) for a in acts])
    return len(acts)


def sync_workouts(start: int, end: int) -> int:
    ws = withings.paged("/v2/measure", "getworkouts", "series",
                        startdateymd=_ymd(start), enddateymd=_ymd(end), data_fields=WORKOUT_FIELDS)
    with db.tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO workouts(id,category,start,end,date,data) VALUES (?,?,?,?,?,?)",
            [(w["id"], w.get("category"), w["startdate"], w["enddate"], w.get("date"),
              json.dumps(w.get("data", {}))) for w in ws],
        )
    return len(ws)


def _series_items(obj):
    if isinstance(obj, dict):
        return [(int(k), v) for k, v in obj.items()]
    return []


def sync_sleep(start: int, end: int) -> int:
    sessions = withings.paged("/v2/sleep", "getsummary", "series",
                              startdateymd=_ymd(start), enddateymd=_ymd(end),
                              data_fields=SLEEP_SUMMARY_FIELDS)
    with db.tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO sleep_sessions(id,date,start,end,tz,data) VALUES (?,?,?,?,?,?)",
            [(s["id"], s["date"], s["startdate"], s["enddate"], s.get("timezone"),
              json.dumps(s.get("data", {}))) for s in sessions],
        )
    # High-resolution night data (HR, HRV, breathing, stages), max 24h per request.
    for s in sessions:
        body = withings.call("/v2/sleep", "get", startdate=s["startdate"], enddate=s["enddate"],
                             data_fields=SLEEP_SERIES_FIELDS)
        hr_rows, series, states = [], {}, []
        for seg in body.get("series", []):
            states.append((seg["startdate"], seg["enddate"], seg.get("state", 0)))
            hr_rows += [(ts, int(v), "sleep") for ts, v in _series_items(seg.get("hr"))]
            for key, col in (("rr", "rr"), ("snoring", "snoring"), ("rmssd", "rmssd"),
                             ("sdnn_1", "sdnn"), ("mvt_score", "mvt")):
                for ts, v in _series_items(seg.get(key)):
                    series.setdefault(ts, {})[col] = v
        with db.tx() as c:
            if states:  # re-segmented nights: drop stale stages/series (keep them on an empty reply)
                c.execute("DELETE FROM sleep_states WHERE start>=? AND start<?", (s["startdate"], s["enddate"]))
                c.execute("DELETE FROM sleep_series WHERE ts>=? AND ts<?", (s["startdate"], s["enddate"]))
            c.executemany("INSERT OR REPLACE INTO sleep_states(start,end,state) VALUES (?,?,?)", states)
            c.executemany("INSERT OR REPLACE INTO hr(ts,bpm,src) VALUES (?,?,?)", hr_rows)
            c.executemany(
                "INSERT OR REPLACE INTO sleep_series(ts,rr,snoring,rmssd,sdnn,mvt) VALUES (?,?,?,?,?,?)",
                [(ts, d.get("rr"), d.get("snoring"), d.get("rmssd"), d.get("sdnn"), d.get("mvt"))
                 for ts, d in series.items()],
            )
    return len(sessions)


def sync_intraday(start: int, end: int) -> int:
    n = 0
    t = start
    while t < end:
        t2 = min(t + 86400, end)
        body = withings.call("/v2/measure", "getintradayactivity", startdate=t, enddate=t2,
                             data_fields=INTRADAY_FIELDS)
        series = body.get("series", {})
        items = series.items() if isinstance(series, dict) else [(x.get("timestamp"), x) for x in series]
        hr_rows, step_rows, spo2_rows = [], [], []
        for ts, d in items:
            ts = int(ts)
            if d.get("heart_rate"):
                hr_rows.append((ts, int(d["heart_rate"]), "intraday"))
            if any(d.get(k) for k in ("steps", "calories", "distance")):
                step_rows.append((ts, d.get("steps", 0), d.get("duration"), d.get("calories"),
                                  d.get("distance"), d.get("elevation")))
            if d.get("spo2_auto"):
                spo2_rows.append((ts, d["spo2_auto"]))
        with db.tx() as c:
            # sleep HR (higher quality during the night) wins over intraday samples at the same ts
            c.executemany("INSERT OR IGNORE INTO hr(ts,bpm,src) VALUES (?,?,?)", hr_rows)
            c.executemany("INSERT OR REPLACE INTO steps(ts,steps,duration,calories,distance,elevation) "
                          "VALUES (?,?,?,?,?,?)", step_rows)
            c.executemany("INSERT OR REPLACE INTO spo2(ts,value) VALUES (?,?)", spo2_rows)
        n += len(items)
        t = t2
    return n
