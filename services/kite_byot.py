"""
BYOT - "bring your own Kite token".

A member logs in with THEIR OWN Zerodha Kite Connect app each day. The browser
computes the checksum (SHA-256 of api_key + request_token + api_secret), so the
api_secret never reaches this server. We forward the checksum to Zerodha to
confirm the login is real, then keep ONLY: email, api_key, kite_user_id,
kite_user_name and today's date. The access token Zerodha returns is discarded
immediately - it is never stored or logged.
"""
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from services.access_gate import _verify_token, _is_member

router = APIRouter()

KITE_SESSION_URL = "https://api.kite.trade/session/token"
_API_KEY_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_CHECKSUM_RE = re.compile(r"^[a-f0-9]{64}$")

# very small in-memory brake: max 8 connect attempts per email per 10 minutes
_attempts = {}


def _today_ist() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


def _too_many(email: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(email, []) if now - t < 600]
    recent.append(now)
    _attempts[email] = recent
    return len(recent) > 8


async def _who(request: Request):
    """Return (email, error_response). Requires a valid member sign-in token."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return None, JSONResponse({"error": "Sign in required", "code": "user_no_token"}, status_code=401)
    email, err = await run_in_threadpool(_verify_token, token)
    if err or not email:
        return None, JSONResponse({"error": "Sign in required", "code": "user_bad_token"}, status_code=401)
    email = email.lower().strip()
    if not await run_in_threadpool(_is_member, email):
        return None, JSONResponse({"error": "This email does not have GreekNova access", "code": "user_not_member"}, status_code=403)
    return email, None


@router.post("/kite/connect")
async def kite_connect(request: Request):
    email, bad = await _who(request)
    if bad:
        return bad
    if _too_many(email):
        return JSONResponse({"error": "Too many attempts. Please wait a few minutes."}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

    api_key = str(body.get("api_key", "")).strip()
    request_token = str(body.get("request_token", "")).strip()
    checksum = str(body.get("checksum", "")).strip().lower()
    if not (_API_KEY_RE.match(api_key) and _TOKEN_RE.match(request_token) and _CHECKSUM_RE.match(checksum)):
        return JSONResponse({"error": "Invalid connection details"}, status_code=400)

    def _exchange():
        return httpx.post(
            KITE_SESSION_URL,
            data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
            headers={"X-Kite-Version": "3"},
            timeout=15.0,
        )

    try:
        resp = await run_in_threadpool(_exchange)
        payload = resp.json()
    except Exception:
        return JSONResponse({"error": "Could not reach Zerodha. Please try again."}, status_code=502)

    if resp.status_code != 200 or payload.get("status") != "success":
        # Zerodha's own message (e.g. wrong secret / expired token) is safe to relay
        msg = str(payload.get("message", "Zerodha did not accept this login"))[:200]
        return JSONResponse({"error": msg}, status_code=400)

    data = payload.get("data", {}) or {}
    kite_user_id = str(data.get("user_id", "")).strip()
    kite_user_name = str(data.get("user_name", "")).strip() or None
    # NOTE: data["access_token"] is deliberately never read, stored or logged.
    if not kite_user_id:
        return JSONResponse({"error": "Zerodha response was incomplete"}, status_code=502)

    today = _today_ist()

    def _save():
        from utils.db import get_supabase
        sb = get_supabase()
        prev = sb.from_("user_kite_connections").select("connect_count").eq("email", email).limit(1).execute()
        count = (prev.data[0]["connect_count"] + 1) if prev.data else 1
        sb.from_("user_kite_connections").upsert({
            "email": email,
            "api_key": api_key,
            "kite_user_id": kite_user_id,
            "kite_user_name": kite_user_name,
            "connected_on": today,
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "connect_count": count,
        }, on_conflict="email").execute()

    try:
        await run_in_threadpool(_save)
    except Exception:
        return JSONResponse({"error": "Could not save your connection. Please try again."}, status_code=500)

    return {"ok": True, "kite_user_id": kite_user_id, "connected_on": today}


@router.get("/kite/status")
async def kite_status(request: Request):
    email, bad = await _who(request)
    if bad:
        return bad

    def _read():
        from utils.db import get_supabase
        r = get_supabase().from_("user_kite_connections").select(
            "kite_user_id,connected_on,api_key").eq("email", email).limit(1).execute()
        return r.data[0] if r.data else None

    try:
        row = await run_in_threadpool(_read)
    except Exception:
        return JSONResponse({"error": "Could not read status"}, status_code=500)
    today = _today_ist()
    if not row:
        return {"connected_today": False, "ever_connected": False, "today": today}
    return {
        "connected_today": row["connected_on"] == today,
        "ever_connected": True,
        "kite_user_id": row["kite_user_id"],
        "last_connected_on": row["connected_on"],
        "api_key": row["api_key"],   # not a secret - lets the page prefill the form
        "today": today,
    }
