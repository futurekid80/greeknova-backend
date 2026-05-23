from utils.db import get_supabase
from datetime import datetime, timezone, timedelta
import time


INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
STOCKS = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN","BHARTIARTL",
    "KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN","SUNPHARMA","ULTRACEMCO",
    "BAJFINANCE","WIPRO","HCLTECH","TATACONSUM","TATASTEEL","ADANIENT","POWERGRID","NTPC",
    "ONGC","JSWSTEEL","COALINDIA","BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL",
    "BRITANNIA","CIPLA","DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE",
    "INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT","ADANIPORTS",
    "BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS","HAL","INDIGO","PFC",
    "RECLTD","SAIL","TATAPOWER","VEDL",
]

INDEX_NSE_MAP = {
    "NIFTY":    "NSE:NIFTY 50",
    "BANKNIFTY":"NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
}
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
ALL_NSE_MAP = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}


def get_prev_trading_day(kite) -> str:
    """Get previous trading day date — skip weekends."""
    from datetime import date as date_type
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    # Go back 1 day, skip weekends
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev.isoformat()


def compute_cpr(high: float, low: float, close: float) -> dict:
    """Compute CPR levels per Frank Ochoa's Pivot Boss."""
    pivot = (high + low + close) / 3
    bc    = (high + low) / 2
    tc    = (pivot - bc) + pivot

    # Ensure TC is always above BC
    if tc < bc:
        tc, bc = bc, tc

    width_pts = tc - bc
    width_pct = round(width_pts / close * 100, 3) if close > 0 else 0

    return {
        "pivot":     round(pivot, 2),
        "tc":        round(tc, 2),
        "bc":        round(bc, 2),
        "width_pts": round(width_pts, 2),
        "width_pct": width_pct,
    }


def get_cpr_label(width_pct: float) -> dict:
    """CPR width classification per Pivot Boss."""
    if width_pct < 0.15:
        return {"label": "Extremely Narrow", "color": "RED",    "emoji": "🔴", "priority": 1}
    elif width_pct < 0.30:
        return {"label": "Narrow",           "color": "AMBER",  "emoji": "🟡", "priority": 2}
    elif width_pct < 0.60:
        return {"label": "Normal",           "color": "GRAY",   "emoji": "⚪", "priority": 3}
    else:
        return {"label": "Wide",             "color": "BLUE",   "emoji": "🔵", "priority": 4}


def get_cpr_position(cmp: float, tc: float, bc: float) -> dict:
    """Determine price position relative to CPR."""
    if cmp > tc:
        return {"position": "ABOVE_CPR",   "label": "Above CPR",   "bias": "BULLISH", "color": "EMERALD"}
    elif cmp < bc:
        return {"position": "BELOW_CPR",   "label": "Below CPR",   "bias": "BEARISH", "color": "RED"}
    else:
        return {"position": "INSIDE_CPR",  "label": "Inside CPR",  "bias": "NEUTRAL", "color": "AMBER"}


