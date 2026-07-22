import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
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
    # Week 1 additions — Jun 15
    "PAYTM","NYKAA","PERSISTENT","DIXON",
    # Jul 2026 additions
    "BSE","MCX","TMPV","GODREJPROP","DIVISLAB","COFORGE","ANGELONE","CDSL","OIL",
    # Jul 18 2026 expansion — added to backfill.py/positional_radar.py/iv_analysis.py
    # but missed here, capping live capture at 80 symbols instead of 100
    # Jul 20 2026: ZOMATO renamed to ETERNAL on NSE; ESCORTS, IRCTC, LTIM, UBL
    # removed — no live F&O contracts found under these tickers on Kite
    "TVSMOTOR","BHARATFORG","MOTHERSON","LUPIN","TORNTPHARM","AUROPHARMA",
    "GODREJCP","MARICO","DABUR","PIDILITIND","MUTHOOTFIN","SBICARD","ICICIPRULI",
    "IDFCFIRSTB","FEDERALBNK","ETERNAL","POLYCAB","VOLTAS","IEX","ASTRAL",
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
    "PAYTM":"NSE:PAYTM","NYKAA":"NSE:NYKAA",
    "PERSISTENT":"NSE:PERSISTENT","DIXON":"NSE:DIXON",
    "BSE":"NSE:BSE","MCX":"NSE:MCX","TMPV":"NSE:TMPV",
    "GODREJPROP":"NSE:GODREJPROP","DIVISLAB":"NSE:DIVISLAB","COFORGE":"NSE:COFORGE",
    "ANGELONE":"NSE:ANGELONE","CDSL":"NSE:CDSL","OIL":"NSE:OIL",
    "TVSMOTOR":"NSE:TVSMOTOR","BHARATFORG":"NSE:BHARATFORG","MOTHERSON":"NSE:MOTHERSON",
    "LUPIN":"NSE:LUPIN","TORNTPHARM":"NSE:TORNTPHARM",
    "AUROPHARMA":"NSE:AUROPHARMA","GODREJCP":"NSE:GODREJCP","MARICO":"NSE:MARICO",
    "DABUR":"NSE:DABUR","PIDILITIND":"NSE:PIDILITIND",
    "MUTHOOTFIN":"NSE:MUTHOOTFIN","SBICARD":"NSE:SBICARD","ICICIPRULI":"NSE:ICICIPRULI",
    "IDFCFIRSTB":"NSE:IDFCFIRSTB","FEDERALBNK":"NSE:FEDERALBNK","ETERNAL":"NSE:ETERNAL",
    "POLYCAB":"NSE:POLYCAB","VOLTAS":"NSE:VOLTAS","IEX":"NSE:IEX",
    "ASTRAL":"NSE:ASTRAL",
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
    from utils.market_calendar import is_trading_day
    if not is_trading_day(now.date()): return
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

