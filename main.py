import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn, sys, time
from commoditynova.mcx_scheduler import start_mcx_scheduler
from commoditynova.mcx_router import router as mcx_router
from utils.db import get_supabase
from commoditynova.mcx_oi_map_router import router as mcx_oi_map_router
from api.daily_oi_summary import compute_daily_summary


INDICES = ["NIFTY","BANKNIFTY","FINNIFTY"]
TOP30 = [
    # Original 30
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK",
    "LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH",
    "TATACONSUM","TATASTEEL","ADANIENT","POWERGRID","NTPC",
    "ONGC","JSWSTEEL","COALINDIA","BAJAJFINSV","TECHM",
    # Nifty 50 additions
    "APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA",
    "DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO",
    "HDFCLIFE","INDUSINDBK","JIOFIN","M&M","NESTLEIND",
    "SBILIFE","SHRIRAMFIN","TRENT",
    # High-liquidity F&O additions
    "ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN",
    "DLF","GAIL","HAVELLS","HAL","INDIGO",
    "PFC","RECLTD","SAIL","TATAPOWER","VEDL",
]
INDEX_NSE_MAP = {"NIFTY":"NSE:NIFTY 50","BANKNIFTY":"NSE:NIFTY BANK","FINNIFTY":"NSE:NIFTY FIN SERVICE"}
STOCK_NSE_MAP = {
    "RELIANCE":"NSE:RELIANCE","TCS":"NSE:TCS","HDFCBANK":"NSE:HDFCBANK",
    "INFY":"NSE:INFY","ICICIBANK":"NSE:ICICIBANK","HINDUNILVR":"NSE:HINDUNILVR",
    "ITC":"NSE:ITC","SBIN":"NSE:SBIN","BHARTIARTL":"NSE:BHARTIARTL",
    "KOTAKBANK":"NSE:KOTAKBANK","LT":"NSE:LT","AXISBANK":"NSE:AXISBANK",
    "ASIANPAINT":"NSE:ASIANPAINT","MARUTI":"NSE:MARUTI","TITAN":"NSE:TITAN",
    "SUNPHARMA":"NSE:SUNPHARMA","ULTRACEMCO":"NSE:ULTRACEMCO","BAJFINANCE":"NSE:BAJFINANCE",
    "WIPRO":"NSE:WIPRO","HCLTECH":"NSE:HCLTECH","TATACONSUM":"NSE:TATACONSUM",
    "TATASTEEL":"NSE:TATASTEEL","ADANIENT":"NSE:ADANIENT","POWERGRID":"NSE:POWERGRID",
    "NTPC":"NSE:NTPC","ONGC":"NSE:ONGC","JSWSTEEL":"NSE:JSWSTEEL",
    "COALINDIA":"NSE:COALINDIA","BAJAJFINSV":"NSE:BAJAJFINSV","TECHM":"NSE:TECHM",
    "APOLLOHOSP":"NSE:APOLLOHOSP","BAJAJ-AUTO":"NSE:BAJAJ-AUTO",
    "BPCL":"NSE:BPCL","BRITANNIA":"NSE:BRITANNIA","CIPLA":"NSE:CIPLA",
    "DRREDDY":"NSE:DRREDDY","EICHERMOT":"NSE:EICHERMOT","GRASIM":"NSE:GRASIM",
    "HEROMOTOCO":"NSE:HEROMOTOCO","HINDALCO":"NSE:HINDALCO",
    "HDFCLIFE":"NSE:HDFCLIFE","INDUSINDBK":"NSE:INDUSINDBK",
    "JIOFIN":"NSE:JIOFIN","M&M":"NSE:M&M","NESTLEIND":"NSE:NESTLEIND",
    "SBILIFE":"NSE:SBILIFE","SHRIRAMFIN":"NSE:SHRIRAMFIN","TRENT":"NSE:TRENT",
    "ADANIPORTS":"NSE:ADANIPORTS","BANKBARODA":"NSE:BANKBARODA",
    "BEL":"NSE:BEL","CANBK":"NSE:CANBK","CHOLAFIN":"NSE:CHOLAFIN",
    "DLF":"NSE:DLF","GAIL":"NSE:GAIL","HAVELLS":"NSE:HAVELLS",
    "HAL":"NSE:HAL","INDIGO":"NSE:INDIGO","PFC":"NSE:PFC",
    "RECLTD":"NSE:RECLTD","SAIL":"NSE:SAIL","TATAPOWER":"NSE:TATAPOWER",
    "VEDL":"NSE:VEDL",
}

# CMP cache — updated each capture cycle, used for ATM-centered selection
_last_cmp: dict = {}

