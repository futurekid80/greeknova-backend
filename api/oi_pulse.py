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
    rows = supabase.from_("oi_snapshots") \
        .select("symbol, oi") \
        .eq("timestamp", timestamp) \
        .limit(50000) \
        .execute()
    return rows.data or []


def get_timestamps_for_date(supabase, date_str: str) -> list:
    result = supabase.from_("oi_snapshots") \
        .select("timestamp") \
        .eq("symbol", "NIFTY") \
        .gte("timestamp", f"{date_str}T00:00:00+00:00") \
        .lt("timestamp", f"{date_str}T23:59:59+00:00") \
        .order("timestamp", desc=False) \
        .limit(200) \
        .execute()
    return sorted(set(r["timestamp"] for r in (result.data or [])))


def get_last_full_trading_day(supabase, before_date: str) -> list:
    """Find the most recent date BEFORE before_date that has 5+ snapshots (full session)."""
    base = datetime.strptime(before_date, '%Y-%m-%d')
    for days_back in range(1, 8):
        check = (base - timedelta(days=days_back)).strftime('%Y-%m-%d')
        ts = get_timestamps_for_date(supabase, check)
        if len(ts) >= 5:
            return ts
    return []


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


def get_oi_pulse(filter_type: str = "all"):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Step 1: Find active date (today or last trading day) ─────────────────
    today_ts = get_timestamps_for_date(supabase, today)

    if len(today_ts) >= 5:
        # Full trading day today
        active_date = today
        ts_new = today_ts[-1]
        # Compare vs previous trading day close
        prev_ts = get_last_full_trading_day(supabase, today)
        ts_old = prev_ts[-1] if prev_ts else today_ts[0]
        is_live = True
    elif len(today_ts) >= 1:
        # Partial day today (weekend capture / pre-market) — use latest snapshot
        # and compare vs last full trading day
        active_date = today
        ts_new = today_ts[-1]
        prev_ts = get_last_full_trading_day(supabase, today)
        ts_old = prev_ts[-1] if prev_ts else today_ts[0]
        is_live = False
    else:
        # No data today — find last full trading day
        prev_ts = get_last_full_trading_day(supabase, today)
        if not prev_ts:
            return {
                "items": [], "as_of": datetime.now(timezone.utc).isoformat(),
                "count": 0, "message": "No data found in the last 7 days"
            }
        active_date = prev_ts[-1][:10]
        ts_new = prev_ts[-1]
        # Compare vs the day before that
        prev_prev_ts = get_last_full_trading_day(supabase, active_date)
        ts_old = prev_prev_ts[-1] if prev_prev_ts else prev_ts[0]
        is_live = False

    # ── Step 2: Fetch OI for both snapshots ───────────────────────────────────
    old_rows = fetch_all_paginated(supabase, ts_old)
    new_rows = fetch_all_paginated(supabase, ts_new)

    # ── Step 3: Aggregate OI per symbol ───────────────────────────────────────
    oi_old = defaultdict(int)
    oi_new = defaultdict(int)
    for r in old_rows:
        oi_old[r["symbol"]] += r["oi"] or 0
    for r in new_rows:
        oi_new[r["symbol"]] += r["oi"] or 0

    # ── Step 4: Get prices ────────────────────────────────────────────────────
    prices = {}
    if is_live:
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
        # Fallback: use stored CMP prices for active date
        try:
            cmp_result = supabase.from_("cmp_prices") \
                .select("symbol, cmp") \
                .gte("timestamp", f"{active_date}T00:00:00+00:00") \
                .lt("timestamp", f"{active_date}T23:59:59+00:00") \
                .order("timestamp", desc=True) \
                .limit(500) \
                .execute()
            seen = set()
            for r in (cmp_result.data or []):
                if r["symbol"] not in seen:
                    prices[r["symbol"]] = {"ltp": r["cmp"], "prev_close": r["cmp"]}
                    seen.add(r["symbol"])
        except Exception as e:
            print(f"[OI Pulse] EOD price fetch failed: {e}")

    # ── Step 5: Build items ───────────────────────────────────────────────────
    items = []
    for sym in ALL_SYMBOLS:
        is_index = sym in INDEX_NSE_MAP
        if filter_type == "index"  and not is_index: continue
        if filter_type == "stocks" and is_index:     continue

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
        "is_live":     is_live,
        "open_time":   to_ist(ts_old),
        "close_time":  to_ist(ts_new),
        "snapshots":   len(today_ts),
    }
