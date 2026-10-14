"""GreekNova access gate (added Sep 26 2026).

One place that decides whether a request to the data API comes from a signed-in
GreekNova member. Modes (env GATE_MODE, read on every request):

  off      - gate does nothing
  log      - DEFAULT. Never blocks. Records what WOULD be blocked so it can be
             checked against real users before enforcing.
  enforce  - blocks user-data requests without a valid member sign-in
             (401 = no/invalid sign-in, 403 = signed in but not a member)

Design notes:
  * A "member" = email present in beta_users. Sign-in = Supabase access token
    sent as  Authorization: Bearer <token>.
  * Public paths (landing page data, market status, waitlist) are never gated.
  * System/admin paths (capture, backfill, cron-style triggers) are NOT gated
    here (something may call them without a user login). They are counted so
    they can be protected separately with a shared secret.
  * If Supabase cannot be reached to verify a token, the request is allowed
    (fail open) and counted as verify_error - a Supabase blip must not lock
    every member out.
  * Nothing sensitive is stored: only counters and paths, no emails.
"""
import os
import time
import hashlib
import threading
from collections import Counter, deque
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

PUBLIC_EXACT = {"/", "/health", "/market-status", "/waitlist-join",
                "/docs", "/openapi.json", "/redoc",
                # Oct 14 2026: GATE_MODE=enforce went live today and these
                # two were missed -- they're how someone GETS a token in the
                # first place, so demanding one to call them is a lockout,
                # not a protection. Caught same-day via a real failed login
                # ("Zerodha login could not be verified"): every kite-login
                # and demo-login call was 401ing on its own gate. Both still
                # do their own real verification (Kite's checksum exchange;
                # the demo code check) -- this only exempts them from the
                # bearer-token requirement that nothing can satisfy yet.
                "/auth/kite-login", "/auth/demo-login"}
PUBLIC_PREFIXES = ("/public/",)

# Oct 14 2026: the two endpoints behind Manish's personal "Morning Market
# Report" scheduled task (api/premarket_brief.py, api/eod_report.py) are
# normal member-gated "user" paths -- real signed-in members reach them
# fine because the frontend's window.fetch monkeypatch (lib/authFetch.ts)
# attaches their Supabase token automatically. The scheduled task is a
# server-side cron caller with no browser and no Supabase session, so it
# has no token to send -- it started 401ing ("Sign in required") the
# moment GATE_MODE went to enforce today. Found while investigating "the
# pre-market report doesn't run and fails every day" -- the report artifact
# was frozen on Oct 9 data days before enforce even went live, so whatever
# broke it originally is still unconfirmed, but this is a second, now
# guaranteed-reproducible, blocker on top of that.
# Fix: a narrow internal-key bypass for exactly these two paths, checked
# BEFORE the normal member/token check below -- not added to SYSTEM_PREFIXES
# or PUBLIC_EXACT, which would also open them to the browser/anonymous path
# Zerodha asked us to close off (any domain, any unauthenticated caller).
# Only a request carrying the correct X-Internal-Key header skips the
# member check; everyone else (including an unauthenticated browser) still
# falls through to the normal user-gate logic below, unaffected.
INTERNAL_FEED_PATHS = {"/premarket-brief", "/eod-report"}

# Callable by machines/cron or admin tools - not gated here (see docstring).
SYSTEM_PREFIXES = (
    "/admin/", "/capture-now", "/force-login", "/archive-", "/run-",
    "/fetch-delivery", "/test-", "/alerts-test", "/cpr-compute",
    "/signal-log/seed-eod", "/daily-oi-summary/compute",
    "/participant-flow/fetch", "/participant-flow/backfill",
    "/spot-volume/backfill", "/spot-volume/eod-append", "/clear-radar-cache",
    "/radar-cache-clear", "/debug-", "/push-check-now",
)

_TOKEN_TTL = 300        # seconds a verified token is trusted from cache
_NEG_TTL = 30           # seconds a failed token is remembered
_MEMBER_TTL = 60        # seconds the beta_users list is cached

_lock = threading.Lock()
_token_cache = {}       # sha256(token) -> (email or None, expires_at)
_members = {"emails": set(), "loaded_at": 0.0}
_trials = {"active": {}, "expired": set(), "loaded_at": 0.0}  # active: email -> expires_date (iso str)
_kite_logins = {"dates": {}, "loaded_at": 0.0}  # email -> last_login_date (iso str), anyone who's ever logged in via Zerodha
_stats = Counter()
_would_block_paths = Counter()
_recent = deque(maxlen=200)
_seen_members_today = {"day": "", "hashes": set()}
_http = httpx.Client(http2=False, timeout=5.0)


def _mode() -> str:
    m = os.getenv("GATE_MODE", "log").strip().lower()
    return m if m in ("off", "log", "enforce") else "log"


def _admin_mode() -> str:
    m = os.getenv("ADMIN_GATE", "log").strip().lower()
    return m if m in ("off", "log", "enforce") else "log"


