from utils.db import get_supabase
from datetime import datetime, timezone, timedelta
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

MARKET_OPEN_UTC  = 3 * 60 + 45   # 03:45 UTC = 09:15 IST
MARKET_CLOSE_UTC = 10 * 60 + 0   # 10:00 UTC = 15:30 IST


def is_market_ts(ts: str) -> bool:
    try:
        hour = int(ts[11:13])
        minute = int(ts[14:16])
        total = hour * 60 + minute
        return MARKET_OPEN_UTC <= total <= MARKET_CLOSE_UTC
    except:
        return False


def is_market_hours() -> bool:
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return False
    total = now_utc.hour * 60 + now_utc.minute
    return MARKET_OPEN_UTC <= total <= MARKET_CLOSE_UTC


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


# ── FIX: Paginated fetch — old limit(50000) was cutting off at ~30 stocks
# 66 symbols × 1280 rows = 84,480 rows needed, well over 50k
def get_nearest_expiry_per_symbol(supabase, timestamp: str) -> dict:
    """Get nearest active expiry for each symbol at a given timestamp"""
    today = timestamp[:10]
    result = supabase.from_("oi_snapshots")\
        .select("symbol, expiry")\
        .eq("timestamp", timestamp)\
        .gte("expiry", today)\
        .execute()
    nearest = {}
    for r in (result.data or []):
        sym = r["symbol"]
        exp = r["expiry"]
        if exp and (sym not in nearest or exp < nearest[sym]):
            nearest[sym] = exp
    return nearest


