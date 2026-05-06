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

def get_total_oi_for_symbol(supabase, symbol, timestamp):
    result = supabase.from_("oi_snapshots")\
        .select("oi")\
        .eq("symbol", symbol)\
        .eq("timestamp", timestamp)\
        .execute()
    return sum(r["oi"] or 0 for r in result.data)

def get_oi_pulse(filter_type: str = "all"):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get distinct timestamps for today only
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data))

    if len(timestamps) < 2:
        return {"items": [], "as_of": datetime.now(timezone.utc).isoformat(),
                "count": 0, "message": f"Need 2+ snapshots today — have {len(timestamps)} so far"}

    ts_old = timestamps[0]   # first snapshot (market open)
    ts_new = timestamps[-1]  # latest snapshot

    # Get current prices from Kite
    prices = {}
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        all_map = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}
        kite_keys = list(all_map.values())
        quotes = kite.quote(kite_keys)
        for sym, key in all_map.items():
            if key in quotes:
                q = quotes[key]
                prev = q.get("ohlc", {}).get("close", q["last_price"])
                prices[sym] = {
                    "ltp":        q["last_price"],
                    "prev_close": prev,
                }
    except Exception as e:
        print(f"Price fetch failed: {e}")

    items = []
    for sym in ALL_SYMBOLS:
        is_index = sym in INDEX_NSE_MAP

        if filter_type == "index"  and not is_index: continue
        if filter_type == "stocks" and is_index:     continue

        oi_old = get_total_oi_for_symbol(supabase, sym, ts_old)
        oi_new = get_total_oi_for_symbol(supabase, sym, ts_new)

        if oi_old == 0:
            continue

        oi_chg_pct = round((oi_new - oi_old) / oi_old * 100, 2)
        oi_chg_abs = oi_new - oi_old

        price_chg_pct = 0.0
        ltp = None
        if sym in prices:
            ltp       = prices[sym]["ltp"]
            prev      = prices[sym]["prev_close"]
            if prev and prev > 0:
                price_chg_pct = round((ltp - prev) / prev * 100, 2)

        signal, label, color, bg, border = classify(oi_chg_pct, price_chg_pct)

        items.append({
            "symbol":        sym,
            "is_index":      is_index,
            "oi_now":        oi_new,
            "oi_prev":       oi_old,
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

    # IST time labels
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
        "items":      items,
        "as_of":      datetime.now(timezone.utc).isoformat(),
        "count":      len(items),
        "open_time":  to_ist(ts_old),
        "close_time": to_ist(ts_new),
        "snapshots":  len(timestamps),
    }