def run_full_capture():
    global _last_cmp
    if os.getenv("CAPTURE_ENABLED", "false").lower() != "true":
        return
    from dotenv import load_dotenv
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    if now.weekday() >= 5: return
    if not (9 <= now.hour <= 15): return
    if now.hour == 9 and now.minute < 15: return
    if now.hour == 15 and now.minute > 30: return
    print(f"[CAPTURE] Auto-capture at {now.strftime('%H:%M:%S')}...")
    try:
        from services.kite_auth import get_kite_client
        from utils.db import get_supabase
        kite = get_kite_client()
        supabase = get_supabase()
        timestamp = now.astimezone(timezone.utc).isoformat()
        records = []
        cmp_records = []

        try:
            idx_quotes = kite.quote(list(INDEX_NSE_MAP.values()))
            for sym, key in INDEX_NSE_MAP.items():
                price = idx_quotes.get(key, {}).get("last_price", 0)
                if price:
                    cmp_records.append({"timestamp": timestamp, "symbol": sym, "cmp": float(price)})
                    _last_cmp[sym] = float(price)
        except Exception as e: print(f"  ⚠️ Index CMP: {e}")

        try:
            stk_quotes = kite.quote(list(STOCK_NSE_MAP.values()))
            for sym, key in STOCK_NSE_MAP.items():
                price = stk_quotes.get(key, {}).get("last_price", 0)
                if price:
                    cmp_records.append({"timestamp": timestamp, "symbol": sym, "cmp": float(price)})
                    _last_cmp[sym] = float(price)
        except Exception as e: print(f"  ⚠️ Stock CMP: {e}")

        instruments = kite.instruments("NFO")

        for symbol in INDICES + TOP30:
            is_index = symbol in INDICES
            limit = 50 if is_index else 20
            half  = limit // 2

            found = [i for i in instruments if i["name"] == symbol and i["instrument_type"] in ["CE","PE","FUT"]]
            if not found: continue

            # Separate FUT instruments — always include them, no ATM filtering needed
            fut_instruments = [i for i in found if i["instrument_type"] == "FUT"]
            found = [i for i in found if i["instrument_type"] != "FUT"]

            expiries = sorted(set(i["expiry"] for i in found))
            fut_expiries = sorted(set(i["expiry"] for i in fut_instruments))
            num_expiries = 3 if is_index else 2

            current_price = _last_cmp.get(symbol, 0)

            nearest = []
            for exp in expiries[:num_expiries]:
                exp_instruments = [i for i in found if i["expiry"] == exp]
                if not exp_instruments:
                    continue

                if current_price > 0:
                    exp_instruments.sort(key=lambda i: i["strike"])
                    strikes_sorted = sorted(set(i["strike"] for i in exp_instruments))
                    atm_strike = min(strikes_sorted, key=lambda s: abs(s - current_price))
                    atm_idx = strikes_sorted.index(atm_strike)
                    lower_idx = max(0, atm_idx - half)
                    upper_idx = min(len(strikes_sorted) - 1, atm_idx + half)
                    selected_strikes = set(strikes_sorted[lower_idx:upper_idx + 1])
                    selected = [i for i in exp_instruments if i["strike"] in selected_strikes]
                else:
                    exp_instruments.sort(key=lambda i: i["strike"])
                    mid = len(exp_instruments) // 2
                    selected = exp_instruments[max(0, mid - half): mid + half]

                nearest.extend(selected)

            try:
                # Add FUT instruments for nearest expiries
                for exp in fut_expiries[:num_expiries]:
                    nearest.extend([i for i in fut_instruments if i["expiry"] == exp])
                quotes = kite.quote(["NFO:" + i["tradingsymbol"] for i in nearest])
                for inst in nearest:
                    key = f"NFO:{inst['tradingsymbol']}"
                    if key in quotes:
                        q = quotes[key]
                        records.append({
                        "timestamp":       timestamp,
                        "symbol":          symbol,
                        "tradingsymbol":   inst["tradingsymbol"],
                        "strike":          float(inst["strike"]) if inst["instrument_type"] != "FUT" else 0.0,
                        "option_type":     inst["instrument_type"],
                        "expiry":          inst["expiry"].isoformat(),
                        "oi":              int(q.get("oi", 0)),
                        "oi_day_high":     int(q.get("oi_day_high", 0)),
                        "volume":          int(q.get("volume", 0)),
                        "last_price":      float(q.get("last_price", 0)),
                        "is_index":        is_index,
                    })
                time.sleep(0.3)
            except Exception as e:
                print(f"  ❌ {symbol}: {e}")

        if records:
            for i in range(0, len(records), 500):
                supabase.table("oi_snapshots").insert(records[i:i+500]).execute()
        if cmp_records:
            supabase.table("cmp_prices").insert(cmp_records).execute()
        print(f"  ✅ Saved {len(records)} OI + {len(cmp_records)} CMP records")

        try:
            from services.alert_engine import run_alert_check
            run_alert_check()
        except Exception as ae:
            print(f"  ⚠️ Alert engine error: {ae}")

        try:
            from api.cpr import update_cpr_status
            update_cpr_status()
        except Exception as ce:
            print(f"  ⚠️ CPR status update error: {ce}")

    except Exception as e:
        print(f"  ❌ Capture failed: {e}")


scheduler = BackgroundScheduler(
    job_defaults={"misfire_grace_time": 300}
)

