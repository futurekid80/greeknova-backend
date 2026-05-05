import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn, sys, time



INDICES = ["NIFTY","BANKNIFTY","FINNIFTY"]
TOP30 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK",
    "LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH",
    "TATACONSUM","TATASTEEL","ADANIENT","POWERGRID","NTPC",
    "ONGC","JSWSTEEL","COALINDIA","BAJAJFINSV","TECHM"
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
    "COALINDIA":"NSE:COALINDIA","BAJAJFINSV":"NSE:BAJAJFINSV","TECHM":"NSE:TECHM"
}

def run_full_capture():
    from dotenv import load_dotenv
    load_dotenv('/Users/apple/optionspulse/.env')
    from datetime import datetime, timezone
    now = datetime.now()
    if not (9 <= now.hour <= 15): return
    if now.hour == 9 and now.minute < 15: return
    if now.hour == 15 and now.minute > 30: return
    print(f"📸 Auto-capture at {now.strftime('%H:%M:%S')}...")
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
                if price: cmp_records.append({"timestamp": timestamp, "symbol": sym, "cmp": float(price)})
        except Exception as e: print(f"  ⚠️ Index CMP: {e}")
        try:
            stk_quotes = kite.quote(list(STOCK_NSE_MAP.values()))
            for sym, key in STOCK_NSE_MAP.items():
                price = stk_quotes.get(key, {}).get("last_price", 0)
                if price: cmp_records.append({"timestamp": timestamp, "symbol": sym, "cmp": float(price)})
        except Exception as e: print(f"  ⚠️ Stock CMP: {e}")
        instruments = kite.instruments("NFO")
        for symbol in INDICES + TOP30:
            is_index = symbol in INDICES
            limit = 40 if is_index else 20
            found = [i for i in instruments if i["name"] == symbol and i["instrument_type"] in ["CE","PE"]]
            if not found: continue
            expiries = sorted(set(i["expiry"] for i in found))
            nearest = [i for i in found if i["expiry"] == expiries[0]][:limit]
            try:
                quotes = kite.quote(["NFO:" + i["tradingsymbol"] for i in nearest])
                for inst in nearest:
                    key = f"NFO:{inst['tradingsymbol']}"
                    if key in quotes:
                        q = quotes[key]
                        records.append({
                            "timestamp": timestamp, "symbol": symbol,
                            "tradingsymbol": inst["tradingsymbol"],
                            "strike": float(inst["strike"]),
                            "option_type": inst["instrument_type"],
                            "expiry": inst["expiry"].isoformat(),
                            "oi": int(q.get("oi", 0)),
                            "oi_day_high": int(q.get("oi_day_high", 0)),
                            "volume": int(q.get("volume", 0)),
                            "last_price": float(q.get("last_price", 0)),
                            "is_index": is_index,
                        })
                time.sleep(0.3)
            except Exception as e: print(f"  ❌ {symbol}: {e}")
        if records:
            for i in range(0, len(records), 500):
                supabase.table("oi_snapshots").insert(records[i:i+500]).execute()
        if cmp_records:
            supabase.table("cmp_prices").insert(cmp_records).execute()
        print(f"  ✅ Saved {len(records)} OI + {len(cmp_records)} CMP records")
    except Exception as e: print(f"  ❌ Capture failed: {e}")

scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(run_full_capture, "interval", minutes=5, id="full_capture")
    scheduler.add_job(auto_refresh_token, "cron", hour=8, minute=30, timezone="Asia/Kolkata", id="token_refresh")
    scheduler.start()
    print("✅ GreekNova backend started")
    print("📸 Full capture every 5 min during market hours")
    yield
    scheduler.shutdown()

app = FastAPI(title="GreekNova API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000", "https://greeknova-frontend.vercel.app"],
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

@app.get("/oi-spikes")
def oi_spikes(threshold: float = 10.0):
    from api.oi_spike import get_oi_spikes
    return get_oi_spikes(threshold)

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
def volume_spikes(threshold: float = 50.0):
    from api.volume_spike import get_volume_spikes
    return get_volume_spikes(threshold)

@app.get("/confluence")
def confluence():
    from api.confluence import get_confluence
    return get_confluence()

@app.get("/max-pain")
def max_pain():
    from api.max_pain import get_max_pain_all
    return get_max_pain_all()

def auto_refresh_token():
    """Auto-login every morning at 8:30 AM"""
    from datetime import datetime
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    print(f"🔐 Auto token refresh at {now.strftime('%H:%M IST')}...")
    try:
        import os
        os.remove(os.path.expanduser('~/.greeksnova_token'))
    except:
        pass
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        profile = kite.profile()
        print(f"✅ Auto-login successful: {profile['user_name']}")
    except Exception as e:
        print(f"❌ Auto-login failed: {e}")

@app.get("/uoa")
def uoa():
    from api.uoa import get_uoa
    return get_uoa()

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
