import os
import json
import time
import requests
import pyotp
from dotenv import load_dotenv

load_dotenv('/Users/apple/optionspulse/.env')

TOKEN_FILE = os.path.expanduser('~/.greeksnova_token')
API_KEY = os.getenv('KITE_API_KEY', '90q9h621tubh2kkk')
API_SECRET = os.getenv('KITE_API_SECRET', '')
USER_ID = os.getenv('ZERODHA_USER_ID', 'DQA100')
PASSWORD = os.getenv('ZERODHA_PASSWORD', '')
TOTP_SECRET = os.getenv('ZERODHA_TOTP_SECRET', '')


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


def get_token_from_supabase() -> str | None:
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


def auto_login() -> str:
    """Fully automated login using TOTP — no manual steps needed"""
    print("🔐 Starting auto-login...")

    session = requests.Session()

    # Step 1: POST login with user_id + password
    login_res = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD}
    )
    login_data = login_res.json()

    if login_data.get("status") != "success":
        raise Exception(f"Login failed: {login_data.get('message', 'Unknown error')}")

    request_id = login_data["data"]["request_id"]
    print(f"  ✅ Password accepted, request_id: {request_id[:8]}...")

    # Step 2: Generate TOTP and complete 2FA
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

    # Step 3: Get request token from redirect
    login_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
    res = session.get(login_url, allow_redirects=False)

    redirect_url = res.headers.get('Location', '')
    for _ in range(5):
        if 'request_token' in redirect_url:
            break
        res = session.get(redirect_url, allow_redirects=False)
        redirect_url = res.headers.get('Location', '')

    if 'request_token' not in redirect_url:
        raise Exception(f"Could not get request_token from redirect: {redirect_url}")

    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(redirect_url)
    request_token = parse_qs(parsed.query).get('request_token', [None])[0]

    if not request_token:
        raise Exception("request_token not found in URL")

    print(f"  ✅ Got request_token: {request_token[:8]}...")

    # Step 4: Exchange for access_token
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]

    # Save to local file (Mac) + Supabase (Railway)
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump({
                "access_token": access_token,
                "timestamp":    time.time(),
                "date":         time.strftime("%Y-%m-%d")
            }, f)
    except Exception:
        pass  # Local file optional — Supabase is the source of truth

    save_token_to_supabase(access_token)

    kite.set_access_token(access_token)
    return kite


def get_kite_client():
    """
    Get authenticated Kite client.
    Priority: 1) Local file (Mac), 2) Supabase (Railway), 3) Auto-login
    """
    from kiteconnect import KiteConnect
    today = time.strftime("%Y-%m-%d")

    # ── 1. Try local token file (Mac dev environment) ─────────────────────────
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

    # ── 2. Try Supabase token (Railway environment) ───────────────────────────
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

    # ── 3. Auto-login (runs on Mac, saves token to Supabase for Railway) ──────
    if TOTP_SECRET and PASSWORD:
        return auto_login()
    else:
        raise Exception("No valid token found and TOTP_SECRET/PASSWORD not set")