_sys_recent = deque(maxlen=100)


def _classify(path: str) -> str:
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
        return "public"
    if path.startswith(SYSTEM_PREFIXES):
        return "system"
    return "user"


def _load_members():
    from utils.db import get_supabase
    res = get_supabase().from_("beta_users").select("email").execute()
    return {(r.get("email") or "").strip().lower() for r in (res.data or [])}


def _load_trials():
    """Oct 9 2026: trial_users holds timed access for people who logged in
    via Zerodha but aren't a permanent beta_users member. Returns
    (active: email -> trial_expires_date iso str, expired: set of emails)
    split by today's date (Asia/Kolkata), so a trial_expired decision below
    can carry a different message than a plain not-a-member one."""
    from utils.db import get_supabase
    from utils.market_calendar import today_ist
    res = get_supabase().from_("trial_users").select("email,trial_expires_date").execute()
    today = today_ist().isoformat()
    active, expired = {}, set()
    for r in (res.data or []):
        email = (r.get("email") or "").strip().lower()
        exp = r.get("trial_expires_date")
        if not email or not exp:
            continue
        if exp >= today:
            active[email] = exp
        else:
            expired.add(email)
    return active, expired


def _load_kite_logins():
    """email -> last_login_date (iso str), for every email that has ever
    logged in via Zerodha. An email with no row here never went through
    Zerodha (e.g. the shared demo account, or a beta_users row added by hand
    before anyone built this), so it's not held to the daily-freshness rule
    below -- there's nothing to go stale."""
    from utils.db import get_supabase
    res = get_supabase().from_("kite_login_log").select("email,last_login_date").execute()
    out = {}
    for r in (res.data or []):
        email = (r.get("email") or "").strip().lower()
        d = r.get("last_login_date")
        if email and d:
            out[email] = d
    return out


def _refresh_members_and_trials():
    now = time.time()
    with _lock:
        stale = now - _members["loaded_at"] > _MEMBER_TTL
    if not stale:
        return
    try:
        emails = _load_members()
        active, expired = _load_trials()
        try:
            kite_logins = _load_kite_logins()
        except Exception as e:
            print(f"[GATE] kite_login_log load failed (table may not exist yet): {e}")
            kite_logins = None
        with _lock:
            _members["emails"] = emails
            _members["loaded_at"] = now
            _trials["active"] = active
            _trials["expired"] = expired
            _trials["loaded_at"] = now
            if kite_logins is not None:
                _kite_logins["dates"] = kite_logins
                _kite_logins["loaded_at"] = now
    except Exception as e:
        print(f"[GATE] member/trial list load failed: {e}")


def _kite_session_stale(email: str) -> bool:
    """True only for an email that HAS logged in via Zerodha before but not
    today (Asia/Kolkata). An email never seen in kite_login_log returns
    False (nothing to check) -- covers the demo account and any legacy
    non-Zerodha beta user."""
    from utils.market_calendar import today_ist
    with _lock:
        last = _kite_logins["dates"].get(email)
    if not last:
        return False
    return last != today_ist().isoformat()


def _access_status(email: str) -> str:
    """Returns 'member' (permanent, beta_users), 'trial_active' (within
    trial_users window), 'trial_expired' (trial_users row but past
    trial_expires_date), or 'none' (never logged in / not tracked)."""
    _refresh_members_and_trials()
    with _lock:
        if email in _members["emails"]:
            return "member"
        if email in _trials["active"]:
            return "trial_active"
        if email in _trials["expired"]:
            return "trial_expired"
    return "none"


def _is_member(email: str) -> bool:
    """Back-compat for callers (kite_byot.py) written against the old
    binary check. Now means 'has access' - permanent member or active
    trial - matching what _access_status/_gate use everywhere else."""
    return _access_status(email) in ("member", "trial_active")


def _verify_token(token: str):
    """Returns (email or None, error_flag)."""
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _lock:
        hit = _token_cache.get(key)
    if hit and hit[1] > now:
        return hit[0], False
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    apikey = os.getenv("SUPABASE_KEY", "")
    try:
        r = _http.get(f"{url}/auth/v1/user",
                      headers={"apikey": apikey, "Authorization": f"Bearer {token}"})
    except Exception as e:
        print(f"[GATE] token verify error: {e}")
        return None, True
    if r.status_code == 200:
        email = (r.json().get("email") or "").strip().lower() or None
        with _lock:
            _token_cache[key] = (email, now + _TOKEN_TTL)
            if len(_token_cache) > 5000:
                _token_cache.clear()
        return email, False
    if r.status_code in (400, 401, 403):
        with _lock:
            _token_cache[key] = (None, now + _NEG_TTL)
        return None, False
    return None, True


def _note_member(email: str):
    day = time.strftime("%Y-%m-%d")
    h = hashlib.sha256(email.encode()).hexdigest()[:16]
    with _lock:
        if _seen_members_today["day"] != day:
            _seen_members_today["day"] = day
            _seen_members_today["hashes"] = set()
        _seen_members_today["hashes"].add(h)


