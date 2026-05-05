import os
import time
from datetime import datetime
from dotenv import load_dotenv
from backend.utils.db import get_supabase
from backend.services.kite_auth import get_kite_client

load_dotenv()

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
TOP30 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK",
    "LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH",
    "TATACONSUM","TATASTEEL","ADANIENT","POWERGRID","NTPC",
    "ONGC","JSWSTEEL","COALINDIA","BAJAJFINSV","TECHM"
]
ALL_SYMBOLS = INDICES + TOP30

def capture_oi_snapshot():
    now = datetime.now()
    if not (9 <= now.hour <= 15):
        print(f"⏸️  Market closed at {now.strftime('%H:%M')} — skipping")
        return
    if now.hour == 9 and now.minute < 15:
        return
    if now.hour == 15 and now.minute > 30:
        return

    print(f"📸 Capturing at {now.strftime('%H:%M:%S')}...")
    try:
        kite = get_kite_client()
        supabase = get_supabase()
        instruments = kite.instruments("NFO")
        timestamp = now.isoformat()
        records = []

        # Also get spot prices for indices and stocks
        spot_map = {}
        try:
            spot_symbols = ["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:NIFTY FIN SERVICE"]
            spots = kite.quote(spot_symbols)
            spot_map["NIFTY"] = spots.get("NSE:NIFTY 50", {}).get("last_price", 0)
            spot_map["BANKNIFTY"] = spots.get("NSE:NIFTY BANK", {}).get("last_price", 0)
            spot_map["FINNIFTY"] = spots.get("NSE:NIFTY FIN SERVICE", {}).get("last_price", 0)
        except:
            pass

        for symbol in ALL_SYMBOLS:
            is_index = symbol in INDICES
            limit = 40 if is_index else 20
            found = [i for i in instruments if i["name"] == symbol and i["instrument_type"] in ["CE","PE"]]
            if not found:
                continue
            expiries = sorted(set(i["expiry"] for i in found))
            nearest = [i for i in found if i["expiry"] == expiries[0]][:limit]

            try:
                quotes = kite.quote(["NFO:" + i["tradingsymbol"] for i in nearest])
                for inst in nearest:
                    key = f"NFO:{inst['tradingsymbol']}"
                    if key in quotes:
                        q = quotes[key]
                        records.append({
                            "timestamp": timestamp,
                            "symbol": symbol,
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
                time.sleep(0.3)  # avoid rate limit
            except Exception as e:
                print(f"  ❌ {symbol}: {e}")

        if records:
            for i in range(0, len(records), 500):
                supabase.table("oi_snapshots").insert(records[i:i+500]).execute()
            print(f"  ✅ Saved {len(records)} records ({len(ALL_SYMBOLS)} symbols)")
    except Exception as e:
        print(f"  ❌ Capture failed: {e}")

if __name__ == "__main__":
    capture_oi_snapshot()
