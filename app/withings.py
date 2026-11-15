"""Minimal Withings Public API client (OAuth2 + the endpoints we need).

Docs: https://developer.withings.com/api-reference
"""
import time
import urllib.parse

import httpx

from . import config, db

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
API = "https://wbsapi.withings.net"
SCOPES = "user.info,user.metrics,user.activity"


class WithingsError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.WITHINGS_CLIENT_ID and config.WITHINGS_CLIENT_SECRET)


def authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.WITHINGS_CLIENT_ID,
        "scope": SCOPES,
        "redirect_uri": config.WITHINGS_REDIRECT_URI,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post(path: str, data: dict, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(4):
        r = httpx.post(f"{API}{path}", data=data, headers=headers, timeout=60)
        r.raise_for_status()
        js = r.json()
        status = js.get("status")
        if status == 0:
            return js.get("body", {})
        if status == 601 and attempt < 3:  # rate limited
            time.sleep(10 * (attempt + 1))
            continue
        raise WithingsError(f"Withings {path} {data.get('action')}: status {status} {js.get('error', '')}")
    raise WithingsError("unreachable")


def _store_tokens(body: dict) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO tokens(id, access_token, refresh_token, expires_at, userid, scope) "
            "VALUES (1,?,?,?,?,?)",
            (
                body["access_token"],
                body["refresh_token"],
                int(time.time()) + int(body.get("expires_in", 10800)) - 120,
                str(body.get("userid", "")),
                body.get("scope", ""),
            ),
        )


def exchange_code(code: str) -> None:
    body = _post("/v2/oauth2", {
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": config.WITHINGS_CLIENT_ID,
        "client_secret": config.WITHINGS_CLIENT_SECRET,
        "code": code,
        "redirect_uri": config.WITHINGS_REDIRECT_URI,
    })
    _store_tokens(body)


def connected() -> bool:
    return db.q1("SELECT 1 FROM tokens WHERE id=1") is not None


def disconnect() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM tokens")


def access_token() -> str:
    row = db.q1("SELECT * FROM tokens WHERE id=1")
    if row is None:
        raise WithingsError("Not connected to Withings")
    if row["expires_at"] > time.time():
        return row["access_token"]
    # Refresh tokens rotate: always persist the new one.
    body = _post("/v2/oauth2", {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": config.WITHINGS_CLIENT_ID,
        "client_secret": config.WITHINGS_CLIENT_SECRET,
        "refresh_token": row["refresh_token"],
    })
    _store_tokens(body)
    return body["access_token"]


def call(path: str, action: str, **params) -> dict:
    data = {"action": action, **{k: v for k, v in params.items() if v is not None}}
    body = _post(path, data, access_token())
    time.sleep(0.55)  # stay well below the 120 req/min limit
    return body


def paged(path: str, action: str, list_key: str, **params) -> list:
    out, offset = [], None
    while True:
        body = call(path, action, offset=offset, **params)
        out.extend(body.get(list_key, []))
        if not body.get("more"):
            return out
        offset = body.get("offset")


def subscribe_notifications() -> list[str]:
    """Ask Withings to POST to us whenever new data arrives (requires public PUBLIC_URL)."""
    cb = f"{config.PUBLIC_URL}/api/withings/notify"
    done = []
    for appli in (1, 4, 16, 44):  # weight, bp/temp/spo2, activity, sleep
        try:
            call("/notify", "subscribe", callbackurl=cb, appli=appli)
            done.append(str(appli))
        except WithingsError as e:
            if "294" not in str(e):  # 294 = already subscribed
                raise
    return done
