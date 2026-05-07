from utils.db import get_supabase
from datetime import datetime, timezone
from collections import defaultdict

STOCK_NSE_MAP = {
    "RELIANCE":"NSE:RELIANCE","TCS":"NSE:TCS","HDFCBANK":"NSE:HDFCBANK",
    "INFY":"NSE:INFY","ICICIBANK":"NSE:ICICIBANK","HINDUNILVR":"NSE:HINDUNILVR",
    "ITC":"NSE:ITC","SBIN":"NSE:SBIN","BHARTIARTL":"NSE:BHARTIARTL",
    "KOTAKBANK":"NSE:KOTAKBANK","LT":"NSE:LT","AXISBANK":"NSE:AXISBANK",
    "ASIANPAINT":"NSE:ASIANPAINT","MARUTI":"NSE:MARUTI","TITAN":"NSE:TITAN",
    "SUNPHARMA":"NSE:SUNPHARMA","ULTRACEMCO":"NSE:ULTRACEMCO",
    "BAJFINANCE":"NSE:BAJFINANCE","WIPRO":"NSE:WIPRO","HCLTECH":"NSE:HCLTECH",
    "TATACONSUM":"NSE:TATACONSUM","TATASTEEL":"NSE:TATASTEEL",
    "ADANIENT":"NSE:ADANIENT","POWERGRID":"NSE:POWERGRID","NTPC":"NSE:NTPC",
    "ONGC":"NSE:ONGC","JSWSTEEL":"NSE:JSWSTEEL","COALINDIA":"NSE:COALINDIA",
    "BAJAJFINSV":"NSE:BAJAJFINSV","TECHM":"NSE:TECHM",
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
INDEX_NSE_MAP = {
    "NIFTY":     "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY":  "NSE:NIFTY FIN SERVICE",
}
ALL_SYMBOLS = list(INDEX_NSE_MAP.keys()) + list(STOCK_NSE_MAP.keys())

def classify(oi_chg_pct, price_chg_pct):
    if oi_chg_pct > 0 and price_chg_pct > 0:
        return "LONG_BUILDUP",   "Long Buildup",   "text-emerald-400", "bg-emerald-950/30", "border-emerald-800/40"
    if oi_chg_pct > 0 and price_chg_pct < 0:
        return "SHORT_BUILDUP",  "Short Buildup",  "text-red-400",     "bg-red-950/30",     "border-red-800/40"
    if oi_chg_pct < 0 and price_chg_pct > 0:
        return "SHORT_COVERING", "Short Covering", "text-cyan-400",    "bg-cyan-950/30",    "border-cyan-800/40"
    if oi_chg_pct < 0 and price_chg_pct < 0:
        return "LONG_UNWINDING", "Long Unwinding", "text-orange-400",  "bg-orange-950/30",  "border-orange-800/40"
    return "NEUTRAL", "Neutral", "text-gray-400", "bg-gray-900/30", "border-gray-800"

def fetch_all_paginated(supabase, timestamp):
    """Fetch all OI data for a timestamp using pagination"""
    all_data = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("symbol, oi")\
            .eq("timestamp", timestamp)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_data.extend(batch.data)
        if len(batch.data) < 1000:
            break
    return all_data

def get_oi_pulse(filter_type: str = "all"):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get distinct timestamps for today via NIFTY
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    # Paginate to get all timestamps beyond 1000 row limit
    all_ts_data = list(ts_result.data)
    if len(ts_result.data) == 1000:
        for offset in range(1000, 20000, 1000):
            batch = supabase.from_("oi_snapshots")                .select("timestamp")                .eq("symbol", "NIFTY")                .gte("timestamp", f"{today}T00:00:00+00:00")                .order("timestamp", desc=False)                .range(offset, offset + 999)                .execute()
            if not batch.data: break
            all_ts_data.extend(batch.data)
            if len(batch.data) < 1000: break

    timestamps = sorted(set(r["timestamp"] for r in all_ts_data))
    if len(timestamps) < 2:
        return {"items": [], "as_of": datetime.now(timezone.utc).isoformat(),
                "count": 0, "message": f"Need 2+ snapshots today — have {len(timestamps)} so far"}

    ts_new = timestamps[-1]   # latest snapshot
    ts_old = timestamps[-2]   # previous snapshot (ensures same stock coverage)

    # Batch fetch OI for both timestamps
    old_rows = fetch_all_paginated(supabase, ts_old)
    new_rows = fetch_all_paginated(supabase, ts_new)

    # Aggregate OI per symbol
    oi_old = defaultdict(int)
    oi_new = defaultdict(int)
    for r in old_rows:
        oi_old[r["symbol"]] += r["oi"] or 0
    for r in new_rows:
        oi_new[r["symbol"]] += r["oi"] or 0

    # Get current prices from Kite
    prices = {}
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        all_map = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}
        quotes = kite.quote(list(all_map.values()))
        for sym, key in all_map.items():
            if key in quotes:
                q = quotes[key]
                prev = q.get("ohlc", {}).get("close", q["last_price"])
                prices[sym] = {"ltp": q["last_price"], "prev_close": prev}
    except Exception as e:
        print(f"Price fetch failed: {e}")

    items = []
    for sym in ALL_SYMBOLS:
        is_index = sym in INDEX_NSE_MAP
        if filter_type == "index"  and not is_index: continue
        if filter_type == "stocks" and is_index:     continue

        o_old = oi_old.get(sym, 0)
        o_new = oi_new.get(sym, 0)
        if o_old == 0:
            continue

        oi_chg_pct = round((o_new - o_old) / o_old * 100, 2)
        oi_chg_abs = o_new - o_old

        price_chg_pct = 0.0
        ltp = None
        if sym in prices:
            ltp  = prices[sym]["ltp"]
            prev = prices[sym]["prev_close"]
            if prev and prev > 0:
                price_chg_pct = round((ltp - prev) / prev * 100, 2)

        signal, label, color, bg, border = classify(oi_chg_pct, price_chg_pct)

        items.append({
            "symbol": sym, "is_index": is_index,
            "oi_now": o_new, "oi_prev": o_old,
            "oi_chg_abs": oi_chg_abs, "oi_chg_pct": oi_chg_pct,
            "ltp": ltp, "price_chg_pct": price_chg_pct,
            "signal": signal, "label": label,
            "color": color, "bg": bg, "border": border,
        })

    items.sort(key=lambda x: abs(x["oi_chg_pct"]), reverse=True)

    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                clean = f"{base}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist = dt.hour * 60 + dt.minute + 330
            return f"{(ist//60)%24:02d}:{ist%60:02d}"
        except:
            return ts[11:16]

    return {
        "items": items, "count": len(items),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "open_time": to_ist(ts_old), "close_time": to_ist(ts_new),
        "snapshots": len(timestamps),
    }