def fetch_oi_for_timestamp(supabase, timestamp: str, nearest_expiry_map: dict = None):
    """Fetch OI filtered to nearest expiry per symbol — avoids rollover inflation"""
    all_rows = []
    for offset in range(0, 500000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("symbol, oi, expiry")\
            .eq("timestamp", timestamp)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not nearest_expiry_map:
        return all_rows

    # Filter each row to nearest expiry for that symbol
    filtered = []
    for r in all_rows:
        sym = r["symbol"]
        nearest = nearest_expiry_map.get(sym)
        if nearest and r.get("expiry") == nearest:
            filtered.append(r)
        elif not nearest:
            filtered.append(r)
    return filtered


def get_latest_market_timestamp(supabase):
    # Find most recent trading day that has data
    for days_back in range(6):
        check_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')
        result = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{check_date}T00:00:00+00:00")\
            .lt("timestamp", f"{check_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(100)\
            .execute()
        for r in (result.data or []):
            if is_market_ts(r["timestamp"]):
                return r["timestamp"]
    return None
    result = supabase.from_("oi_snapshots") \
        .select("timestamp") \
        .eq("symbol", "NIFTY") \
        .gte("timestamp", f"{since}T00:00:00+00:00") \
        .order("timestamp", desc=True) \
        .limit(500) \
        .execute()
    for r in (result.data or []):
        if is_market_ts(r["timestamp"]):
            return r["timestamp"]
    return None


def get_prev_market_timestamp(supabase, current_date: str):
    """Get FIRST snapshot of the SAME trading day — intraday comparison"""
    result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{current_date}T03:30:00+00:00")\
        .lt("timestamp", f"{current_date}T10:30:00+00:00")\
        .order("timestamp", desc=False)\
        .limit(10)\
        .execute()
    for r in (result.data or []):
        if is_market_ts(r["timestamp"]):
            return r["timestamp"]
    # Fallback to previous day EOD
    result2 = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .lt("timestamp", f"{current_date}T03:30:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(100)\
        .execute()
    for r in (result2.data or []):
        if is_market_ts(r["timestamp"]):
            return r["timestamp"]
    return result2.data[0]["timestamp"] if result2.data else None


def get_prices_for_timestamp(supabase, timestamp: str):
    result = supabase.from_("oi_snapshots") \
        .select("symbol, last_price") \
        .eq("timestamp", timestamp) \
        .limit(5000) \
        .execute()
    prices = {}
    seen = set()
    for r in (result.data or []):
        if r["symbol"] not in seen and r.get("last_price"):
            prices[r["symbol"]] = r["last_price"]
            seen.add(r["symbol"])
    return prices


def get_timestamps_for_date(supabase, date_str: str) -> list:
    result = supabase.from_("oi_snapshots") \
        .select("timestamp") \
        .eq("symbol", "NIFTY") \
        .gte("timestamp", f"{date_str}T00:00:00+00:00") \
        .lt("timestamp", f"{date_str}T23:59:59+00:00") \
        .order("timestamp", desc=False) \
        .limit(500) \
        .execute()
    return sorted(set(r["timestamp"] for r in (result.data or [])))


def to_ist(ts: str) -> str:
    try:
        clean = ts.split('+')[0].split('Z')[0]
        if '.' in clean:
            base, frac = clean.split('.')
            clean = f"{base}.{frac[:6].ljust(6, '0')}"
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        ist = dt.hour * 60 + dt.minute + 330
        return f"{(ist // 60) % 24:02d}:{ist % 60:02d}"
    except:
        return ts[11:16]


# ── FIX: Removed filter_type param — backend now always returns ALL symbols
# Frontend handles All/Index/Stocks filtering locally (no extra API calls)
def get_oi_pulse():
    supabase = get_supabase()
    live = is_market_hours()

    # Step 1: Get latest market-hours snapshot
    ts_new = get_latest_market_timestamp(supabase)
    if not ts_new:
        return {"items": [], "count": 0, "message": "No market data found for today"}

    active_date = ts_new[:10]
    today_ts = get_timestamps_for_date(supabase, active_date)

    # Step 2: Get previous trading day's last snapshot
    ts_old = get_prev_market_timestamp(supabase, active_date)
    if not ts_old:
        ts_old = today_ts[0] if today_ts else ts_new

    # Step 3: Fetch OI — filtered to nearest expiry to avoid rollover inflation
    nearest_expiry_map = get_nearest_expiry_per_symbol(supabase, ts_new)
    old_rows = fetch_oi_for_timestamp(supabase, ts_old, nearest_expiry_map)
    new_rows = fetch_oi_for_timestamp(supabase, ts_new, nearest_expiry_map)

    oi_old = defaultdict(int)
    oi_new = defaultdict(int)
    for r in old_rows:
        oi_old[r["symbol"]] += r["oi"] or 0
    for r in new_rows:
        oi_new[r["symbol"]] += r["oi"] or 0

    # Step 4: Get prices
    prices = {}
    if live:
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
            print(f"[OI Pulse] Live price fetch failed: {e}")

    if not prices:
        active_date = ts_new[:10]
        # Use cmp_prices table for stock prices — NOT option LTP from oi_snapshots
        cmp_result = supabase.from_("cmp_prices")\
            .select("symbol, cmp, timestamp")\
            .gte("timestamp", f"{active_date}T00:00:00+00:00")\
            .lt("timestamp", f"{active_date}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .limit(5000)\
            .execute()
        
        first_cmp: dict = {}
        last_cmp: dict = {}
        for r in (cmp_result.data or []):
            sym = r["symbol"]
            if sym not in first_cmp:
                first_cmp[sym] = float(r["cmp"])
            last_cmp[sym] = float(r["cmp"])
        
        for sym in last_cmp:
            prices[sym] = {
                "ltp": last_cmp[sym],
                "prev_close": first_cmp.get(sym, last_cmp[sym])
            }

    # Step 5: Build items for ALL symbols — no filter here
    items = []
    for sym in ALL_SYMBOLS:
        is_index = sym in INDEX_NSE_MAP

        o_old = oi_old.get(sym, 0)
        o_new = oi_new.get(sym, 0)
        if o_new == 0:
            continue

        oi_chg_pct = 0.0
        oi_chg_abs = 0
        if o_old > 0:
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
            "symbol":        sym,
            "is_index":      is_index,
            "oi_now":        o_new,
            "oi_prev":       o_old,
            "oi_chg_abs":    oi_chg_abs,
            "oi_chg_pct":    oi_chg_pct,
            "ltp":           ltp,
            "price_chg_pct": price_chg_pct,
            "signal":        signal,
            "label":         label,
            "color":         color,
            "bg":            bg,
            "border":        border,
        })

    items.sort(key=lambda x: abs(x["oi_chg_pct"]), reverse=True)

    return {
        "items":       items,
        "count":       len(items),
        "as_of":       datetime.now(timezone.utc).isoformat(),
        "active_date": active_date,
        "is_live":     live,
        "open_time":   to_ist(ts_old),
        "close_time":  to_ist(ts_new),
        "snapshots":   len(today_ts),
    }