def keepalive_ping():
    try:
        import requests
        requests.get("https://greeknova-backend-production.up.railway.app/health", timeout=5)
        print("💓 Keepalive ping sent")
    except Exception as e:
        print(f"⚠️ Keepalive failed: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Login on startup ───────────────────────────────────────────────────
    try:
        from services.kite_auth import auto_login
        auto_login()
        print("✅ Startup login successful")
    except Exception as e:
        print(f"⚠️ Startup login failed: {e}")

    # ── Startup capture — runs immediately after login ─────────────────────
    try:
        import threading
        threading.Thread(target=run_full_capture, daemon=True).start()
        print("📸 Startup capture triggered")
    except Exception as e:
        print(f"⚠️ Startup capture failed: {e}")

    # ── Warm up Positional Radar cache ─────────────────────────────────────
    try:
        from api.positional_radar import get_positional_radar
        def _warmup():
            try:
                import time
                time.sleep(5)
                get_positional_radar(0)
                print("📊 Positional Radar warm-up complete")
            except Exception as e:
                print(f"📊 Positional Radar warm-up failed (non-fatal): {e}")
        threading.Thread(target=_warmup, daemon=True).start()
        print("📊 Positional Radar cache warm-up triggered")
    except Exception as e:
        print(f"⚠️ Positional Radar warm-up failed: {e}")

    # ── GreekNova jobs (unchanged) ─────────────────────────────────────────
    scheduler.add_job(run_full_capture, "interval", minutes=5, id="full_capture")
    scheduler.add_job(auto_refresh_token, "cron", hour=8, minute=30, timezone="Asia/Kolkata", id="token_refresh")
    scheduler.add_job(
        lambda: __import__('api.cpr', fromlist=['compute_and_store_cpr']).compute_and_store_cpr(),
        "cron", hour=16, minute=45, timezone="Asia/Kolkata", id="eod_cpr_compute",
        misfire_grace_time=600
    )
    scheduler.add_job(
        lambda: __import__('api.cpr', fromlist=['compute_and_store_weekly_monthly_cpr']).compute_and_store_weekly_monthly_cpr(),
        "cron", hour=16, minute=50, timezone="Asia/Kolkata", id="weekly_monthly_cpr_compute",
        misfire_grace_time=600
    )
    scheduler.add_job(keepalive_ping, "interval", minutes=10, id="keepalive")
    scheduler.add_job(
        archive_old_snapshots,
        "cron",
        day_of_week="sun", hour=20, minute=0,
        timezone="Asia/Kolkata",
        id="weekly_archive",
        misfire_grace_time=3600,
        replace_existing=True
    )
    scheduler.add_job(
        lambda: compute_daily_summary(get_supabase()),
        "cron",
        hour=16, minute=45,
        timezone="Asia/Kolkata",
        id="daily_oi_summary",
        misfire_grace_time=600,
        replace_existing=True
    )

    scheduler.add_job(
        save_eod_signal_log,
        "cron", hour=15, minute=35, timezone="Asia/Kolkata", id="eod_signal_save",
        misfire_grace_time=600
    )

    scheduler.add_job(
        refresh_radar_cache,
        "cron", hour=16, minute=46, timezone="Asia/Kolkata", id="radar_cache_refresh",
        misfire_grace_time=600
    )

    scheduler.add_job(
        watchdog_cpr,
        "cron", hour=17, minute=15, timezone="Asia/Kolkata", id="cpr_watchdog",
        misfire_grace_time=600
    )
    scheduler.start()

    scheduler.add_job(
        lambda: __import__('api.participant_flow', fromlist=['fetch_and_store_participant_flow']).fetch_and_store_participant_flow(),
        "cron", hour=19, minute=30, timezone="Asia/Kolkata", id="participant_flow_fetch",
        misfire_grace_time=600
    )
    scheduler.add_job(
        watchdog_participant_flow,
        "cron", hour=20, minute=0, timezone="Asia/Kolkata", id="participant_flow_watchdog",
        misfire_grace_time=600
    )

    # ── CommodityNova scheduler ────────────────────────────────────────────
    from services.kite_auth import get_kite_client
    from utils.db import get_supabase
    kite = get_kite_client()
    supabase = get_supabase()
    start_mcx_scheduler(kite, supabase)
    # ──────────────────────────────────────────────────────────────────────

    print("✅ GreekNova backend started")
    print("✅ CommodityNova MCX scheduler started")
    print("📸 Full capture every 5 min during market hours")
    print("🔔 Alert engine: wired into capture cycle")
    print("🎯 ATM-centered strike selection: 25 above + 25 below for indices")
    yield
    scheduler.shutdown()

app = FastAPI(title="GreekNova API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "https://greeknova-frontend.vercel.app",
        "https://app.greeknova.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root(): return {"status": "GreekNova API running", "version": "0.1.0"}

@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/capture-now")
def capture_now(): run_full_capture(); return {"status": "capture triggered"}

@app.get("/force-login")
def force_login():
    try:
        from services.kite_auth import auto_login
        kite = auto_login()
        profile = kite.profile()
        return {"status": "success", "user": profile["user_name"]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/alerts-test")
def alerts_test():
    try:
        from services.alert_engine import run_alert_check
        run_alert_check()
        return {"status": "alert check triggered"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/oi-spikes")
def oi_spikes(threshold: float = 10.0, date: str = None):
    from api.oi_spike import get_oi_spikes
    return get_oi_spikes(threshold, date)

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)

@app.get("/pcr-trend/{symbol}")
def pcr_trend(symbol: str = "NIFTY", expiry: str = None):
    from api.pcr_trend import get_pcr_trend
    return get_pcr_trend(symbol.upper(), expiry)

@app.get("/pcr-expiries/{symbol}")
def pcr_expiries(symbol: str = "NIFTY"):
    from utils.db import get_supabase
    from datetime import datetime, timezone
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = supabase.from_("oi_snapshots").select("expiry").eq("symbol", symbol).gte("timestamp", f"{today}T00:00:00+00:00").execute()
    expiries = sorted(set(r["expiry"] for r in result.data if r["expiry"]))
    return {"symbol": symbol, "expiries": expiries}

@app.get("/stock-oi/{symbol}")
def stock_oi(symbol: str):
    from api.stock_oi import get_stock_oi
    return get_stock_oi(symbol.upper())

@app.get("/volume-spikes")
def volume_spikes(threshold: float = 50.0, date: str = None):
    from api.volume_spike import get_volume_spikes
    return get_volume_spikes(threshold, date)

@app.get("/confluence")
def confluence():
    from api.confluence import get_confluence
    return get_confluence()

@app.get("/max-pain")
def max_pain():
    from api.max_pain import get_max_pain_all
    return get_max_pain_all()

def archive_old_snapshots():
    """
    Weekly job — moves non-EOD intraday snapshots older than 7 days to archive.
    Keeps only last snapshot of each day for older dates.
    Nothing deleted without archiving first.
    """
    from utils.db import get_supabase
    from datetime import datetime, timedelta
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    cutoff = (datetime.now(ist) - timedelta(days=7)).strftime('%Y-%m-%dT00:00:00+00:00')
    supabase = get_supabase()
    try:
        print(f"[ARCHIVE] Starting weekly archive — cutoff: {cutoff}")
        supabase.rpc('archive_old_oi_snapshots', {'cutoff_ts': cutoff}).execute()
        print(f"[ARCHIVE] Weekly archive complete ✅")
    except Exception as e:
        print(f"[ARCHIVE] Error: {e}")

def watchdog_cpr():
    """
    Watchdog job — runs at 5:15 PM IST.
    Checks if next trading day's CPR was computed by the 4:45 PM job.
    If missing (e.g. due to Railway restart), re-runs CPR compute automatically.
    """
    import pytz
    from datetime import datetime, timedelta
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return  # Skip weekends

    # Compute what trade_date should exist
    next_day = now.date() + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    expected_date = next_day.isoformat()

    # Check if CPR exists for that date
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        result = supabase.from_("cpr_levels")\
            .select("symbol")\
            .eq("trade_date", expected_date)\
            .limit(1).execute()

        if result.data:
            print(f"[CPR Watchdog] ✅ CPR for {expected_date} exists — no action needed")
            return

        # CPR missing — re-run
        print(f"[CPR Watchdog] ⚠️ CPR for {expected_date} missing — re-running compute...")
        from api.cpr import compute_and_store_cpr
        result = compute_and_store_cpr()
        print(f"[CPR Watchdog] ✅ Re-computed: {result}")

    except Exception as e:
        print(f"[CPR Watchdog] ❌ Error: {e}")

def watchdog_participant_flow():
    """Watchdog — runs at 7 PM. Re-fetches if today's data missing."""
    import pytz
    from datetime import datetime, timedelta
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return
    today = now.date()
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        result = supabase.from_("participant_flow")\
            .select("participant")\
            .eq("trade_date", today.isoformat())\
            .limit(1).execute()
        if result.data:
            print(f"[ParticipantFlow Watchdog] ✅ Data exists for {today}")
            return
        print(f"[ParticipantFlow Watchdog] ⚠️ Missing — fetching...")
        from api.participant_flow import fetch_and_store_participant_flow
        fetch_and_store_participant_flow()
    except Exception as e:
        print(f"[ParticipantFlow Watchdog] ❌ {e}")

def save_eod_signal_log():
    import pytz
    from datetime import datetime
    ist = pytz.timezone('Asia/Kolkata')
    if datetime.now(ist).weekday() >= 5:
        return
    try:
        import api.signal_log as sl
        from utils.db import get_supabase
        original = sl.is_market_hours
        sl.is_market_hours = lambda: True
        try:
            result = sl.get_signal_log()
        finally:
            sl.is_market_hours = original
        if result.get("signals"):
            sl._save_eod_to_supabase(get_supabase(), result)
            print(f"[EOD Signal] ✅ Saved {len(result['signals'])} signals")
        else:
            print(f"[EOD Signal] ℹ️ No signals today")
    except Exception as e:
        print(f"[EOD Signal] ❌ {e}")

def refresh_radar_cache():
    """Refresh positional radar cache at 4:46 PM after EOD summary."""
    import pytz
    from datetime import datetime
    ist = pytz.timezone('Asia/Kolkata')
    if datetime.now(ist).weekday() >= 5:
        return
    try:
        from api.positional_radar import get_monthly_expiry, get_series_start
        from utils.db import get_supabase
        import datetime as dt
        today = dt.date.today()
        expiry = get_monthly_expiry(today.year, today.month)
        series_start = get_series_start(expiry)
        result = get_supabase().rpc("refresh_positional_radar_cache", {
            "p_series_start": series_start,
            "p_series_end": today.isoformat()
        }).execute()
        print(f"[Radar Cache] ✅ Refreshed")
    except Exception as e:
        print(f"[Radar Cache] ❌ {e}")

def auto_refresh_token():
    from datetime import datetime
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    if now.weekday() >= 5: return
    print(f"🔐 Railway auto-login at {now.strftime('%H:%M IST')}...")
    try:
        from services.kite_auth import auto_login
        kite = auto_login()
        profile = kite.profile()
        print(f"✅ Railway auto-login successful: {profile['user_name']}")
    except Exception as e:
        print(f"❌ Railway auto-login failed: {e}")

@app.get("/uoa")
def uoa(date: str = None):
    from api.uoa import get_uoa
    return get_uoa(date)

@app.get("/option-chain/{symbol}")
def option_chain(symbol: str = "NIFTY", expiry: str = None):
    from api.option_chain import get_option_chain
    return get_option_chain(symbol.upper(), expiry)

@app.get("/oi-history/{symbol}")
def oi_history(symbol: str = "NIFTY", date_a: str = None, date_b: str = None, expiry: str = None):
    from api.oi_history import get_oi_comparison
    return get_oi_comparison(symbol.upper(), date_a, date_b, expiry)

@app.get("/oi-dates/{symbol}")
def oi_dates(symbol: str = "NIFTY"):
    from api.oi_history import get_available_dates
    return get_available_dates(symbol.upper())

@app.get("/eod-analysis/{symbol}")
def eod_analysis(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    from api.eod_analysis import get_eod_analysis
    return get_eod_analysis(symbol.upper(), date, expiry)

@app.get("/oi-pulse")
def oi_pulse():
    from api.oi_pulse import get_oi_pulse
    return get_oi_pulse()

@app.get("/options-jungle")
def options_jungle(oi_threshold: float = 10.0, vol_threshold: float = 50.0, date: str = None):
    from api.options_jungle import get_options_jungle
    return get_options_jungle(oi_threshold, vol_threshold, date)

@app.get("/relative-strength")
def relative_strength(benchmark: str = "NIFTY"):
    from api.relative_strength import get_relative_strength
    return get_relative_strength(benchmark)

@app.get("/iv-analysis")
def iv_analysis_all(date: str = None):
    from api.iv_analysis import get_iv_analysis
    return get_iv_analysis(symbol=None, date=date)

@app.get("/iv-analysis/{symbol}")
def iv_analysis_symbol(symbol: str, date: str = None):
    from api.iv_analysis import get_iv_analysis
    return get_iv_analysis(symbol=symbol.upper(), date=date)

@app.post("/ask-claude")
async def ask_claude(request: dict):
    import os
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set in environment"}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=request.get("system", ""),
            messages=request.get("messages", []),
        )
        return {"content": response.content[0].text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/ask-context/{symbol}")
def ask_context(symbol: str = "NIFTY"):
    from api.ask_context import get_ask_context
    return get_ask_context(symbol.upper())

@app.get("/ask-context")
def ask_context_default():
    from api.ask_context import get_ask_context
    return get_ask_context("NIFTY")

@app.get("/radar-cache-clear")
def radar_cache_clear():
    from api.positional_radar import clear_radar_cache
    return clear_radar_cache()

@app.get("/oi-buildup/{symbol}")
def oi_buildup(symbol: str, days: int = 15):
    from utils.db import get_supabase
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    supabase = get_supabase()

    # Get last N+1 trading days with FUT snapshots
    hist_start = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime('%Y-%m-%d')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Fetch all FUT snapshots for this symbol in date range
    rows = supabase.from_("oi_snapshots")\
        .select("timestamp, oi, last_price, expiry")\
        .eq("symbol", symbol.upper())\
        .eq("option_type", "FUT")\
        .gte("timestamp", f"{hist_start}T00:00:00+00:00")\
        .lte("timestamp", f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .limit(50000)\
        .execute()

    if not rows.data:
        return {"symbol": symbol.upper(), "days": 0, "data": []}

    # Group by date + expiry — pick nearest active expiry per day
    date_expiry_map = defaultdict(lambda: defaultdict(list))
    for r in rows.data:
        date_str = r["timestamp"][:10]
        expiry = str(r.get("expiry") or "")
        if expiry >= date_str:  # only future/same-day expiries
            date_expiry_map[date_str][expiry].append(r)

    # For each date pick nearest expiry (smallest expiry >= that date)
    date_map = {}
    for date_str, expiry_groups in date_expiry_map.items():
        nearest = min(expiry_groups.keys())
        date_map[date_str] = expiry_groups[nearest]

    # Build daily summary — FUT OI only
    daily = []
    for date_str in sorted(date_map.keys()):
        snaps = date_map[date_str]
        first = snaps[0]
        last = snaps[-1]
        open_oi = int(first.get("oi") or 0)
        close_oi = int(last.get("oi") or 0)
        open_price = float(first.get("last_price") or 0)
        close_price = float(last.get("last_price") or 0)
        daily.append({
            "date": date_str,
            "open_oi": open_oi,
            "close_oi": close_oi,
            "open_price": open_price,
            "close_price": close_price,
        })

    def classify(oi, price):
        MIN = 0.3
        if oi > 0 and price >= MIN:  return "LONG_BUILDUP",   "Long Buildup"
        if oi > 0 and price <= -MIN: return "SHORT_BUILDUP",  "Short Buildup"
        if oi < 0 and price >= MIN:  return "SHORT_COVERING", "Short Covering"
        if oi < 0 and price <= -MIN: return "LONG_UNWINDING", "Long Unwinding"
        return "NEUTRAL", "Neutral"

    data = []
    for i in range(1, len(daily)):
        today_d = daily[i]
        prev_d = daily[i - 1]

        # FUT OI change: prev day close OI → today close OI
        prev_close_oi = prev_d["close_oi"]
        today_close_oi = today_d["close_oi"]
        oi_chg_pct = round((today_close_oi - prev_close_oi) / prev_close_oi * 100, 2) if prev_close_oi > 0 else 0

        # Price change: prev day close → today close (candle close to close)
        prev_close_price = prev_d["close_price"]
        today_close_price = today_d["close_price"]
        price_chg_pct = round((today_close_price - prev_close_price) / prev_close_price * 100, 2) if prev_close_price > 0 else 0

        sig, label = classify(oi_chg_pct, price_chg_pct)

        data.append({
            "date":          today_d["date"],
            "oi_chg_pct":    oi_chg_pct,
            "price_chg_pct": price_chg_pct,
            "close_price":   today_close_price,
            "close_oi":      today_close_oi,
            "signal":        sig,
            "label":         label,
        })

    # Filter to current series only — avoids rollover confusion
    try:
        from api.positional_radar import get_monthly_expiry, get_series_start
        import datetime as _dt
        _today = _dt.date.today()
        _expiry = get_monthly_expiry(_today.year, _today.month)
        _series_start = get_series_start(_expiry)
        data = [d for d in data if d["date"] > _series_start]
    except Exception as e:
        print(f"[OI Buildup] Series filter failed: {e}")
        data = data[-days:]

    return {"symbol": symbol.upper(), "days": len(data), "data": data}

@app.get("/positional-volume-alert")
def positional_volume_alert():
    from utils.db import get_supabase
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    supabase = get_supabase()

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get last 21 days of daily OI summary for all symbols
    hist_start = (datetime.now(timezone.utc) - timedelta(days=35)).strftime('%Y-%m-%d')
    rows = supabase.from_("daily_oi_summary")\
        .select("symbol, trade_date, total_volume, fut_vol, oi_chg_pct, price_chg_pct, close_price")\
        .gte("trade_date", hist_start)\
        .order("trade_date", desc=False)\
        .limit(10000)\
        .execute()

    # Group by symbol
    sym_rows = defaultdict(list)
    for r in (rows.data or []):
        sym_rows[r["symbol"]].append(r)

    # Get today's positional radar for context
    try:
        from api.positional_radar import get_positional_radar
        radar_data = get_positional_radar(min_consec=0)
        radar_map = {r["symbol"]: r for r in radar_data.get("results", [])}
    except:
        radar_map = {}

    alerts = []
    for sym, sym_data in sym_rows.items():
        # Sort by date
        sym_data = sorted(sym_data, key=lambda x: x["trade_date"])

        # Need at least 21 days for 20D avg
        if len(sym_data) < 5:
            continue

        today_row = sym_data[-1]  # last available trading day
        today_vol = int(today_row.get("fut_vol") or today_row.get("total_volume") or 0)
        if today_vol == 0:
            continue

        # Historical rows (exclude today)
        hist = sym_data[:-1]
        hist_vols = [int(r.get("fut_vol") or r.get("total_volume") or 0) for r in hist if (r.get("fut_vol") or r.get("total_volume") or 0) > 0]

        if len(hist_vols) < 5:
            continue

        avg_10d = sum(hist_vols[-10:]) / len(hist_vols[-10:]) if len(hist_vols) >= 10 else None
        avg_20d = sum(hist_vols[-20:]) / len(hist_vols[-20:]) if len(hist_vols) >= 20 else None

        # Check thresholds
        ratio_10d = round(today_vol / avg_10d, 2) if avg_10d and avg_10d > 0 else 0
        ratio_20d = round(today_vol / avg_20d, 2) if avg_20d and avg_20d > 0 else 0

        # Must exceed 1.5x either avg
        if ratio_10d < 1.5 and ratio_20d < 1.5:
            continue

        # OI confirmation — must have meaningful OI change
        oi_chg = float(today_row.get("oi_chg_pct") or 0)
        if abs(oi_chg) < 2.0:
            continue

        # Tier
        best_ratio = max(ratio_10d, ratio_20d)
        if ratio_20d >= 2.0:
            tier = "🔴 Elite"
            tier_color = "RED"
        elif ratio_20d >= 1.5:
            tier = "🟠 Strong"
            tier_color = "ORANGE"
        elif ratio_10d >= 2.0:
            tier = "🟡 Notable"
            tier_color = "AMBER"
        else:
            tier = "⚪ Watch"
            tier_color = "GRAY"

        # Radar context
        radar = radar_map.get(sym, {})

        alerts.append({
            "symbol":           sym,
            "today_vol":        today_vol,
            "avg_10d":          round(avg_10d) if avg_10d else None,
            "avg_20d":          round(avg_20d) if avg_20d else None,
            "ratio_10d":        ratio_10d,
            "ratio_20d":        ratio_20d,
            "best_ratio":       best_ratio,
            "oi_chg_pct":       round(oi_chg, 2),
            "price_chg_pct":    round(float(today_row.get("price_chg_pct") or 0), 2),
            "close_price":      float(today_row.get("close_price") or 0),
            "tier":             tier,
            "tier_color":       tier_color,
            # Radar context
            "in_radar":         sym in radar_map,
            "radar_signal":     radar.get("signal"),
            "radar_bias":       radar.get("bias"),
            "radar_conviction": radar.get("conviction_label"),
            "radar_consec":     radar.get("consec_days"),
            "radar_consistency": radar.get("consistency_pct"),
        })

    # Sort: radar stocks first, then by best ratio
    alerts.sort(key=lambda x: (-int(x["in_radar"]), -x["best_ratio"]))

    return {
        "date":   today,
        "total":  len(alerts),
        "alerts": alerts
    }

@app.get("/watch-today")
def watch_today():
    from api.watch_today import get_watch_today
    from utils.db import get_supabase
    return get_watch_today(get_supabase())

@app.get("/positional-radar")
def positional_radar(min_consec: int = 0):
    from api.positional_radar import get_positional_radar
    return get_positional_radar(min_consec=min_consec)

@app.get("/oi-profile/{symbol}")
def oi_profile(symbol: str, date: str = None, expiry: str = None):
    from api.oi_profile import get_oi_profile
    return get_oi_profile(symbol=symbol, date=date, expiry=expiry)

@app.get("/vacuum-scanner")
def vacuum_scanner(max_distance_pct: float = 10.0):
    from api.vacuum_scanner import get_vacuum_scanner
    return get_vacuum_scanner(max_distance_pct=max_distance_pct)

@app.get("/cpr-scanner")
def cpr_scanner():
    from api.cpr import get_cpr_scanner
    return get_cpr_scanner()
    
@app.get("/cpr-scanner-timeframe")
def cpr_scanner_timeframe(timeframe: str = "daily"):
    from api.cpr import get_cpr_scanner_timeframe
    return get_cpr_scanner_timeframe(timeframe)

@app.get("/cpr-compute-weekly")
def cpr_compute_weekly(trade_date: str = None):
    from api.cpr import compute_and_store_weekly_monthly_cpr
    return compute_and_store_weekly_monthly_cpr(trade_date=trade_date)

@app.get("/participant-flow")
def participant_flow_endpoint(days: int = 20):
    from api.participant_flow import get_participant_flow
    return get_participant_flow(days)

@app.get("/participant-flow/fetch")
def participant_flow_fetch(trade_date: str = None):
    from api.participant_flow import fetch_and_store_participant_flow
    return fetch_and_store_participant_flow(trade_date)

@app.get("/participant-flow/backfill")
def participant_flow_backfill(days: int = 20):
    from api.participant_flow import fetch_and_store_participant_flow
    from datetime import date, timedelta
    results = []
    d = date.today() - timedelta(days=1)
    fetched = 0
    attempts = 0
    while fetched < days and attempts < 40:
        if d.weekday() < 5:
            result = fetch_and_store_participant_flow(d.isoformat())
            results.append({"date": d.isoformat(), **result})
            if "stored" in result:
                fetched += 1
        d -= timedelta(days=1)
        attempts += 1
    return {"fetched": fetched, "results": results}
    
@app.get("/cpr-compute")
def cpr_compute(trade_date: str = None):
    from api.cpr import compute_and_store_cpr
    return compute_and_store_cpr(trade_date=trade_date)

@app.get("/signal-log")
def signal_log(date: str = None, symbol: str = None):
    from api.signal_log import get_signal_log
    return get_signal_log(date)

@app.get("/stealth-buildup")
def stealth_buildup():
    from utils.db import get_supabase
    from collections import defaultdict
    import datetime as _dt
    import pytz

    supabase = get_supabase()
    IST = pytz.timezone("Asia/Kolkata")

    # ── Get last trading day ──────────────────────────────────────────────
    today = _dt.datetime.now(IST).date()
    check = today
    for _ in range(7):
        if check.weekday() < 5:
            break
        check -= _dt.timedelta(days=1)
    last_trading_day = check.isoformat()

    # ── Get series start ──────────────────────────────────────────────────
    from api.positional_radar import get_monthly_expiry, get_series_start
    expiry = get_monthly_expiry(today.year, today.month)
    series_start = get_series_start(expiry)

    # ── Fetch last 15 days of FUT OI change from daily_oi_summary ────────
    hist_start = (check - _dt.timedelta(days=25)).isoformat()
    hist_res = supabase.from_("daily_oi_summary")\
        .select("symbol, trade_date, fut_oi_chg_pct, close_price")\
        .gte("trade_date", hist_start)\
        .lte("trade_date", last_trading_day)\
        .gt("fut_oi_chg_pct", 0)\
        .order("trade_date", desc=False)\
        .limit(10000)\
        .execute()

    # Group by symbol — get last 15 trading days
    from collections import defaultdict
    sym_history = defaultdict(list)
    for r in (hist_res.data or []):
        sym_history[r["symbol"]].append({
            "date": r["trade_date"],
            "fut_oi_chg_pct": float(r.get("fut_oi_chg_pct") or 0),
            "close_price": float(r.get("close_price") or 0),
        })

    # ── Get today's price change ──────────────────────────────────────────
    price_res = supabase.from_("daily_oi_summary")\
        .select("symbol, close_price, price_chg_pct")\
        .eq("trade_date", last_trading_day)\
        .limit(200)\
        .execute()
    price_map = {r["symbol"]: r for r in (price_res.data or [])}

    # ── Get CMP for ATM calculation ───────────────────────────────────────
    cmp_res = supabase.from_("cmp_prices")\
        .select("symbol, cmp")\
        .gte("timestamp", f"{last_trading_day}T00:00:00+00:00")\
        .lte("timestamp", f"{last_trading_day}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(500)\
        .execute()
    cmp_map = {}
    seen = set()
    for r in (cmp_res.data or []):
        if r["symbol"] not in seen:
            cmp_map[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

    # ── Fetch ATM±5 strikes CE/PE OI for last trading day ────────────────
    STRIKE_INTERVALS = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}

    oi_res = supabase.from_("oi_snapshots")\
        .select("symbol, strike, option_type, oi, expiry")\
        .in_("option_type", ["CE", "PE"])\
        .gte("timestamp", f"{last_trading_day}T09:50:00+00:00")\
        .lte("timestamp", f"{last_trading_day}T10:05:00+00:00")\
        .limit(50000)\
        .execute()

    # Group CE/PE OI by symbol → strike
    sym_strikes = defaultdict(lambda: defaultdict(lambda: {"ce_oi": 0, "pe_oi": 0}))
    sym_expiry = {}
    for r in (oi_res.data or []):
        sym = r["symbol"]
        exp = str(r.get("expiry") or "")
        if not exp or exp < last_trading_day:
            continue
        if sym not in sym_expiry or exp < sym_expiry[sym]:
            sym_expiry[sym] = exp
    for r in (oi_res.data or []):
        sym = r["symbol"]
        if str(r.get("expiry") or "") != sym_expiry.get(sym, ""):
            continue
        strike = float(r["strike"])
        ot = r["option_type"]
        oi = int(r.get("oi") or 0)
        sym_strikes[sym][strike][f"{ot.lower()}_oi"] += oi

    # Compute Net Delta (ATM±5 strikes only)
    def compute_net_delta(sym, cmp):
        if cmp <= 0 or sym not in sym_strikes:
            return None, 0, 0
        strikes_data = sym_strikes[sym]
        interval = STRIKE_INTERVALS.get(sym, None)
        if not interval:
            strikes_sorted = sorted(strikes_data.keys())
            if len(strikes_sorted) >= 2:
                diffs = [strikes_sorted[i+1]-strikes_sorted[i] for i in range(min(5, len(strikes_sorted)-1))]
                valid = [d for d in diffs if d > 0]
                interval = min(valid) if valid else 5
            else:
                interval = 5
        snapped_atm = round(cmp / interval) * interval
        lower = snapped_atm - (5 * interval)
        upper = snapped_atm + (5 * interval)
        ce_total = 0
        pe_total = 0
        for strike, v in strikes_data.items():
            if lower <= strike <= upper:
                ce_total += v.get("ce_oi", 0)
                pe_total += v.get("pe_oi", 0)
        net_delta = pe_total - ce_total
        return net_delta, pe_total, ce_total

    # ── Score each symbol ─────────────────────────────────────────────────
    results = []
    for sym, history in sym_history.items():
        if len(history) < 3:
            continue

        # Get last 15 days OI values
        last_15 = history[-15:]
        today_data = next((h for h in reversed(last_15) if h["date"] == last_trading_day), None)
        if not today_data:
            continue

        today_oi_chg = today_data["fut_oi_chg_pct"]
        if today_oi_chg <= 0:
            continue

        # Rank today's OI vs last 15 days
        all_oi_values = sorted([h["fut_oi_chg_pct"] for h in last_15], reverse=True)
        rank = all_oi_values.index(today_oi_chg) + 1 if today_oi_chg in all_oi_values else 99

        # Price change today
        price_data = price_map.get(sym, {})
        price_chg = float(price_data.get("price_chg_pct") or 0)
        close_price = float(price_data.get("close_price") or 0)
        cmp = cmp_map.get(sym, close_price)

        # Net Delta (ATM±5)
        net_delta, pe_oi, ce_oi = compute_net_delta(sym, cmp)
        net_delta_L = round(net_delta / 100000, 2) if net_delta is not None else None
        net_delta_bullish = net_delta is not None and net_delta > 0

        # Classify tier
        abs_price = abs(price_chg)
        if rank <= 3 and abs_price <= 0.5 and net_delta_bullish:
            tier = "ELITE"
            tier_label = "🥇 Elite"
            tier_color = "GOLD"
        elif rank <= 3 and abs_price <= 1.0:
            tier = "STRONG"
            tier_label = "🥈 Strong"
            tier_color = "SILVER"
        elif rank <= 5 and abs_price <= 1.5:
            tier = "WATCH"
            tier_label = "🥉 Watch"
            tier_color = "BRONZE"
        else:
            continue

        results.append({
            "symbol":           sym,
            "tier":             tier,
            "tier_label":       tier_label,
            "tier_color":       tier_color,
            "rank":             rank,
            "total_days":       len(last_15),
            "fut_oi_chg_pct":   round(today_oi_chg, 2),
            "price_chg_pct":    round(price_chg, 2),
            "close_price":      close_price,
            "cmp":              cmp,
            "net_delta_L":      net_delta_L,
            "net_delta_bullish": net_delta_bullish,
            "pe_oi_L":          round(pe_oi / 100000, 2),
            "ce_oi_L":          round(ce_oi / 100000, 2),
            "oi_history":       [round(h["fut_oi_chg_pct"], 2) for h in last_15],
        })

    # Sort: Elite first, then Strong, then Watch, then by OI rank
    tier_order = {"ELITE": 0, "STRONG": 1, "WATCH": 2}
    results.sort(key=lambda x: (tier_order.get(x["tier"], 3), x["rank"]))

    return {
        "date":         last_trading_day,
        "series_start": series_start,
        "expiry":       expiry,
        "total":        len(results),
        "elite":        sum(1 for r in results if r["tier"] == "ELITE"),
        "strong":       sum(1 for r in results if r["tier"] == "STRONG"),
        "watch":        sum(1 for r in results if r["tier"] == "WATCH"),
        "results":      results,
    }

@app.get("/signal-log/seed-eod")
def seed_signal_log_eod(date: str = None):
    """Manually force-compute and save EOD snapshot. Defaults to today if market closed, else yesterday."""
    from api.signal_log import _save_eod_to_supabase
    from utils.db import get_supabase
    import datetime, pytz
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.datetime.now(ist)
    
    if date:
        last_trading_day = date
    else:
        # Use today if post-market, else yesterday
        today = now.date()
        if now.hour >= 15 and now.minute >= 30 and today.weekday() < 5:
            last_trading_day = today.isoformat()
        else:
            for i in range(1, 6):
                d = today - datetime.timedelta(days=i)
                if d.weekday() < 5:
                    last_trading_day = d.isoformat()
                    break

    supabase = get_supabase()

    # Force compute by monkey-patching market hours check
    import api.signal_log as sl
    original = sl.is_market_hours
    sl.is_market_hours = lambda: True
    try:
        result = sl.get_signal_log(date=last_trading_day)
    finally:
        sl.is_market_hours = original

    if result.get("signals"):
        _save_eod_to_supabase(supabase, result)
        return {"status": "saved", "date": last_trading_day, "signals": len(result["signals"])}
    return {"status": "no signals found", "date": last_trading_day, "snapshots": result.get("snapshots", 0)}
    
    if result.get("signals"):
        _save_eod_to_supabase(get_supabase(), result)
        return {"status": "saved", "date": last_trading_day, "signals": len(result["signals"])}
    return {"status": "no signals found", "date": last_trading_day}
    
@app.get("/daily-oi-summary/compute")
def trigger_daily_oi_summary(date: str = None):
    from utils.db import get_supabase
    result = compute_daily_summary(get_supabase(), trade_date=date)
    return result

@app.get("/wall-migration")
def wall_migration():
    from api.wall_migration import get_wall_migration
    from utils.db import get_supabase
    return get_wall_migration(get_supabase())
    
@app.get("/vol-oi-breakout")
async def vol_oi_breakout_endpoint():
    from api.vol_oi_breakout import get_vol_oi_breakout
    from utils.db import get_supabase
    return get_vol_oi_breakout(get_supabase())

@app.get("/oi-heatmap/{symbol}")
def oi_heatmap(symbol: str, date: str = None, expiry: str = None):
    from api.oi_heatmap import get_oi_heatmap
    return get_oi_heatmap(symbol=symbol, date=date, expiry=expiry)

@app.get("/debug-cpr-candle/{symbol}")
def debug_cpr_candle(symbol: str):
    from services.kite_auth import get_kite_client
    from datetime import datetime, timedelta
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    from_date = (today - timedelta(days=5)).isoformat()
    to_date = today.isoformat()
    kite = get_kite_client()
    instruments = kite.instruments("NSE")
    token = None
    for inst in instruments:
        if inst["tradingsymbol"] == symbol.upper():
            token = inst["instrument_token"]
            break
    if not token:
        return {"error": "token not found"}
    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_date,
        to_date=to_date,
        interval="day",
        continuous=False,
        oi=False,
    )
    return {
        "today_ist": today.isoformat(),
        "candles": [
            {
                "date": str(c["date"]),
                "date_type": type(c["date"]).__name__,
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
            for c in candles
        ]
    }

def keepalive_ping():
    try:
        import requests
        requests.get("https://greeknova-backend-production.up.railway.app/health", timeout=5)
        print("💓 Keepalive ping sent")
    except Exception as e:
        print(f"⚠️ Keepalive failed: {e}")

    # ── Token health check every 30 mins ──────────────────────────────────────
    try:
        from services.kite_auth import check_and_refresh_token
        check_and_refresh_token()
    except Exception as e:
        print(f"⚠️ Token health check error: {e}")

@app.get("/oi-walls/{symbol}")
def oi_walls_detail(symbol: str):
    from utils.db import get_supabase
    from datetime import datetime, timedelta
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    check = datetime.now(ist).date()
    for _ in range(5):
        if check.weekday() < 5:
            break
        check -= timedelta(days=1)
    today = check.isoformat()
    supabase = get_supabase()

    # Latest snapshot timestamp
    latest = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol.upper())\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()
    if not latest.data:
        return {"symbol": symbol, "strikes": [], "cmp": 0}

    ts = latest.data[0]["timestamp"]

    # Latest CMP
    cmp_row = supabase.from_("cmp_prices")\
        .select("cmp")\
        .eq("symbol", symbol.upper())\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()
    cmp = float(cmp_row.data[0]["cmp"]) if cmp_row.data else 0

    # Strike OI within 15% of CMP
    rows = supabase.from_("oi_snapshots")\
        .select("strike, option_type, oi, last_price")\
        .eq("symbol", symbol.upper())\
        .eq("timestamp", ts)\
        .in_("option_type", ["CE", "PE"])\
        .limit(2000).execute()

    strike_map: dict = {}
    for r in (rows.data or []):
        s = float(r["strike"])
        if cmp > 0 and abs(s - cmp) / cmp > 0.15:
            continue
        if s not in strike_map:
            strike_map[s] = {"strike": s, "ce_oi": 0, "pe_oi": 0,
                             "ce_ltp": 0, "pe_ltp": 0}
        if r["option_type"] == "CE":
            strike_map[s]["ce_oi"] += int(r["oi"] or 0)
            strike_map[s]["ce_ltp"] = float(r["last_price"] or 0)
        else:
            strike_map[s]["pe_oi"] += int(r["oi"] or 0)
            strike_map[s]["pe_ltp"] = float(r["last_price"] or 0)

    strikes = sorted(strike_map.values(), key=lambda x: x["strike"], reverse=True)
    if not strikes:
        return {"symbol": symbol.upper(), "strikes": [], "cmp": cmp}

    # ── Max OI walls — highest OI anywhere in range ───────────────────────
    ce_candidates = [s for s in strikes if s["strike"] > cmp]
    pe_candidates = [s for s in strikes if s["strike"] < cmp]

    # Fallback if no strikes above/below
    if not ce_candidates: ce_candidates = strikes
    if not pe_candidates: pe_candidates = strikes

    ce_wall = max(ce_candidates, key=lambda x: x["ce_oi"])["strike"]
    pe_wall = max(pe_candidates, key=lambda x: x["pe_oi"])["strike"]
    ce_wall_oi = max(ce_candidates, key=lambda x: x["ce_oi"])["ce_oi"]
    pe_wall_oi = max(pe_candidates, key=lambda x: x["pe_oi"])["pe_oi"]

    # ── Intraday walls — nearest significant strike above/below CMP ───────
    # Significant = OI >= 10% of max OI in that direction
    ce_threshold = ce_wall_oi * 0.10
    pe_threshold = pe_wall_oi * 0.10

    ce_significant = [s for s in ce_candidates if s["ce_oi"] >= ce_threshold]
    pe_significant = [s for s in pe_candidates if s["pe_oi"] >= pe_threshold]

    # Nearest = smallest distance from CMP
    intraday_ce = min(ce_significant, key=lambda x: x["strike"])["strike"] if ce_significant else ce_wall
    intraday_pe = max(pe_significant, key=lambda x: x["strike"])["strike"] if pe_significant else pe_wall

    # ── Trade range ───────────────────────────────────────────────────────
    intraday_range = round(abs(intraday_ce - intraday_pe), 1)
    intraday_range_pct = round(intraday_range / cmp * 100, 1) if cmp > 0 else 0
    maxoi_range = round(abs(ce_wall - pe_wall), 1)
    maxoi_range_pct = round(maxoi_range / cmp * 100, 1) if cmp > 0 else 0

    return {
        "symbol":              symbol.upper(),
        "cmp":                 cmp,
        "strikes":             strikes,
        # Max OI walls — highest committed OI (positional/writing context)
        "ce_wall":             ce_wall,
        "pe_wall":             pe_wall,
        "ce_wall_oi_L":        round(ce_wall_oi / 100000, 2),
        "pe_wall_oi_L":        round(pe_wall_oi / 100000, 2),
        "trade_range":         maxoi_range,
        "trade_range_pct":     maxoi_range_pct,
        # Intraday walls — nearest significant strike (intraday context)
        "intraday_ce_wall":    intraday_ce,
        "intraday_pe_wall":    intraday_pe,
        "intraday_range":      intraday_range,
        "intraday_range_pct":  intraday_range_pct,
    }

# ── CommodityNova routes ───────────────────────────────────────────────────
app.include_router(mcx_router, prefix="/mcx", tags=["MCX"])
app.include_router(mcx_oi_map_router, prefix="/mcx", tags=["MCX"])
# ──────────────────────────────────────────────────────────────────────────