def get_cpr_scanner():
    """
    Main CPR scanner — fetches prev day OHLC from Kite,
    computes CPR for all symbols, enriches with CMP + OI signals.
    """
    supabase = get_supabase()

    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        return {"error": f"Kite auth failed: {e}", "data": []}

    prev_day = get_prev_trading_day(kite)
    today    = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Fetch previous day OHLC from Kite ────────────────────────────────────
    # Use historical_data for each symbol — batch in groups to avoid rate limits
    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

    # Fetch OHLC in batches of 20
    batch_size = 20
    for i in range(0, len(all_symbols), batch_size):
        batch = all_symbols[i:i + batch_size]
        nse_keys = [ALL_NSE_MAP[s] for s in batch if s in ALL_NSE_MAP]

        try:
            ohlc_data = kite.ohlc(nse_keys)
            for sym in batch:
                nse_key = ALL_NSE_MAP.get(sym)
                if nse_key and nse_key in ohlc_data:
                    d = ohlc_data[nse_key]
                    ohlc_map[sym] = {
                        "high":  float(d["ohlc"]["high"]),
                        "low":   float(d["ohlc"]["low"]),
                        "open":  float(d["ohlc"]["open"]),
                        "close": float(d["ohlc"]["close"]),
                        "cmp":   float(d["last_price"]),
                    }
        except Exception as e:
            print(f"[CPR] OHLC batch {i} failed: {e}")

        time.sleep(0.2)

    # ── Get today's CMP from Supabase as fallback ─────────────────────────────
    cmp_rows = []
    try:
        for offset in range(0, 10000, 1000):
            batch = supabase.from_("cmp_prices")\
                .select("symbol, cmp")\
                .gte("timestamp", f"{today}T00:00:00+00:00")\
                .order("timestamp", desc=True)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            cmp_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
    except Exception as e:
        print(f"[CPR] CMP fetch failed: {e}")

    supabase_cmp: dict = {}
    seen = set()
    for r in cmp_rows:
        if r["symbol"] not in seen:
            supabase_cmp[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

    # ── Get active UOA signals for confluence ─────────────────────────────────
    active_signals: dict = {}  # symbol → list of signal types
    try:
        from api.uoa import get_uoa
        uoa_data = get_uoa()
        for sig in uoa_data.get("signals", []):
            sym = sig["symbol"]
            if sym not in active_signals:
                active_signals[sym] = []
            active_signals[sym].append({
                "signal_type": sig["signal_type"],
                "bias":        sig["bias"],
                "option_type": sig["option_type"],
                "strike":      sig["strike"],
                "score":       sig["score"],
            })
    except Exception as e:
        print(f"[CPR] UOA fetch failed: {e}")

    # ── Compute CPR for each symbol ───────────────────────────────────────────
    results = []

    for sym in all_symbols:
        ohlc = ohlc_map.get(sym)
        if not ohlc:
            continue

        high  = ohlc["high"]
        low   = ohlc["low"]
        close = ohlc["close"]
        cmp   = ohlc.get("cmp") or supabase_cmp.get(sym) or close

        if not all([high, low, close]):
            continue

        cpr      = compute_cpr(high, low, close)
        label    = get_cpr_label(cpr["width_pct"])
        position = get_cpr_position(cmp, cpr["tc"], cpr["bc"])

        # OI confluence check
        sym_signals  = active_signals.get(sym, [])
        has_oi_signal = len(sym_signals) > 0
        confluence    = label["priority"] <= 2 and has_oi_signal

        # Best signal for display
        best_signal = None
        if sym_signals:
            best_signal = max(sym_signals, key=lambda s: s["score"])

        results.append({
            "symbol":       sym,
            "is_index":     sym in INDICES,
            "cmp":          round(cmp, 2),
            "prev_high":    round(high, 2),
            "prev_low":     round(low, 2),
            "prev_close":   round(close, 2),
            "pivot":        cpr["pivot"],
            "tc":           cpr["tc"],
            "bc":           cpr["bc"],
            "width_pts":    cpr["width_pts"],
            "width_pct":    cpr["width_pct"],
            "width_label":  label["label"],
            "width_color":  label["color"],
            "width_emoji":  label["emoji"],
            "width_priority": label["priority"],
            "cpr_position": position["position"],
            "position_label": position["label"],
            "position_bias":  position["bias"],
            "position_color": position["color"],
            "has_oi_signal":  has_oi_signal,
            "confluence":     confluence,
            "oi_signals":     sym_signals[:3],  # top 3 signals
            "best_signal":    best_signal,
        })

    # Sort: confluence first, then by width_pct ascending (narrowest first)
    results.sort(key=lambda x: (not x["confluence"], x["width_priority"], x["width_pct"]))

    return {
        "data":      results,
        "total":     len(results),
        "prev_day":  prev_day,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
    }
