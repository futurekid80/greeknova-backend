from typing import Optional
import os
import json
import time
import requests
import pyotp
from dotenv import load_dotenv

load_dotenv()  # Removed hardcoded local path — uses Railway env vars directly

TOKEN_FILE = os.path.expanduser('~/.greeksnova_token')
API_KEY    = os.getenv('KITE_API_KEY', '90q9h621tubh2kkk')
API_SECRET = os.getenv('KITE_API_SECRET', '')
USER_ID    = os.getenv('ZERODHA_USER_ID', 'DQA100')
PASSWORD   = os.getenv('ZERODHA_PASSWORD', '')
TOTP_SECRET = os.getenv('ZERODHA_TOTP_SECRET', '')

# ── Token health check tracking ───────────────────────────────────────────────
_last_token_check: float = 0
TOKEN_CHECK_INTERVAL = 30 * 60  # 30 minutes


def save_token_to_supabase(access_token: str):
    """Save today's token to Supabase so Railway + Claude Code routines can use it"""
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        today = time.strftime("%Y-%m-%d")
        now   = time.strftime("%Y-%m-%d %H:%M:%S+00")
        supabase.from_("user_kite_tokens").upsert({
            "email":             "hardhittrader@gmail.com",
            "kite_user_id":      USER_ID,
            "kite_user_name":    "Manish",
            "kite_access_token": access_token,
            "token_date":        today,
            "connected_at":      now,
        }, on_conflict="email").execute()
        print(f"  ✅ Token saved to Supabase for {today}")
    except Exception as e:
        print(f"  ⚠️ Could not save token to Supabase: {e}")


def get_token_from_supabase() -> Optional[str]:
    """Read today's token from Supabase — Railway's source of truth"""
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        today = time.strftime("%Y-%m-%d")
        result = supabase.from_("user_kite_tokens")\
            .select("kite_access_token, token_date")\
            .eq("email", "hardhittrader@gmail.com")\
            .eq("token_date", today)\
            .limit(1).execute()
        if result.data:
            token = result.data[0]["kite_access_token"]
            print(f"  ✅ Token loaded from Supabase for {today}")
            return token
    except Exception as e:
        print(f"  ⚠️ Could not read token from Supabase: {e}")
    return None


def _do_login() -> object:
    """
    Core login function — performs TOTP login and returns authenticated kite client.
    Does NOT retry — caller handles retries.
    """
    from kiteconnect import KiteConnect

    print("🔐 Starting auto-login...")
    session = requests.Session()

    # Step 1: Password login
    login_res = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD}
    )
    login_data = login_res.json()

    if login_data.get("status") != "success":
        raise Exception(f"Login failed: {login_data.get('message', 'Unknown error')}")

    request_id = login_data["data"]["request_id"]
    print(f"  ✅ Password accepted, request_id: {request_id[:8]}...")

    # Step 2: TOTP 2FA
    totp = pyotp.TOTP(TOTP_SECRET)
    totp_value = totp.now()
    print(f"  🔢 Generated TOTP: {totp_value}")

    twofa_res = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":      USER_ID,
            "request_id":   request_id,
            "twofa_value":  totp_value,
            "twofa_type":   "totp",
            "skip_session": True,
        }
    )
    twofa_data = twofa_res.json()

    if twofa_data.get("status") != "success":
        raise Exception(f"2FA failed: {twofa_data.get('message', 'Unknown error')}")

    print("  ✅ 2FA accepted")

    # Step 3: Get request token
    login_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
    res = session.get(login_url, allow_redirects=False)
    redirect_url = res.headers.get('Location', '')

    for _ in range(5):
        if 'request_token' in redirect_url:
            break
        if not redirect_url:
            raise Exception(f"Empty redirect URL — Kite may be blocking Railway IP. Use Mac login instead.")
        res = session.get(redirect_url, allow_redirects=False)
        redirect_url = res.headers.get('Location', '')

    if 'request_token' not in redirect_url:
        raise Exception(f"Could not get request_token from redirect. Railway IP may be blocked by Kite. Token will load from Supabase instead.")

    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(redirect_url)
    request_token = parse_qs(parsed.query).get('request_token', [None])[0]

    if not request_token:
        raise Exception("request_token not found in URL")

    print(f"  ✅ Got request_token: {request_token[:8]}...")

    # Step 4: Exchange for access token
    kite = KiteConnect(api_key=API_KEY)
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]

    # Save locally + Supabase
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump({
                "access_token": access_token,
                "timestamp":    time.time(),
                "date":         time.strftime("%Y-%m-%d")
            }, f)
    except Exception:
        pass

    save_token_to_supabase(access_token)
    kite.set_access_token(access_token)
    return kite


def auto_login(max_retries: int = 3, retry_delay: int = 30) -> object:
    """
    Fully automated login with retry logic.
    Retries up to max_retries times with retry_delay seconds between attempts.
    This makes startup login bulletproof against transient network issues.
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            kite = _do_login()
            if attempt > 1:
                print(f"✅ Login successful on attempt {attempt}")
            return kite
        except Exception as e:
            last_error = e
            print(f"⚠️ Login attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                print(f"   Retrying in {retry_delay}s...")
                time.sleep(retry_delay)

    raise Exception(f"All {max_retries} login attempts failed. Last error: {last_error}")


def check_and_refresh_token():
    """
    Token health check — validates current token every 30 mins.
    If token is invalid, auto-logins to get a fresh one.
    Called from the keepalive ping or capture cycle.
    """
    global _last_token_check
    now = time.time()

    # Only check every 30 minutes
    if now - _last_token_check < TOKEN_CHECK_INTERVAL:
        return

    _last_token_check = now

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=API_KEY)

        # Try Supabase token first
        supabase_token = get_token_from_supabase()
        if supabase_token:
            kite.set_access_token(supabase_token)
            try:
                kite.profile()
                print("💚 Token health check: OK")
                return
            except Exception:
                print("⚠️ Token health check: Token invalid — refreshing...")

        # Token invalid — auto-login
        auto_login()
        print("✅ Token refreshed via health check")

    except Exception as e:
        print(f"❌ Token health check failed: {e}")


def get_kite_client():
    """
    Get authenticated Kite client.
    Priority: 1) Local file (Mac), 2) Supabase (Railway), 3) Auto-login with retry
    """
    from kiteconnect import KiteConnect
    today = time.strftime("%Y-%m-%d")

    # ── 1. Try local token file ───────────────────────────────────────────────
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                token_data = json.load(f)
            if token_data.get("date") == today:
                kite = KiteConnect(api_key=API_KEY)
                kite.set_access_token(token_data["access_token"])
                kite.profile()  # Validate
                print("  ✅ Using cached local token")
                return kite
        except Exception:
            print("  ⚠️ Local token invalid, trying Supabase...")

    # ── 2. Try Supabase token ─────────────────────────────────────────────────
    supabase_token = get_token_from_supabase()
    if supabase_token:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(supabase_token)
        try:
            kite.profile()  # Validate
            print("  ✅ Using Supabase token")
            return kite
        except Exception:
            print("  ⚠️ Supabase token invalid, attempting auto-login...")

    # ── 3. Auto-login with retry ──────────────────────────────────────────────
    if TOTP_SECRET and PASSWORD:
        return auto_login()
    else:
        raise Exception("No valid token found and TOTP_SECRET/PASSWORD not set")
