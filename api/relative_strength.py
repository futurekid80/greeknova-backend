from datetime import datetime, timezone

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
    "PAYTM":"NSE:PAYTM","NYKAA":"NSE:NYKAA",
    "PERSISTENT":"NSE:PERSISTENT","DIXON":"NSE:DIXON",
}
INDEX_NSE_MAP = {
    "NIFTY":     "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY":  "NSE:NIFTY FIN SERVICE",
}

def get_relative_strength(benchmark: str = "NIFTY"):
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        return {"error": str(e), "items": []}

    all_map = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}
    bench_key = INDEX_NSE_MAP.get(benchmark.upper(), "NSE:NIFTY 50")

    # Fetch all quotes in one call
    all_keys = list(all_map.values())
    try:
        quotes = kite.quote(all_keys)
    except Exception as e:
        return {"error": str(e), "items": []}

    def pct_change(q):
        ltp   = q.get("last_price", 0)
        prev  = q.get("ohlc", {}).get("close", 0)
        if not prev or prev == 0:
            return 0.0
        return round((ltp - prev) / prev * 100, 2)

    # Benchmark change
    bench_q   = quotes.get(bench_key, {})
    bench_chg = pct_change(bench_q)
    bench_ltp = bench_q.get("last_price", 0)

    items = []
    for sym, kite_key in STOCK_NSE_MAP.items():
        q = quotes.get(kite_key, {})
        if not q:
            continue
        ltp       = q.get("last_price", 0)
        prev      = q.get("ohlc", {}).get("close", 0)
        open_     = q.get("ohlc", {}).get("open", 0)
        chg_pct   = pct_change(q)
        rs        = round(chg_pct - bench_chg, 2)

        # RS signal
        if rs > 1:
            signal, color = "Strong Outperformer", "text-emerald-400"
        elif rs > 0:
            signal, color = "Outperformer",        "text-green-400"
        elif rs > -1:
            signal, color = "Underperformer",      "text-orange-400"
        else:
            signal, color = "Weak Underperformer", "text-red-400"

        items.append({
            "symbol":    sym,
            "ltp":       ltp,
            "prev":      prev,
            "open":      open_,
            "chg_pct":   chg_pct,
            "rs":        rs,
            "signal":    signal,
            "color":     color,
        })

    # Sort by RS descending
    items.sort(key=lambda x: x["rs"], reverse=True)

    return {
        "benchmark":     benchmark.upper(),
        "bench_ltp":     bench_ltp,
        "bench_chg_pct": bench_chg,
        "items":         items,
        "as_of":         datetime.now(timezone.utc).isoformat(),
        "count":         len(items),
    }
