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
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": totp_value,
            "twofa_type": "totp",
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
    
    # Follow redirects to get request_token
    redirect_url = res.headers.get('Location', '')
    for _ in range(5):
        if 'request_token' in redirect_url:
            break
        res = session.get(redirect_url, allow_redirects=False)
        redirect_url = res.headers.get('Location', '')
    
    if 'request_token' not in redirect_url:
        # Try getting it from cookies
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
    
    import hashlib
    checksum = hashlib.sha256(f"{API_KEY}{request_token}{API_SECRET}".encode()).hexdigest()
    
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]
    
    # Save token
    with open(TOKEN_FILE, 'w') as f:
        json.dump({
            "access_token": access_token,
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d")
        }, f)
    
    print(f"  ✅ Access token saved: {access_token[:8]}...")
    kite.set_access_token(access_token)
    return kite

def get_kite_client():
    """Get authenticated Kite client — auto-logins if token expired"""
    from kiteconnect import KiteConnect
    
    today = time.strftime("%Y-%m-%d")
    
    # Check if we have a valid token for today
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)
        
        if token_data.get("date") == today:
            kite = KiteConnect(api_key=API_KEY)
            kite.set_access_token(token_data["access_token"])
            try:
                kite.profile()  # Validate token
                print(f"  ✅ Using cached token from today")
                return kite
            except Exception:
                print("  ⚠️ Cached token invalid, re-logging in...")
    
    # Auto-login
    if TOTP_SECRET and PASSWORD:
        return auto_login()
    else:
        raise Exception("TOTP_SECRET or PASSWORD not set in .env — cannot auto-login")