_clients = Counter()


def _who(request: Request) -> str:
    try:
        import hashlib
        h = request.headers
        origin = (h.get("origin") or h.get("referer") or "-")[:40]
        ua = (h.get("user-agent") or "-")[:45]
        ip = (h.get("x-forwarded-for") or (request.client.host if request.client else "-")).split(",")[0].strip()
        iph = hashlib.sha256(ip.encode()).hexdigest()[:6]
        return f"{iph} | {origin} | {ua}"
    except Exception:
        return "?"


def _record(decision: str, path: str, blocked_in_enforce: bool, who: str = ""):
    with _lock:
        _stats[decision] += 1
        if blocked_in_enforce:
            _would_block_paths[path] += 1
            _recent.append((time.strftime("%H:%M:%S"), path, decision, who))
            if who:
                _clients[who] += 1


async def _gate(request: Request, call_next):
    mode = _mode()
    path = request.url.path
    if mode == "off" or request.method == "OPTIONS":
        return await call_next(request)

    kind = _classify(path)
    if kind == "system":
        import hmac
        admin_key = os.getenv("GATE_STATS_KEY", "")
        sent = request.headers.get("x-gate-key", "")
        ok = bool(admin_key) and hmac.compare_digest(sent, admin_key)
        if ok:
            _record("system_ok", path, False)
        else:
            _record("system_no_key", path, False)
            with _lock:
                _sys_recent.append((time.strftime("%H:%M:%S"), path, _who(request)))
            if _admin_mode() == "enforce":
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    if kind != "user":
        _record(kind, path, False)
        return await call_next(request)

    if path in INTERNAL_FEED_PATHS:
        import hmac
        feed_key = os.getenv("INTERNAL_FEED_KEY", "")
        sent = request.headers.get("x-internal-key", "")
        if feed_key and hmac.compare_digest(sent, feed_key):
            _record("internal_feed_ok", path, False)
            return await call_next(request)
        # wrong/missing key -- fall through to the normal member/token
        # check below, same as any other caller of this path

    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    if not token:
        decision, block, status = "user_no_token", True, 401
    else:
        email, err = await run_in_threadpool(_verify_token, token)
        if err:
            decision, block, status = "user_verify_error", False, 0
        elif not email:
            decision, block, status = "user_bad_token", True, 401
        else:
            status_kind = await run_in_threadpool(_access_status, email)
            if status_kind in ("member", "trial_active"):
                # Oct 14 2026: being a member/active trial proves identity and
                # payment/eligibility, but Zerodha requires the Zerodha login
                # itself to still be current -- someone who logged in via
                # Kite days ago and hasn't been back today shouldn't see data
                # just because their GreekNova (Supabase) session is still
                # valid. Only applies to emails that have a Zerodha login on
                # record at all, so the shared demo account is unaffected.
                if await run_in_threadpool(_kite_session_stale, email):
                    decision, block, status = "kite_session_stale", True, 401
                else:
                    _note_member(email)
                    decision, block, status = (
                        ("user_member_ok", False, 0) if status_kind == "member"
                        else ("user_trial_ok", False, 0)
                    )
            elif status_kind == "trial_expired":
                decision, block, status = "user_trial_expired", True, 403
            else:
                decision, block, status = "user_not_member", True, 403

    _record(decision, path, block, _who(request) if block else "")
    if block and mode == "enforce":
        if decision == "kite_session_stale":
            msg = "Your Zerodha session has expired. Please log in with Zerodha again."
        elif decision == "user_trial_expired":
            msg = "Your GreekNova trial has ended. Upgrade to keep access."
        elif status == 401:
            msg = "Sign in required"
        else:
            msg = "This email does not have GreekNova access"
        return JSONResponse({"error": msg, "code": decision}, status_code=status)
    return await call_next(request)


def _stats_route(request: Request):
    key = os.getenv("GATE_STATS_KEY", "")
    if not key or request.headers.get("x-gate-key", "") != key:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    with _lock:
        return {
            "mode": _mode(),
            "admin_mode": _admin_mode(),
            "system_no_key_recent": list(_sys_recent)[-25:],
            "since_restart_counts": dict(_stats),
            "distinct_members_seen_today": len(_seen_members_today["hashes"]),
            "top_would_block_paths": _would_block_paths.most_common(25),
            "recent_would_block": list(_recent)[-25:],
            "blocked_clients": _clients.most_common(10),
            "member_list_size": len(_members["emails"]),
            "trial_active_count": len(_trials["active"]),
            "trial_expired_count": len(_trials["expired"]),
        }


def install_gate(app):
    app.add_middleware(BaseHTTPMiddleware, dispatch=_gate)
    app.add_api_route("/admin/gate-stats", _stats_route, methods=["GET"])
    print(f"[GATE] access gate installed, mode={_mode()}")