def fetch_delivery_data():
    """Fetch today's delivery data from NSE bhav copy after market close."""
    import pytz, zipfile, io, requests
    from datetime import datetime
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    if today.weekday() >= 5:
        return
    from utils.market_calendar import is_trading_day
    if not is_trading_day(today):
        return
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        # Check if already fetched
        # Note: removed early-exit-if-exists check — upsert is safe to re-run,
        # and this was blocking legitimate refetches when a partial/stale row existed.
        SYMBOLS = ["RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN","SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH","TATACONSUM","TATASTEEL","ADANIENT","POWERGRID","NTPC","ONGC","JSWSTEEL","COALINDIA","BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA","DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE","INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT","ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS","HAL","INDIGO","PFC","RECLTD","SAIL","TATAPOWER","VEDL","PAYTM","NYKAA","PERSISTENT","DIXON","BSE","MCX","TMPV","LTIM","GODREJPROP","DIVISLAB","COFORGE","ANGELONE","CDSL","OIL","TVSMOTOR","BHARATFORG","MOTHERSON","ESCORTS","LUPIN","TORNTPHARM","AUROPHARMA","GODREJCP","MARICO","DABUR","PIDILITIND","UBL","MUTHOOTFIN","SBICARD","ICICIPRULI","IDFCFIRSTB","FEDERALBNK","ZOMATO","POLYCAB","VOLTAS","IRCTC","IEX","ASTRAL"]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Referer": "https://www.nseindia.com/",
        }
        date_str = today.strftime('%d%m%Y')
        # Correct NSE report: "Full Bhav Data" — the older CM bhavcopy URL had NO
        # delivery columns at all (it's the F&O-style unified file), which caused
        # every fetch to silently fail via a blank StopIteration error. This URL
        # is the genuine cash-market report that actually contains DELIV_QTY/DELIV_PER.
        url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            print(f"[Delivery] HTTP {res.status_code} for {today}")
            return
        content = res.text
        lines = content.strip().split('\n')
        header = [h.strip() for h in lines[0].split(',')]
        sym_idx = next((i for i,h in enumerate(header) if h == 'SYMBOL'), None)
        trd_idx = next((i for i,h in enumerate(header) if h == 'TTL_TRD_QNTY'), None)
        del_idx = next((i for i,h in enumerate(header) if h == 'DELIV_QTY'), None)
        del_pct_idx = next((i for i,h in enumerate(header) if h == 'DELIV_PER'), None)
        series_idx = next((i for i,h in enumerate(header) if h == 'SERIES'), None)
        if sym_idx is None or trd_idx is None or del_idx is None:
            print(f"[Delivery] Column not found in header: {header}")
            return
        records = []
        for line in lines[1:]:
            parts = [p.strip().strip('"') for p in line.split(',')]
            if len(parts) <= max(sym_idx, trd_idx, del_idx): continue
            sym = parts[sym_idx].upper()
            if sym not in SYMBOLS: continue
            if series_idx and parts[series_idx].strip() not in ('EQ','BE','BZ'): continue
            try:
                traded = int(float(parts[trd_idx] or 0))
                deliv = int(float(parts[del_idx] or 0))
                pct = float(parts[del_pct_idx] or 0) if del_pct_idx else (round(deliv/traded*100,2) if traded>0 else 0)
                records.append({"trade_date": today.isoformat(), "symbol": sym, "traded_qty": traded, "deliverable_qty": deliv, "delivery_pct": pct})
            except: continue
        if records:
            for i in range(0, len(records), 100):
                supabase.from_("delivery_data").upsert(records[i:i+100]).execute()
            print(f"[Delivery] ✅ {today} — {len(records)} stocks saved")
        else:
            print(f"[Delivery] ⚠️ {today} — no matching symbols")
    except Exception as e:
        print(f"[Delivery] ❌ {e}")

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
    def _run_push_checks_job():
        try:
            from services.push_checker import run_push_checks
            from utils.db import get_supabase
            from datetime import datetime as _dt
            import pytz as _pytz
            _ist = _pytz.timezone("Asia/Kolkata")
            _now = _dt.now(_ist)
            _is_mkt = _now.weekday() < 5 and (9*60+15) <= (_now.hour*60+_now.minute) <= (15*60+30)
            if _is_mkt:
                run_push_checks(get_supabase())
        except Exception as e:
            print(f"[Push] Scheduled check failed: {e}")
    scheduler.add_job(_run_push_checks_job, "interval", minutes=5, id="push_checks")
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
    scheduler.add_job(
        watchdog_archive,
        "cron", day_of_week="mon", hour=9, minute=0, timezone="Asia/Kolkata", id="archive_watchdog",
        misfire_grace_time=600
    )
    scheduler.add_job(
        lambda: __import__('api.positional_radar', fromlist=['clear_radar_cache']).clear_radar_cache(),
        "cron", hour=9, minute=0, timezone="Asia/Kolkata", id="daily_radar_cache_clear",
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
    for retry_hour in [21, 22, 23, 0]:
        scheduler.add_job(
            watchdog_participant_flow,
            "cron", hour=retry_hour, minute=0, timezone="Asia/Kolkata",
            id=f"participant_flow_retry_{retry_hour}",
            misfire_grace_time=600
        )

    scheduler.add_job(
        fetch_delivery_data,
        "cron", hour=18, minute=30, timezone="Asia/Kolkata", id="delivery_data_fetch",
        misfire_grace_time=600
    )
    for _delivery_hour in [19, 20, 21, 22, 23]:
        scheduler.add_job(
            fetch_delivery_data,
            "cron", hour=_delivery_hour, minute=0, timezone="Asia/Kolkata",
            id=f"delivery_data_retry_{_delivery_hour}",
            misfire_grace_time=600
        )

    # ── CommodityNova scheduler ────────────────────────────────────────────
    try:
        from services.kite_auth import get_kite_client
        from utils.db import get_supabase
        kite = get_kite_client()
        supabase = get_supabase()
        start_mcx_scheduler(kite, supabase)
    except Exception as e:
        print(f"⚠️ CommodityNova scheduler skipped — no Kite token: {e}")
        print("⚠️ Will retry captures using Supabase token once available")
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

@app.get("/writer-buyer-score/{symbol}")
def writer_buyer_score(symbol: str):
    from utils.db import get_supabase
    from collections import defaultdict
    import datetime as _dt
    import pytz

    supabase = get_supabase()
    IST = pytz.timezone("Asia/Kolkata")
    sym = symbol.upper()

    today = _dt.datetime.now(IST).date()
    check = today
    for _ in range(7):
        if check.weekday() < 5:
            break
        check -= _dt.timedelta(days=1)
    last_trading_day = check.isoformat()

    # ── 1. Fetch ATM±10 strikes OI (EOD snapshot) ─────────────────────────
    from api.positional_radar import get_monthly_expiry, get_series_start
    expiry = get_monthly_expiry(today.year, today.month)

    cmp_res = supabase.from_("cmp_prices")\
        .select("cmp")\
        .eq("symbol", sym)\
        .gte("timestamp", f"{last_trading_day}T00:00:00+00:00")\
        .lte("timestamp", f"{last_trading_day}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    cmp = float(cmp_res.data[0]["cmp"]) if cmp_res.data else 0
    if cmp <= 0:
        return {"symbol": sym, "error": "No CMP data"}

    # Auto detect interval
    oi_res = supabase.from_("oi_snapshots")\
        .select("strike, option_type, oi")\
        .eq("symbol", sym)\
        .eq("expiry", expiry)\
        .in_("option_type", ["CE", "PE"])\
        .gte("timestamp", f"{last_trading_day}T09:50:00+00:00")\
        .lte("timestamp", f"{last_trading_day}T10:05:00+00:00")\
        .limit(10000).execute()

    if not oi_res.data:
        return {"symbol": sym, "error": "No OI snapshot data"}

    strikes_list = sorted(set(float(r["strike"]) for r in oi_res.data))
    if len(strikes_list) >= 2:
        diffs = [strikes_list[i+1]-strikes_list[i] for i in range(min(5, len(strikes_list)-1))]
        interval = min(d for d in diffs if d > 0)
    else:
        interval = 5

    snapped_atm = round(cmp / interval) * interval

    # Compute ATM±3 and total OI
    atm3_ce = atm3_pe = total_ce = total_pe = 0
    atm10_ce = atm10_pe = 0

    for r in oi_res.data:
        strike = float(r["strike"])
        oi = int(r.get("oi") or 0)
        ot = r["option_type"]
        dist = abs(strike - snapped_atm) / interval  # distance in strike intervals

        if ot == "CE":
            total_ce += oi
            if dist <= 10:
                atm10_ce += oi
            if dist <= 3:
                atm3_ce += oi
        else:
            total_pe += oi
            if dist <= 10:
                atm10_pe += oi
            if dist <= 3:
                atm3_pe += oi

    total_oi = total_ce + total_pe
    atm3_oi = atm3_ce + atm3_pe
    atm10_oi = atm10_ce + atm10_pe

# Component 1: OI Concentration score (0-30) — TIGHTENED
    conc_pct = round(atm3_oi / atm10_oi * 100, 1) if atm10_oi > 0 else 0
    if conc_pct >= 60:   conc_score = 30
    elif conc_pct >= 50: conc_score = 20
    elif conc_pct >= 40: conc_score = 10
    else:                conc_score = 0

    # ── 2. PCR stability (0-20) ────────────────────────────────────────────
    pcr = round(atm10_pe / atm10_ce, 2) if atm10_ce > 0 else 0
    if 0.8 <= pcr <= 1.2:   pcr_score = 20  # tight balanced = writers
    elif 0.6 <= pcr <= 1.5: pcr_score = 10  # slightly skewed
    else:                    pcr_score = 0   # heavily skewed = buyers dominating

    # ── 3. FUT vs Options DIVERGENCE (0-25) — NEW LOGIC ──────────────────
    # Writers = FUT OI and Options OI move in OPPOSITE directions
    # (e.g. FUT longs building while CE OI also building = institutions
    #  writing calls against their FUT longs = writer behavior)
    # Buyers = Options OI spikes without FUT confirmation
    hist_res = supabase.from_("daily_oi_summary")\
        .select("trade_date, fut_oi_chg_pct, oi_chg_pct, price_chg_pct")\
        .eq("symbol", sym)\
        .gte("trade_date", (check - _dt.timedelta(days=10)).isoformat())\
        .lte("trade_date", last_trading_day)\
        .order("trade_date", desc=False)\
        .limit(10).execute()

    hist_rows = hist_res.data or []

    # Writer signal: FUT OI meaningful + options OI also building (covered writing)
    # OR FUT OI meaningful + options OI shrinking (directional FUT only)
    writer_days = 0
    buyer_days = 0
    for r in hist_rows:
        fut_chg = float(r.get("fut_oi_chg_pct") or 0)
        opt_chg = float(r.get("oi_chg_pct") or 0)
        # Writer: FUT has meaningful position change (>1%) = institutional
        if abs(fut_chg) >= 1.0:
            writer_days += 1
        # Buyer: Options OI spikes (>5%) with minimal FUT activity (<0.5%)
        elif abs(opt_chg) >= 5.0 and abs(fut_chg) < 0.5:
            buyer_days += 1

    total_days = len(hist_rows)
    writer_pct = writer_days / total_days * 100 if total_days > 0 else 0
    if writer_pct >= 70:   fut_score = 25
    elif writer_pct >= 50: fut_score = 15
    elif writer_pct >= 30: fut_score = 5
    else:                  fut_score = 0

    # ── 4. OI consistency (0-25) — TIGHTENED ─────────────────────────────
    directions = []
    for r in hist_rows:
        fut_chg = float(r.get("fut_oi_chg_pct") or 0)
        price_chg = float(r.get("price_chg_pct") or 0)
        if abs(fut_chg) > 0.5:
            if fut_chg > 0 and price_chg >= 0.3:    directions.append("LONG")
            elif fut_chg > 0 and price_chg <= -0.3: directions.append("SHORT")
            elif fut_chg < 0 and price_chg >= 0.3:  directions.append("COVER")
            elif fut_chg < 0 and price_chg <= -0.3: directions.append("UNWIND")
            else:                                    directions.append("NEUTRAL")

    if directions:
        most_common = max(set(directions), key=directions.count)
        consistency_pct = directions.count(most_common) / len(directions) * 100
        if consistency_pct >= 75:   consec_score = 25  # very consistent
        elif consistency_pct >= 65: consec_score = 15
        elif consistency_pct >= 55: consec_score = 8
        else:                       consec_score = 0   # mixed = not writer
    else:
        consec_score = 0
        consistency_pct = 0
        most_common = "NEUTRAL"

    # Update fut_align note for breakdown
    fut_align_days = writer_days

    # ── Final score ────────────────────────────────────────────────────────
    total_score = conc_score + pcr_score + fut_score + consec_score

    if total_score >= 70:
        verdict = "WRITER_DOMINATED"
        verdict_label = "✍️ Writer Dominated"
        verdict_color = "EMERALD"
        verdict_note = "Institutional writers setting the range — high conviction positioning"
    elif total_score >= 45:
        verdict = "MIXED"
        verdict_label = "⚖️ Mixed Activity"
        verdict_color = "AMBER"
        verdict_note = "Both writers and buyers active — wait for clarity"
    else:
        verdict = "BUYER_DOMINATED"
        verdict_label = "🎯 Buyer Dominated"
        verdict_color = "RED"
        verdict_note = "Speculative/event-driven activity — options being bought not written"

    return {
        "symbol":            sym,
        "date":              last_trading_day,
        "cmp":               cmp,
        "score":             total_score,
        "verdict":           verdict,
        "verdict_label":     verdict_label,
        "verdict_color":     verdict_color,
        "verdict_note":      verdict_note,
        "breakdown": {
            "concentration": {
                "score":      conc_score,
                "max":        30,
                "atm3_pct":   conc_pct,
                "note":       f"ATM±3 = {conc_pct}% of ATM±10 OI"
            },
            "pcr_stability": {
                "score":      pcr_score,
                "max":        20,
                "pcr":        pcr,
                "note":       f"PCR {pcr} — {'balanced' if 0.7 <= pcr <= 1.3 else 'skewed'}"
            },
            "fut_alignment": {
                "score":      fut_score,
                "max":        25,
                "aligned_days": writer_days,
                "note":       f"FUT meaningful activity {writer_days} of {total_days} days ({round(writer_pct)}%)"
            },
            "oi_consistency": {
                "score":      consec_score,
                "max":        25,
                "consistency_pct": round(consistency_pct, 1),
                "dominant":   most_common,
                "note":       f"{most_common} signal {round(consistency_pct,1)}% of days"
            }
        },
        "atm_data": {
            "atm_strike":  snapped_atm,
            "interval":    interval,
            "atm3_ce_L":   round(atm3_ce/100000, 2),
            "atm3_pe_L":   round(atm3_pe/100000, 2),
            "atm10_ce_L":  round(atm10_ce/100000, 2),
            "atm10_pe_L":  round(atm10_pe/100000, 2),
            "pcr":         pcr,
        }
    }

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

    Processes ONE DAY PER CALL (archive_single_day_oi_snapshots) instead of
    looping through every old day inside a single giant transaction — the
    old approach hit Postgres's statement timeout once there was more than
    about a week of backlog (e.g. after this job silently missed a few runs
    due to Railway container restarts wiping the in-memory weekly scheduler
    slot), which rolled back the ENTIRE multi-week batch every time, so nothing
    ever got archived. Per-day calls are small and fast, and one bad day can't
    block the rest.
    """
    from utils.db import get_supabase
    from datetime import datetime, timedelta
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    cutoff_date = (datetime.now(ist) - timedelta(days=7)).date()
    supabase = get_supabase()

    try:
        # Avoid asking Postgres to scan all 12M+ rows to find distinct old
        # days (that scan itself was timing out) — just get the earliest
        # timestamp (cheap, index-backed) and generate the day range in
        # Python instead. archive_single_day_oi_snapshots() safely no-ops
        # for any day that has no data, so calling it for every calendar
        # day in the range (not just ones we've confirmed have data) is fine.
        earliest_res = supabase.from_("oi_snapshots").select("timestamp").order("timestamp", desc=False).limit(1).execute()
        if not earliest_res.data:
            print("[ARCHIVE] No data in oi_snapshots — nothing to do")
            return
        earliest_date = datetime.fromisoformat(earliest_res.data[0]["timestamp"].replace("Z", "+00:00")).date()

        old_days = []
        d = earliest_date
        while d < cutoff_date:
            old_days.append(d.isoformat())
            d += timedelta(days=1)

        print(f"[ARCHIVE] Starting archive — {len(old_days)} calendar day(s) from {earliest_date} to {cutoff_date}")

        # Chunk each day into small 2-hour windows instead of one call per
        # day — the biggest days (~1M rows) were still occasionally timing
        # out even with the proper index, since a single INSERT+DELETE over
        # that much data can just genuinely take a while. Small chunks stay
        # comfortably within any statement timeout regardless of day size.
        succeeded_chunks = 0
        failed_chunks = 0
        for day in old_days:
            day_dt = datetime.fromisoformat(day)
            day_start = ist.localize(datetime.combine(day_dt, datetime.min.time())).astimezone(pytz.utc)
            day_end = day_start + timedelta(days=1)

            try:
                eod_res = supabase.from_("oi_snapshots") \
                    .select("timestamp") \
                    .gte("timestamp", day_start.isoformat()) \
                    .lt("timestamp", day_end.isoformat()) \
                    .order("timestamp", desc=True) \
                    .limit(1).execute()
                if not eod_res.data:
                    continue  # no data this day, nothing to do
                eod_ts = eod_res.data[0]["timestamp"]
            except Exception as e:
                print(f"[ARCHIVE] Day {day} — couldn't find EOD timestamp, skipping: {e}")
                continue

            def _archive_chunk(c_start, c_end, depth=0):
                """
                Try a chunk; on timeout, split it in half and retry each half
                recursively (down to a 5-minute floor) instead of giving up
                until the next external trigger. Some 30-min windows during
                the busiest market-open/mid-day periods were consistently
                too dense to fit in one statement even after retrying the
                exact same 30-min chunk multiple times across separate runs —
                splitting finer (down to ~5 min) fits comfortably instead.
                """
                nonlocal succeeded_chunks, failed_chunks
                try:
                    supabase.rpc('archive_range_oi_snapshots', {
                        'range_start': c_start.isoformat(),
                        'range_end': c_end.isoformat(),
                        'eod_ts': eod_ts,
                    }).execute()
                    succeeded_chunks += 1
                except Exception as e:
                    width = c_end - c_start
                    if width > timedelta(minutes=5) and depth < 4:
                        mid = c_start + width / 2
                        _archive_chunk(c_start, mid, depth + 1)
                        _archive_chunk(mid, c_end, depth + 1)
                    else:
                        failed_chunks += 1
                        print(f"[ARCHIVE] Day {day} chunk {c_start.strftime('%H:%M')}-{c_end.strftime('%H:%M')} failed even at finest granularity (will retry next run): {e}")

            chunk_hours = 0.5  # was 2 — market-hours windows (9:15am-3:30pm IST) have much denser data than off-hours, so even 2-hour chunks there were still hitting timeout
            chunk_start = day_start
            while chunk_start < day_end:
                chunk_end = min(chunk_start + timedelta(hours=chunk_hours), day_end)
                _archive_chunk(chunk_start, chunk_end)
                chunk_start = chunk_end

        print(f"[ARCHIVE] Complete — {succeeded_chunks} chunk(s) succeeded, {failed_chunks} failed, across {len(old_days)} day(s) ✅")
    except Exception as e:
        print(f"[ARCHIVE] Error: {e}")

def watchdog_archive():
    """
    Watchdog job — runs Monday mornings, checking whether the weekly archival
    job (scheduled Sundays 8PM IST) actually fired. This exists because the
    scheduler is in-memory and Railway container restarts can silently wipe
    a scheduled slot without any error — exactly what happened Jul 5 and
    Jul 12 2026, when the archive job missed two consecutive Sundays and
    oi_snapshots grew unchecked to 10.89M rows before anyone noticed. Same
    self-healing pattern as watchdog_cpr/watchdog_participant_flow.
    """
    import pytz
    from datetime import datetime, timedelta
    ist = pytz.timezone('Asia/Kolkata')
    supabase = get_supabase()
    try:
        recent = supabase.from_("oi_snapshots_archive") \
            .select("created_at") \
            .order("created_at", desc=True) \
            .limit(1).execute()
        last_run = None
        if recent.data:
            last_run = datetime.fromisoformat(recent.data[0]["created_at"].replace("Z", "+00:00"))
        stale = (not last_run) or (datetime.now(pytz.utc) - last_run > timedelta(days=8))
        if stale:
            print("[ARCHIVE Watchdog] ⚠️ No archive activity in over 8 days — re-triggering...")
            archive_old_snapshots()
        else:
            print(f"[ARCHIVE Watchdog] ✅ Last archive run {last_run.isoformat()} — within expected window")
    except Exception as e:
        print(f"[ARCHIVE Watchdog] Error: {e}")


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

    # ── Also check positional radar cache for today ───────────────────────
    try:
        import datetime as _dt
        _today = _dt.date.today().isoformat()
        radar_check = supabase.from_("positional_radar_cache")\
            .select("trade_date")\
            .eq("trade_date", _today)\
            .limit(1).execute()

        if not radar_check.data:
            print(f"[CPR Watchdog] ⚠️ Radar cache missing for {_today} — refreshing...")
            from api.positional_radar import get_monthly_expiry, get_series_start, clear_radar_cache
            _today_dt = _dt.date.today()
            _expiry = get_monthly_expiry(_today_dt.year, _today_dt.month)
            _series_start = get_series_start(_expiry)
            supabase.rpc("refresh_positional_radar_cache", {
                "p_series_start": _series_start,
                "p_series_end": _today
            }).execute()
            clear_radar_cache()
            print(f"[CPR Watchdog] ✅ Radar cache refreshed for {_today}")
        else:
            print(f"[CPR Watchdog] ✅ Radar cache OK for {_today}")
    except Exception as re:
        print(f"[CPR Watchdog] ❌ Radar cache check failed: {re}")

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
        from api.positional_radar import clear_radar_cache
        clear_radar_cache()
        print(f"[Radar Cache] ✅ In-memory cache cleared")
    except Exception as e:
        print(f"[Radar Cache] ❌ {e}")

def auto_refresh_token():
    from datetime import datetime
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    if now.weekday() >= 5: return
    from utils.market_calendar import is_trading_day
    if not is_trading_day(now.date()): return
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

@app.get("/historical-chain/dates/{symbol}")
def historical_chain_dates(symbol: str):
    from api.historical_chain import get_available_dates
    return get_available_dates(symbol)

@app.get("/historical-chain/snapshots/{symbol}")
def historical_chain_snapshots(symbol: str, date: str):
    from api.historical_chain import get_available_snapshots
    return get_available_snapshots(symbol, date)

@app.get("/historical-chain/{symbol}")
def historical_chain(symbol: str, date: str, timestamp: str = None, expiry: str = None):
    from api.historical_chain import get_historical_chain
    return get_historical_chain(symbol, date, timestamp, expiry)

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

@app.get("/index-data")
def index_data():
    import time
    cache = getattr(index_data, '_cache', None)
    cache_time = getattr(index_data, '_cache_time', 0)
    if cache and (time.time() - cache_time) < 60:
        return cache

    supabase = get_supabase()
    try:
        # Get last available trading timestamp for NIFTY FUT
        ts_res = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .eq("option_type", "FUT")\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        if not ts_res.data:
            return {"timestamp": None, "rows": [], "cmps": []}
        ts = ts_res.data[0]["timestamp"]

        # Fetch index OI data + CMP in parallel
        from concurrent.futures import ThreadPoolExecutor
        def fetch_oi_batch(rng):
            return supabase.from_("oi_snapshots")\
                .select("symbol,strike,option_type,oi,volume,last_price,expiry")\
                .eq("timestamp", ts)\
                .in_("symbol", ["NIFTY","BANKNIFTY","FINNIFTY"])\
                .range(rng[0], rng[1])\
                .execute()
        def fetch_cmps():
            return supabase.from_("cmp_prices")\
                .select("symbol,cmp")\
                .order("timestamp", desc=True)\
                .limit(200)\
                .execute()

        with ThreadPoolExecutor(max_workers=3) as ex:
            f1 = ex.submit(fetch_oi_batch, (0, 999))
            f2 = ex.submit(fetch_oi_batch, (1000, 1999))
            f3 = ex.submit(fetch_cmps)
            b1, b2, cmps = f1.result(), f2.result(), f3.result()

        rows = (b1.data or []) + (b2.data or [])
        result = {"timestamp": ts, "rows": rows, "cmps": cmps.data or []}
        index_data._cache = result
        index_data._cache_time = time.time()
        return result
    except Exception as e:
        print(f"[INDEX-DATA] Error: {e}")
        return {"timestamp": None, "rows": [], "cmps": []}

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
            max_tokens=3000,
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

        # Price change: today open → today close (candle body — more accurate for signal classification)
        today_open_price = today_d["open_price"]
        today_close_price = today_d["close_price"]
        price_chg_pct = round((today_close_price - today_open_price) / today_open_price * 100, 2) if today_open_price > 0 else 0

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

@app.get("/clear-radar-cache")
def clear_radar_cache_endpoint():
    from api.positional_radar import clear_radar_cache
    return clear_radar_cache()

@app.get("/positional-radar")
def positional_radar(min_consec: int = 0):
    from api.positional_radar import get_positional_radar
    return get_positional_radar(min_consec=min_consec)

@app.get("/market-status")
def market_status():
    from utils.market_calendar import get_market_status
    return get_market_status()

@app.get("/delivery-confluence")
def delivery_confluence():
    from api.delivery_confluence import get_delivery_confluence
    from utils.db import get_supabase
    return get_delivery_confluence(get_supabase())

@app.get("/stock-intel/{symbol}")
def stock_intel(symbol: str):
    """Unified stock intelligence — aggregates all signals for one symbol."""
    from utils.db import get_supabase
    import pytz
    from datetime import datetime, timedelta
    supabase = get_supabase()
    sym = symbol.upper()
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()

    # Last trading day
    try:
        from utils.market_calendar import is_trading_day
        check = today
        if not is_trading_day(check):
            check -= timedelta(days=1)
            while not is_trading_day(check):
                check -= timedelta(days=1)
        last_trading_day = check.isoformat()
    except:
        last_trading_day = today.isoformat()

    result = {"symbol": sym, "last_trading_day": last_trading_day}

    # ── 1. FUT Signal (today) ─────────────────────────────────────────
    try:
        fut_res = supabase.from_("daily_oi_summary")\
            .select("trade_date, fut_signal, fut_oi_chg_pct, price_chg_pct, close_price, fut_vol")\
            .eq("symbol", sym)\
            .eq("trade_date", last_trading_day)\
            .limit(1).execute()
        result["fut_signal"] = fut_res.data[0] if fut_res.data else None
    except:
        result["fut_signal"] = None

    # ── 2. Signal History (last 20 days) ──────────────────────────────
    try:
        hist_res = supabase.from_("daily_oi_summary")\
            .select("trade_date, fut_signal, fut_oi_chg_pct, price_chg_pct, close_price, fut_vol")\
            .eq("symbol", sym)\
            .order("trade_date", desc=True)\
            .limit(20).execute()
        vols = [int(r.get("fut_vol") or 0) for r in (hist_res.data or [])[1:6] if r.get("fut_vol")]
        avg_vol = sum(vols) / len(vols) if vols else 0
        history = []
        for r in (hist_res.data or []):
            vol = int(r.get("fut_vol") or 0)
            history.append({
                "date": r["trade_date"],
                "signal": r.get("fut_signal") or "NEUTRAL",
                "fut_oi_chg": round(float(r.get("fut_oi_chg_pct") or 0), 2),
                "price_chg": round(float(r.get("price_chg_pct") or 0), 2),
                "close_price": round(float(r.get("close_price") or 0), 2),
                "volume": vol,
                "vol_ratio": round(vol / avg_vol, 2) if avg_vol > 0 else None,
            })
        result["signal_history"] = history
    except:
        result["signal_history"] = []

    # ── 3. Delivery ───────────────────────────────────────────────────
    try:
        del_res = supabase.from_("delivery_data")\
            .select("trade_date, delivery_pct")\
            .eq("symbol", sym)\
            .order("trade_date", desc=True)\
            .limit(5).execute()
        result["delivery"] = del_res.data or []
    except:
        result["delivery"] = []

    # ── 4. CPR ────────────────────────────────────────────────────────
    try:
        cpr_res = supabase.from_("cpr_levels")\
            .select("trade_date, tc, bc, pivot, width_pct, cpr_trend, cpr_status")\
            .eq("symbol", sym)\
            .order("trade_date", desc=True)\
            .limit(1).execute()
        result["cpr"] = cpr_res.data[0] if cpr_res.data else None
    except:
        result["cpr"] = None

    # ── 5. UOA ────────────────────────────────────────────────────────────
    try:
        from api.uoa import get_uoa
        uoa_data = get_uoa()
        result["uoa"] = [s for s in (uoa_data.get("signals") or []) if s.get("symbol") == sym]
    except:
        result["uoa"] = []

    # ── 6. Options Jungle — writing signals ───────────────────────────────
    try:
        from api.options_jungle import get_options_jungle
        jungle = get_options_jungle()
        sym_jungle = [s for s in (jungle.get("oi_spikes") or []) if s.get("symbol") == sym]
        result["jungle"] = sym_jungle
        result["put_writing"] = [s for s in sym_jungle if s.get("interpretation") == "PUT_WRITING"]
        result["call_writing"] = [s for s in sym_jungle if s.get("interpretation") == "CALL_WRITING"]
    except:
        result["jungle"] = []
        result["put_writing"] = []
        result["call_writing"] = []

    # ── 7. Max Pain ───────────────────────────────────────────────────────
    try:
        from api.max_pain import get_max_pain
        mp_data = get_max_pain()
        mp_item = next((s for s in (mp_data.get("symbols") or []) if s.get("symbol") == sym), None)
        result["max_pain"] = mp_item
    except:
        result["max_pain"] = None

    return result

@app.get("/rollover")
def rollover():
    from api.rollover import get_rollover
    from utils.db import get_supabase
    return get_rollover(get_supabase())

@app.get("/eod-report")
def eod_report(date: str = None):
    from api.eod_report import get_eod_report
    from utils.db import get_supabase
    return get_eod_report(get_supabase(), date)

@app.get("/positional-intelligence")
def positional_intelligence(min_consec: int = 0):
    from api.positional_intelligence import get_positional_intelligence
    return get_positional_intelligence(min_consec=min_consec)

@app.get("/stock-signal-history/{symbol}")
def stock_signal_history(symbol: str, days: int = 20):
    """
    Returns day-by-day FUT signal history for a single stock.
    Used by the Positional Intelligence signal history popup.
    """
    from datetime import datetime, timedelta
    from utils.db import get_supabase
    import pytz

    try:
        supabase = get_supabase()
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date().isoformat()
        start = (datetime.now(ist).date() - timedelta(days=60)).isoformat()

        result = supabase.from_("daily_oi_summary") \
            .select("trade_date, fut_oi_chg_pct, price_chg_pct, fut_signal, close_price, fut_vol") \
            .eq("symbol", symbol.upper()) \
            .gte("trade_date", start) \
            .lte("trade_date", today) \
            .order("trade_date", desc=True) \
            .limit(days) \
            .execute()

        rows = result.data or []

        # Compute 5-day avg volume for context (is today's volume high or low vs recent norm)
        vols = [int(r.get("fut_vol") or 0) for r in rows if r.get("fut_vol")]
        avg_vol = sum(vols[1:6]) / len(vols[1:6]) if len(vols) > 1 else 0

        history = []
        for r in rows:
            vol = int(r.get("fut_vol") or 0)
            vol_ratio = round(vol / avg_vol, 2) if avg_vol > 0 else None
            history.append({
                "date":         r["trade_date"],
                "signal":       r.get("fut_signal") or "NEUTRAL",
                "fut_oi_chg":   round(float(r.get("fut_oi_chg_pct") or 0), 2),
                "price_chg":    round(float(r.get("price_chg_pct") or 0), 2),
                "close_price":  round(float(r.get("close_price") or 0), 2),
                "volume":       vol,
                "vol_ratio":    vol_ratio,
            })

        return {
            "symbol":  symbol.upper(),
            "history": history,
            "total":   len(history),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"symbol": symbol.upper(), "history": [], "total": 0, "error": str(e)}

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

    # ── Get today's price change (open-to-close candle body) + volume ─────
    price_res = supabase.from_("daily_oi_summary")\
        .select("symbol, close_price, fut_vol")\
        .eq("trade_date", last_trading_day)\
        .limit(200)\
        .execute()
    price_map = {r["symbol"]: r for r in (price_res.data or [])}

    # ── 5-day avg volume per symbol for context ────────────────────────────
    vol_hist_res = supabase.from_("daily_oi_summary")\
        .select("symbol, trade_date, fut_vol")\
        .gte("trade_date", (check - _dt.timedelta(days=10)).isoformat())\
        .lt("trade_date", last_trading_day)\
        .order("trade_date", desc=True)\
        .limit(2000)\
        .execute()
    vol_hist_map = defaultdict(list)
    for r in (vol_hist_res.data or []):
        if r.get("fut_vol"):
            vol_hist_map[r["symbol"]].append(int(r["fut_vol"]))
    avg_vol_map = {sym: sum(v[:5]) / len(v[:5]) for sym, v in vol_hist_map.items() if v}


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
        .select("symbol, strike, option_type, oi, expiry, timestamp")\
        .in_("option_type", ["CE", "PE"])\
        .gte("timestamp", f"{last_trading_day}T09:45:00+00:00")\
        .lte("timestamp", f"{last_trading_day}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
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

    # Build latest timestamp per symbol — use EOD OI (3:29 PM), not morning snapshot
    latest_ts_per_sym = {}
    for r in (oi_res.data or []):
        sym = r["symbol"]
        if str(r.get("expiry") or "") != sym_expiry.get(sym, ""):
            continue
        ts = r.get("timestamp") or ""
        if sym not in latest_ts_per_sym or ts > latest_ts_per_sym[sym]:
            latest_ts_per_sym[sym] = ts

    for r in (oi_res.data or []):
        sym = r["symbol"]
        if str(r.get("expiry") or "") != sym_expiry.get(sym, ""):
            continue
        if r.get("timestamp") != latest_ts_per_sym.get(sym):
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
        if len(history) < 8:
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

        # Price change — close-to-close (prev day close → today close)
        # This correctly identifies days where price DIDN'T react to OI accumulation
        price_data = price_map.get(sym, {})
        close_price = float(price_data.get("close_price") or 0)
        cmp = cmp_map.get(sym, close_price)
        # Get price change directly from daily_oi_summary (most accurate — uses official EOD)
        # Skip if price_chg_pct is null — means no previous day data (new stock)
        if price_data.get("price_chg_pct") is None:
            continue
        price_chg = round(float(price_data.get("price_chg_pct") or 0), 2)

        # Skip if price moved too much either way — stealth = quiet accumulation
        if price_chg < -0.3 or price_chg > 1.0:
            continue

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
        elif rank <= 2 and abs_price <= 1.0:
            tier = "STRONG"
            tier_label = "🥈 Strong"
            tier_color = "SILVER"
        elif rank <= 5 and abs_price <= 1.5:
            tier = "WATCH"
            tier_label = "🥉 Watch"
            tier_color = "BRONZE"
        else:
            continue

        today_vol = int(price_data.get("fut_vol") or 0)
        avg_vol = avg_vol_map.get(sym, 0)
        vol_ratio = round(today_vol / avg_vol, 2) if avg_vol > 0 else None

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
            "volume":           today_vol,
            "vol_ratio":        vol_ratio,
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

    # REPLACE WITH:
    # ── Max OI walls — highest OI anywhere in range (no CMP filter) ──────
    ce_wall_s = max(strikes, key=lambda x: x["ce_oi"])
    pe_wall_s = max(strikes, key=lambda x: x["pe_oi"])
    ce_wall = ce_wall_s["strike"]
    pe_wall = pe_wall_s["strike"]
    ce_wall_oi = ce_wall_s["ce_oi"]
    pe_wall_oi = pe_wall_s["pe_oi"]

   # ── Intraday walls — nearest significant strike above/below CMP ───────
    # Significant = OI >= 10% of max OI in that direction
    ce_threshold = ce_wall_oi * 0.10
    pe_threshold = pe_wall_oi * 0.10

    ce_above = [s for s in strikes if s["strike"] > cmp]
    pe_below = [s for s in strikes if s["strike"] < cmp]
    if not ce_above: ce_above = strikes
    if not pe_below: pe_below = strikes

    ce_significant = [s for s in ce_above if s["ce_oi"] >= ce_threshold]
    pe_significant = [s for s in pe_below if s["pe_oi"] >= pe_threshold]

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

@app.get("/archive-snapshots-now")
def archive_snapshots_now():
    try:
        archive_old_snapshots()
        return {"status": "triggered"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/adx-map")
def adx_map():
    from api.adx_analysis import get_combined_adx_map
    from api.iv_analysis import SYMBOLS
    return {"data": get_combined_adx_map(get_supabase(), symbols=SYMBOLS)}

@app.post("/waitlist-join")
async def waitlist_join(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return {"error": "Please enter a valid email"}
    try:
        supabase = get_supabase()
        res = supabase.from_("waitlist_emails").select("email").eq("email", email).limit(1).execute()
        if res.data:
            return {"status": "already_joined"}
        supabase.from_("waitlist_emails").insert({"email": email}).execute()
        return {"status": "joined"}
    except Exception as e:
        print(f"[Waitlist] Failed: {e}")
        return {"error": "Something went wrong, please try again"}

@app.get("/push-preferences")
def push_preferences_get(endpoint: str):
    from api.push_notifications import get_preferences
    return get_preferences(get_supabase(), endpoint)

@app.post("/push-preferences")
async def push_preferences_post(request: Request):
    from api.push_notifications import save_preferences
    body = await request.json()
    endpoint = body.get("endpoint")
    enabled_signals = body.get("enabled_signals", [])
    spike_threshold = body.get("spike_threshold")
    vol_threshold = body.get("vol_threshold")
    if not endpoint:
        return {"error": "endpoint required"}
    return save_preferences(get_supabase(), endpoint, enabled_signals, spike_threshold, vol_threshold)

@app.get("/premarket-brief")
def premarket_brief():
    from api.premarket_brief import get_premarket_brief
    return get_premarket_brief(get_supabase())

@app.get("/push-check-now")
def push_check_now():
    try:
        from services.push_checker import run_push_checks
        run_push_checks(get_supabase())
        return {"status": "triggered"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/push-subscribe")
async def push_subscribe(request: Request):
    from api.push_notifications import save_subscription
    body = await request.json()
    subscription = body.get("subscription")
    threshold = body.get("spikeThreshold", 10)
    if not subscription:
        return {"error": "Missing subscription"}
    supabase = get_supabase()
    return save_subscription(supabase, subscription, threshold)

@app.post("/push-unsubscribe")
async def push_unsubscribe(request: Request):
    from api.push_notifications import remove_subscription
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        return {"error": "Missing endpoint"}
    supabase = get_supabase()
    return remove_subscription(supabase, endpoint)

@app.get("/oi-buildup-period")
def oi_buildup_period(period: str = "weekly"):
    from api.oi_buildup_period import get_oi_buildup_period
    if period not in ("weekly", "monthly"):
        return {"error": "period must be 'weekly' or 'monthly'"}
    return get_oi_buildup_period(get_supabase(), period)

@app.get("/fetch-delivery")
def fetch_delivery():
    try:
        fetch_delivery_data()
        return {"status": "triggered"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/run-archive")
def run_archive():
    try:
        archive_old_snapshots()
        return {"status": "archive triggered"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ── CommodityNova routes ───────────────────────────────────────────────────
app.include_router(mcx_router, prefix="/mcx", tags=["MCX"])
app.include_router(mcx_oi_map_router, prefix="/mcx", tags=["MCX"])
# ──────────────────────────────────────────────────────────────────────────
