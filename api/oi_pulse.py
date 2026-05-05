from utils.db import get_supabase
from datetime import datetime, timezone, timedelta

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
}
INDEX_NSE_MAP = {
    "NIFTY":"NSE:NIFTY 50",
    "BANKNIFTY":"NSE:NIFTY BANK",
    "FINNIFTY":"NSE:NIFTY FIN SERVICE",
}

def classify(oi_chg_pct: float, price_chg_pct: float):
    if oi_chg_pct > 0 and price_chg_pct > 0:
        return "LONG_BUILDUP",   "Long Buildup",   "text-emerald-400", "bg-emerald-950/30", "border-emerald-800/40"
    if oi_chg_pct > 0 and price_chg_pct < 0:
        return "SHORT_BUILDUP",  "Short Buildup",  "text-red-400",     "bg-red-950/30",     "border-red-800/40"
    if oi_chg_pct < 0 and price_chg_pct > 0:
        return "SHORT_COVERING", "Short Covering", "text-cyan-400",    "bg-cyan-950/30",    "border-cyan-800/40"
    if oi_chg_pct < 0 and price_chg_pct < 0:
        return "LONG_UNWINDING", "Long Unwinding", "text-orange-400",  "bg-orange-950/30",  "border-orange-800/40"
    return "NEUTRAL", "Neutral", "text-gray-400", "bg-gray-900/30", "border-gray-800"

def get_total_oi(supabase, symbol: str, timestamp: str):
    result = supabase.from_("oi_snapshots")\
        .select("oi")\
        .eq("symbol", symbol)\
        .eq("timestamp", timestamp)\
        .execute()
    return sum(r["oi"] or 0 for r in result.data)

def get_oi_pulse(filter_type: str = "all"):
    supabase = get_supabase()
    now_utc   = datetime.now(timezone.utc)
    today     = now_utc.strftime('%Y-%m-%d')
    yesterday = (now_utc - timedelta(days=1)).strftime('%Y-%m-%d')

    # Get all symbols with data today or yesterday
    recent = supabase.from_("oi_snapshots")\
        .select("symbol, timestamp, is_index")\
        .gte("timestamp", f"{yesterday}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .execute()

    if not recent.data:
        return {"items": [], "as_of": None, "message": "No data yet"}

    # Group by symbol — get latest and previous timestamps
    from collections import defaultdict
    sym_ts    = defaultdict(list)
    sym_index = {}
    for r in recent.data:
        sym_ts[r["symbol"]].append(r["timestamp"])
        sym_index[r["symbol"]] = r["is_index"]

    # Get current prices from Kite
    prices = {}
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        all_map = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}
        symbols_to_fetch = [s for s in sym_ts.keys() if s in all_map]
        kite_keys = [all_map[s] for s in symbols_to_fetch]
        if kite_keys:
            quotes = kite.quote(kite_keys)
            for sym in symbols_to_fetch:
                key = all_map[sym]
                if key in quotes:
                    prices[sym] = {
                        "ltp":        quotes[key]["last_price"],
                        "prev_close": quotes[key].get("ohlc", {}).get("close", quotes[key]["last_price"]),
                    }
    except Exception as e:
        print(f"Price fetch failed: {e}")

    items = []
    for sym, timestamps in sym_ts.items():
        unique_ts = sorted(set(timestamps), reverse=True)
        if len(unique_ts) < 2:
            continue  # Need at least 2 snapshots to compare

        latest_ts = unique_ts[0]
        prev_ts   = unique_ts[1]

        oi_now  = get_total_oi(supabase, sym, latest_ts)
        oi_prev = get_total_oi(supabase, sym, prev_ts)

        if oi_prev == 0:
            continue

        oi_chg_pct = round((oi_now - oi_prev) / oi_prev * 100, 2)
        oi_chg_abs = oi_now - oi_prev

        # Price change
        price_chg_pct = 0.0
        ltp = None
        if sym in prices:
            ltp       = prices[sym]["ltp"]
            prev_close= prices[sym]["prev_close"]
            if prev_close and prev_close > 0:
                price_chg_pct = round((ltp - prev_close) / prev_close * 100, 2)

        signal, label, color, bg, border = classify(oi_chg_pct, price_chg_pct)

        is_index = sym_index.get(sym, False)
        if filter_type == "index"  and not is_index: continue
        if filter_type == "stocks" and is_index:     continue

        items.append({
            "symbol":        sym,
            "is_index":      is_index,
            "oi_now":        oi_now,
            "oi_prev":       oi_prev,
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

    # Sort by absolute OI change %
    items.sort(key=lambda x: abs(x["oi_chg_pct"]), reverse=True)

    return {
        "items":   items,
        "as_of":   now_utc.isoformat(),
        "count":   len(items),
    }
