"""FastAPI entrypoint: JSON API + static single-page frontend."""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import hmac
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, insights, metrics, sync, withings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).resolve().parent.parent / "static"
COOKIE = "hs_session"


# --------------------------------------------------------------------------------------
# background sync loop
# --------------------------------------------------------------------------------------

async def _sync_loop():
    await asyncio.sleep(15)
    while True:
        if withings.configured() and withings.connected():
            await asyncio.to_thread(sync.run_sync)
        await asyncio.sleep(config.SYNC_INTERVAL_MIN * 60)


@asynccontextmanager
async def lifespan(_app):
    db.conn()
    task = asyncio.create_task(_sync_loop())
    yield
    task.cancel()


app = FastAPI(title="Pulse - self-hosted Withings health", lifespan=lifespan)


# --------------------------------------------------------------------------------------
# auth (single user, password -> signed cookie)
# --------------------------------------------------------------------------------------

def _sign(value: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def _make_cookie() -> str:
    exp = str(int(time.time()) + 90 * 86400)
    return f"{exp}.{_sign(exp)}"


def _valid_cookie(v: str | None) -> bool:
    if not v or "." not in v:
        return False
    exp, sig = v.split(".", 1)
    return hmac.compare_digest(sig, _sign(exp)) and exp.isdigit() and int(exp) > time.time()


PUBLIC_PATHS = ("/login", "/api/login", "/api/withings/notify", "/manifest.webmanifest",
                "/static/icon.svg", "/static/style.css", "/healthz")


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    path = request.url.path
    if config.APP_PASSWORD and not path.startswith(PUBLIC_PATHS):
        if not _valid_cookie(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return RedirectResponse("/login")
    return await call_next(request)


class LoginIn(BaseModel):
    password: str


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    if not config.APP_PASSWORD or not hmac.compare_digest(body.password, config.APP_PASSWORD):
        time.sleep(1)
        raise HTTPException(401, "wrong password")
    response.set_cookie(COOKIE, _make_cookie(), max_age=90 * 86400, httponly=True, samesite="lax",
                        secure=config.PUBLIC_URL.startswith("https"))
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Withings connection
# --------------------------------------------------------------------------------------

@app.get("/api/status")
def status():
    first = db.q1("SELECT MIN(date), MAX(date), COUNT(*) FROM daily_metrics")
    return {
        "withings_configured": withings.configured(),
        "withings_connected": withings.connected(),
        "redirect_uri": config.WITHINGS_REDIRECT_URI,
        "notify_enabled": config.WITHINGS_NOTIFY,
        "sync": sync.status(),
        "data": {"first": first[0], "last": first[1], "days": first[2]},
        "demo": bool(db.kv_get("demo")),
        "auth": bool(config.APP_PASSWORD),
        "today": dt.datetime.now(metrics.tz()).date().isoformat(),
        "hrmax": db.kv_get("metrics_hrmax"),
    }


@app.get("/api/withings/connect")
def withings_connect():
    if not withings.configured():
        raise HTTPException(400, "Set WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET first")
    state = secrets.token_urlsafe(16)
    db.kv_set("oauth_state", state)
    return RedirectResponse(withings.authorize_url(state))


@app.get("/api/withings/callback")
def withings_callback(background: BackgroundTasks, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(f"/#/settings?error={error or 'no_code'}")
    if state != db.kv_get("oauth_state"):
        raise HTTPException(400, "invalid OAuth state")
    withings.exchange_code(code)
    if db.kv_get("demo"):  # switching from demo data to real data
        _wipe_sensor_data()
        db.kv_set("demo", False)
    if config.WITHINGS_NOTIFY:
        try:
            withings.subscribe_notifications()
        except Exception as e:  # noqa: BLE001
            log.warning("notify subscribe failed: %s", e)
    background.add_task(sync.run_sync, True)
    return RedirectResponse("/#/settings?connected=1")


@app.post("/api/withings/disconnect")
def withings_disconnect():
    withings.disconnect()
    return {"ok": True}


@app.api_route("/api/withings/notify", methods=["GET", "HEAD", "POST"])
async def withings_notify(request: Request, background: BackgroundTasks):
    # Withings calls this when the watch synced new data. Respond fast, sync in background.
    if request.method == "POST" and withings.connected():
        background.add_task(sync.run_sync)
    return Response(status_code=200)


@app.post("/api/sync")
def sync_now(background: BackgroundTasks, full: bool = False):
    if not withings.connected():
        raise HTTPException(400, "Not connected to Withings")
    background.add_task(sync.run_sync, full)
    return {"ok": True, "started": True}


@app.post("/api/recompute")
def recompute():
    return {"days": metrics.recompute_all()}


def _wipe_sensor_data():
    with db.tx() as c:
        for t in ("hr", "steps", "spo2", "sleep_series", "sleep_states", "sleep_sessions",
                  "activity_daily", "workouts", "measures", "daily_metrics", "intraday_derived"):
            c.execute(f"DELETE FROM {t}")
    db.kv_set("last_sync_ts", None)


@app.post("/api/demo")
def load_demo(days: int = 150):
    if withings.connected():
        raise HTTPException(400, "Disconnect Withings before loading demo data")
    from . import demo
    demo.generate(days=days)
    return {"ok": True}


@app.delete("/api/demo")
def clear_demo():
    _wipe_sensor_data()
    with db.tx() as c:
        c.execute("DELETE FROM journal")
    db.kv_set("demo", False)
    return {"ok": True}


# --------------------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------------------

class ProfileIn(BaseModel):
    birth_year: int = Field(1990, ge=1900, le=2030)
    sex: str = "m"
    max_hr: int | None = Field(None, ge=100, le=240)
    sleep_target_h: float = Field(8.0, ge=4, le=12)
    tz: str | None = None
    step_goal: int = Field(10000, ge=0, le=100000)


@app.get("/api/profile")
def get_profile():
    return metrics.profile() | {"tz_effective": str(metrics.tz())}


@app.put("/api/profile")
def put_profile(p: ProfileIn):
    if p.tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(p.tz)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"unknown timezone {p.tz}") from e
    db.kv_set("profile", p.model_dump())
    metrics.recompute_all()
    return get_profile()


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------

@app.get("/api/day/{date}")
def day(date: str):
    try:
        dt.date.fromisoformat(date)
    except ValueError as e:
        raise HTTPException(400, "bad date") from e
    out = metrics.day_detail(date)
    zone = metrics.tz()
    d = dt.date.fromisoformat(date)
    a = int(dt.datetime.combine(d, dt.time(insights.DAY_ROLLOVER_H), zone).timestamp())
    out["journal"] = insights.entries(a, a + 86400)
    return out


@app.get("/api/trends")
def trends(days: int = 90, end: str | None = None):
    e = dt.date.fromisoformat(end) if end else dt.datetime.now(metrics.tz()).date()
    s = e - dt.timedelta(days=days - 1)
    return metrics.daily(s.isoformat(), e.isoformat())


@app.get("/api/workouts")
def workouts(days: int = 60):
    now = int(time.time())
    return metrics.workouts(now - days * 86400, now + 86400)


@app.get("/api/measures/{mtype}")
def measures(mtype: int, days: int = 365):
    since = int(time.time()) - days * 86400
    return [[r[0], r[1]] for r in db.q(
        "SELECT ts, value FROM measures WHERE type=? AND ts>=? ORDER BY ts", (mtype, since))]


# --------------------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------------------

class EntryIn(BaseModel):
    ts: int | None = None
    mood: int | None = Field(None, ge=1, le=5)
    energy: int | None = Field(None, ge=1, le=5)
    tags: list[str] = []
    note: str = Field("", max_length=5000)


@app.get("/api/journal")
def journal_list(days: int = 365):
    return insights.entries(int(time.time()) - days * 86400)


@app.post("/api/journal")
def journal_add(e: EntryIn):
    if e.mood is None and e.energy is None and not e.tags and not e.note.strip():
        raise HTTPException(400, "empty entry")
    return insights.add_entry(e.ts or int(time.time()), e.mood, e.energy, e.tags, e.note)


@app.put("/api/journal/{eid}")
def journal_update(eid: int, e: EntryIn):
    r = insights.update_entry(eid, e.ts or int(time.time()), e.mood, e.energy, e.tags, e.note)
    if not r:
        raise HTTPException(404)
    return r


@app.delete("/api/journal/{eid}")
def journal_delete(eid: int):
    insights.delete_entry(eid)
    return {"ok": True}


class TagIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    emoji: str = Field("", max_length=8)


@app.get("/api/tags")
def tags_list():
    return insights.tags()


@app.post("/api/tags")
def tags_add(t: TagIn):
    insights.set_tag(t.name, t.emoji)
    return insights.tags()


@app.delete("/api/tags/{name}")
def tags_delete(name: str):
    insights.delete_tag(name)
    return insights.tags()


@app.get("/api/insights")
def get_insights(min_n: int = 3):
    return insights.analyze(min_n=min_n)


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------

@app.get("/api/export/daily.csv")
def export_daily():
    rows = metrics.daily()
    keys = sorted({k for r in rows for k, v in r.items() if not isinstance(v, (dict, list))})
    keys.remove("date")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date"] + keys)
    for r in rows:
        w.writerow([r["date"]] + [r.get(k) for k in keys])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=daily_metrics.csv"})


@app.get("/api/export/journal.csv")
def export_journal():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["datetime", "mood", "energy", "tags", "note"])
    zone = metrics.tz()
    for e in reversed(insights.entries(limit=1000000)):
        w.writerow([dt.datetime.fromtimestamp(e["ts"], zone).isoformat(timespec="minutes"),
                    e["mood"], e["energy"], ";".join(e["tags"]), e["note"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=journal.csv"})


# --------------------------------------------------------------------------------------
# frontend
# --------------------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return Response((STATIC / "manifest.webmanifest").read_text(), media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/", include_in_schema=False)
@app.get("/log", include_in_schema=False)
def index():
    return HTMLResponse((STATIC / "index.html").read_text())
