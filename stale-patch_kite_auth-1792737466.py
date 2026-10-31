path = "services/kite_auth.py"
with open(path, "r") as f:
    content = f.read()
orig = content

# ---------- 1. generalize _do_login() to take an explicit api_key/api_secret pair ----------
old_sig = '''def _do_login() -> object:
    """
    Core login function — performs TOTP login and returns authenticated kite client.
    Does NOT retry — caller handles retries.
    """
    from kiteconnect import KiteConnect

    print("🔐 Starting auto-login...")'''

new_sig = '''def _do_login(api_key: str = None, api_secret: str = None, label: str = "main") -> object:
    """
    Core login function — performs TOTP login and returns authenticated kite client.
    Does NOT retry — caller handles retries.

    api_key/api_secret default to the module-level main-app credentials
    (API_KEY/API_SECRET). Pass a different pair to authorize a DIFFERENT
    Kite Connect app against this same Zerodha account (same password+TOTP
    flow, just exchanged against a different app) -- used by
    get_historical_kite_client() below for the dedicated Historical-Data
    app, kept fully separate from the main login so nothing here can ever
    affect the live member-login/data-capture session.
    """
    from kiteconnect import KiteConnect

    key = api_key or API_KEY
    secret = api_secret or API_SECRET

    print(f"🔐 Starting auto-login ({label})...")'''

assert old_sig in content
content = content.replace(old_sig, new_sig, 1)

old_redirect = '''    # Step 3: Get request token
    login_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"'''
new_redirect = '''    # Step 3: Get request token
    login_url = f"https://kite.trade/connect/login?api_key={key}&v=3"'''
assert old_redirect in content
content = content.replace(old_redirect, new_redirect, 1)

old_exchange = '''    # Step 4: Exchange for access token
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
    return kite'''

new_exchange = '''    # Step 4: Exchange for access token
    kite = KiteConnect(api_key=key)
    session_data = kite.generate_session(request_token, api_secret=secret)
    access_token = session_data["access_token"]

    if label == "main":
        # Save locally + Supabase -- only the main app's token is cached to
        # the local file (Mac-only convenience); the historical app's token
        # is Railway-only, saved by get_historical_kite_client() itself.
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
    return kite'''

assert old_exchange in content
content = content.replace(old_exchange, new_exchange, 1)

# ---------- 2. add get_historical_kite_client(), fully separate cached session ----------
anchor2 = '''def get_kite_client():
    """
    Get authenticated Kite client.
    Priority: 1) Local file (Mac), 2) Supabase (Railway), 3) Auto-login with retry
    """'''
assert anchor2 in content

historical_fn = '''HISTORICAL_API_KEY    = os.getenv('TRADING_BOT_API_KEY', '')
HISTORICAL_API_SECRET = os.getenv('TRADING_BOT_API_SECRET', '')
HISTORICAL_TOKEN_EMAIL = "trading-bot-historical@internal"


def _save_historical_token_to_supabase(access_token: str):
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        today = time.strftime("%Y-%m-%d")
        now   = time.strftime("%Y-%m-%d %H:%M:%S+00")
        supabase.from_("user_kite_tokens").upsert({
            "email":             HISTORICAL_TOKEN_EMAIL,
            "kite_user_id":      USER_ID,
            "kite_user_name":    "Trading Bot (historical-only)",
            "kite_access_token": access_token,
            "token_date":        today,
            "connected_at":      now,
        }, on_conflict="email").execute()
        print(f"  ✅ Historical token saved to Supabase for {today}")
    except Exception as e:
        print(f"  ⚠️ Could not save historical token to Supabase: {e}")


def _get_historical_token_from_supabase():
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        today = time.strftime("%Y-%m-%d")
        result = supabase.from_("user_kite_tokens")\\
            .select("kite_access_token, token_date")\\
            .eq("email", HISTORICAL_TOKEN_EMAIL)\\
            .eq("token_date", today)\\
            .limit(1).execute()
        if result.data:
            return result.data[0]["kite_access_token"]
    except Exception as e:
        print(f"  ⚠️ Could not read historical token from Supabase: {e}")
    return None


def get_historical_kite_client():
    """
    Dedicated client for kite.historical_data() calls ONLY (Oct 23 2026) --
    the main app's Kite Connect key is personal/multiuser and structurally
    cannot hold the Historical Data add-on (confirmed via "Insufficient
    permission for that call" on every symbol from 2026-10-22). This logs
    in to a SEPARATE, properly-typed Kite Connect app ("Trading Bot",
    TRADING_BOT_API_KEY/SECRET) that does carry the add-on, using the same
    Zerodha account password+TOTP (one Zerodha login can authorize several
    of its own registered apps independently).

    Deliberately isolated from get_kite_client()/the main session: its own
    env vars, its own Supabase-cached token row (a different email key), no
    shared state. A failure or expired subscription here can only affect
    code that explicitly calls this function (currently just the Wolves
    Scanner's weekly/monthly cache) -- it can never break member login,
    live quotes/OI capture, or anything else running on the main client.
    Raises if TRADING_BOT_API_KEY/SECRET aren't set, so callers should
    wrap this in a try/except and fall back (e.g. to a shorter-lookback
    reconstruction) rather than let it take down the whole job.
    """
    from kiteconnect import KiteConnect

    if not HISTORICAL_API_KEY or not HISTORICAL_API_SECRET:
        raise Exception("TRADING_BOT_API_KEY/TRADING_BOT_API_SECRET not set")

    token = _get_historical_token_from_supabase()
    if token:
        kite = KiteConnect(api_key=HISTORICAL_API_KEY)
        kite.set_access_token(token)
        try:
            kite.profile()
            return kite
        except Exception:
            print("  ⚠️ Historical token invalid, re-logging in...")

    if not (TOTP_SECRET and PASSWORD):
        raise Exception("Historical client: no cached token and TOTP_SECRET/PASSWORD not set")

    kite = _do_login(api_key=HISTORICAL_API_KEY, api_secret=HISTORICAL_API_SECRET, label="historical")
    _save_historical_token_to_supabase(kite.access_token)
    return kite


def get_kite_client():
    """
    Get authenticated Kite client.
    Priority: 1) Local file (Mac), 2) Supabase (Railway), 3) Auto-login with retry
    """'''

content = content.replace(anchor2, historical_fn, 1)

assert content != orig
with open("services/kite_auth_new.py", "w") as f:
    f.write(content)

import ast
ast.parse(content)
print("OK - patched and syntax-valid, written to services/kite_auth_new.py")
