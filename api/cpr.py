from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import time
import time as time_module

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

# ── In-memory cache ───────────────────────────────────────────────────────────
_cpr_cache: dict = {}
_cpr_cache_time: float = 0
_uoa_cache: dict = {}
_uoa_cache_time: float = 0
CPR_CACHE_TTL  = 30
UOA_CACHE_TTL  = 60


def _is_market_hours() -> bool:
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False
    total = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= total <= (15 * 60 + 30)


def compute_cpr(high: float, low: float, close: float) -> dict:
    pivot = (high + low + close) / 3
    bc    = (high + low) / 2
    tc    = (pivot - bc) + pivot
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
    if width_pct < 0.15:
        return {"label": "Extremely Narrow", "color": "RED",   "emoji": "🔴", "priority": 1}
    elif width_pct < 0.30:
        return {"label": "Narrow",           "color": "AMBER", "emoji": "🟡", "priority": 2}
    elif width_pct < 0.60:
        return {"label": "Normal",           "color": "GRAY",  "emoji": "⚪", "priority": 3}
    else:
        return {"label": "Wide",             "color": "BLUE",  "emoji": "🔵", "priority": 4}


def get_cpr_trend(tc: float, bc: float, prev_tc: float, prev_bc: float) -> str:
    if prev_tc is None or prev_bc is None:
        return "UNKNOWN"
    if bc > prev_tc:
        return "ASCENDING"
    elif tc < prev_bc:
        return "DESCENDING"
    else:
        return "SIDEWAYS"


def get_cpr_status(cmp: float, tc: float, bc: float) -> str:
    if cmp > tc * 1.002:
        return "HOLDING_ABOVE"
    elif cmp < bc * 0.998:
        return "HOLDING_BELOW"
    elif cmp > tc:
        return "BROKEN_UP"
    elif cmp < bc:
        return "BROKEN_DOWN"
    else:
        return "INSIDE_CPR"


def get_cpr_position(cmp: float, tc: float, bc: float) -> dict:
    if cmp > tc:
        return {"position": "ABOVE_CPR", "label": "Above CPR", "bias": "BULLISH", "color": "EMERALD"}
    elif cmp < bc:
        return {"position": "BELOW_CPR", "label": "Below CPR", "bias": "BEARISH", "color": "RED"}
    else:
        return {"position": "INSIDE_CPR", "label": "Inside CPR", "bias": "NEUTRAL", "color": "AMBER"}


def _get_uoa_signals_cached() -> dict:
    global _uoa_cache, _uoa_cache_time
    if _uoa_cache and (time_module.time() - _uoa_cache_time) < UOA_CACHE_TTL:
        return _uoa_cache
    active_signals: dict = {}
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
                "strike":      float(sig["strike"]),
                "score":       sig["score"],
            })
    except Exception as e:
        print(f"[CPR] UOA fetch failed: {e}")
    _uoa_cache = active_signals
    _uoa_cache_time = time_module.time()
    return active_signals


def compute_and_store_cpr(trade_date: str = None):
    """
    EOD CPR computation — uses kite.historical_data() for official NSE VWAP close.
    Fetches last completed daily candle — same data as TradingView/GoCharting.
    Called at 4:30 PM after market fully settles.
    """
    supabase = get_supabase()
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        print(f"[CPR] Kite auth failed: {e}")
        return {"error": str(e)}

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        next_day = today + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        trade_date = next_day.isoformat()

    print(f"[CPR] Fetching OHLC via historical_data() for trade_date: {trade_date}")

    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

    # ── Build instrument token map ────────────────────────────────────────────
    # Index tokens are hardcoded, stock tokens fetched from instruments list
    INDEX_TOKENS = {
        "NIFTY":    256265,
        "BANKNIFTY": 260105,
        "FINNIFTY":  257801,
    }
    token_map: dict = {**INDEX_TOKENS}
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            if inst["tradingsymbol"] in STOCKS:
                token_map[inst["tradingsymbol"]] = inst["instrument_token"]
        print(f"[CPR] Got {len(token_map)} instrument tokens")
    except Exception as e:
        print(f"[CPR] Instruments fetch failed: {e}")

    # ── Fetch OHLC via historical_data — last completed daily candle ──────────
    # historical_data with interval="day" returns official VWAP-based close
    # candles[-1] is always the last completed candle (today if after 3:30 PM)
    from_date = (today - timedelta(days=10)).isoformat()
    to_date   = today.isoformat()

    for sym in all_symbols:
        token = token_map.get(sym)
        if not token:
            print(f"[CPR] No token for {sym}, skipping")
            continue
        try:
            candles = kite.historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval="day",
                continuous=False,
                oi=False,
            )
            if candles:
                # Use the last candle whose date matches today
                # If NSE hasn't finalized today's candle yet, fall back to previous day
                import pytz as _pytz
                _ist = _pytz.timezone('Asia/Kolkata')
                _today_str = datetime.now(_ist).date().isoformat()
                c = candles[-1]
                # Check if candle date matches today
                candle_date = str(c["date"])[:10] if c.get("date") else ""
                if candle_date != _today_str and len(candles) >= 2:
                    # Today's candle not settled yet, use previous day
                    c = candles[-2]
                elif candle_date != _today_str:
                    c = candles[-1]
                ohlc_map[sym] = {
                    "high":  float(c["high"]),
                    "low":   float(c["low"]),
                    "close": float(c["close"]),
                }
            time.sleep(0.05)
        except Exception as e:
            print(f"[CPR] historical_data {sym}: {e}")

    print(f"[CPR] Got OHLC for {len(ohlc_map)} symbols")

    # ── Fetch previous CPR for trend calculation ──────────────────────────────
    prev_cpr_map: dict = {}
    try:
        prev_rows = supabase.from_("cpr_levels")\
            .select("symbol, tc, bc")\
            .lt("trade_date", trade_date)\
            .limit(len(all_symbols) * 2)\
            .execute()
        seen_prev = set()
        for r in (prev_rows.data or []):
            if r["symbol"] not in seen_prev:
                prev_cpr_map[r["symbol"]] = {"tc": float(r["tc"]), "bc": float(r["bc"])}
                seen_prev.add(r["symbol"])
    except Exception as e:
        print(f"[CPR] Prev CPR fetch: {e}")

    # ── Compute and store CPR records ─────────────────────────────────────────
    records = []
    for sym in all_symbols:
        ohlc = ohlc_map.get(sym)
        if not ohlc:
            continue
        high  = ohlc["high"]
        low   = ohlc["low"]
        close = ohlc["close"]
        if not all([high, low, close]):
            continue
        cpr   = compute_cpr(high, low, close)
        label = get_cpr_label(cpr["width_pct"])
        prev  = prev_cpr_map.get(sym, {})
        trend = get_cpr_trend(cpr["tc"], cpr["bc"], prev.get("tc"), prev.get("bc"))
        records.append({
            "trade_date":    trade_date,
            "symbol":        sym,
            "is_index":      sym in INDICES,
            "prev_high":     high,
            "prev_low":      low,
            "prev_close":    close,
            "pivot":         cpr["pivot"],
            "tc":            cpr["tc"],
            "bc":            cpr["bc"],
            "width_pts":     cpr["width_pts"],
            "width_pct":     cpr["width_pct"],
            "width_label":   label["label"],
            "width_color":   label["color"],
            "width_emoji":   label["emoji"],
            "width_priority":label["priority"],
            "prev_tc":       prev.get("tc"),
            "prev_bc":       prev.get("bc"),
            "cpr_trend":     trend,
            "is_virgin":     True,
            "cpr_status":    None,
            "last_cmp":      None,
            "status_updated_at": None,
        })

    if records:
        for i in range(0, len(records), 50):
            supabase.table("cpr_levels")\
                .upsert(records[i:i+50], on_conflict="trade_date,symbol")\
                .execute()

    global _cpr_cache, _cpr_cache_time
    _cpr_cache = {}
    _cpr_cache_time = 0

    print(f"[CPR] Stored {len(records)} CPR records for {trade_date}")
    return {"stored": len(records), "trade_date": trade_date}


def update_cpr_status():
    supabase = get_supabase()
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date().isoformat()

    cpr_rows = supabase.from_("cpr_levels")\
        .select("*")\
        .gte("trade_date", today)\
        .limit(500)\
        .execute()

    if not cpr_rows.data:
        return

    cmp_rows = supabase.from_("cmp_prices")\
        .select("symbol, cmp")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .limit(500)\
        .execute()

    cmp_map: dict = {}
    seen = set()
    for r in (cmp_rows.data or []):
        if r["symbol"] not in seen:
            cmp_map[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

    now_utc = datetime.now(timezone.utc).isoformat()

    for row in cpr_rows.data:
        sym = row["symbol"]
        cmp = cmp_map.get(sym)
        if not cmp:
            continue
        tc = float(row["tc"])
        bc = float(row["bc"])
        status    = get_cpr_status(cmp, tc, bc)
        is_virgin = row.get("is_virgin", True)
        if is_virgin and bc <= cmp <= tc:
            is_virgin = False
        try:
            supabase.table("cpr_levels")\
                .update({
                    "cpr_status":        status,
                    "last_cmp":          cmp,
                    "is_virgin":         is_virgin,
                    "status_updated_at": now_utc,
                })\
                .gte("trade_date", today)\
                .eq("symbol", sym)\
                .execute()
        except Exception as e:
            print(f"[CPR] Status update {sym}: {e}")

    global _cpr_cache, _cpr_cache_time
    _cpr_cache = {}
    _cpr_cache_time = 0


def _get_nearest_signal(sym_signals: list, cmp: float) -> dict | None:
    if not sym_signals:
        return None
    qualified = [s for s in sym_signals if s.get("score", 0) >= 3]
    if not qualified:
        return None
    strikes = sorted(set(s.get("strike", 0) for s in qualified if s.get("strike", 0) > 0))
    if len(strikes) >= 2:
        intervals = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
        strike_interval = min(intervals)
    else:
        if cmp > 5000:   strike_interval = 100
        elif cmp > 1000: strike_interval = 50
        elif cmp > 500:  strike_interval = 20
        elif cmp > 100:  strike_interval = 5
        else:            strike_interval = 2.5
    for s in qualified:
        strike = s.get("strike", 0)
        abs_distance = abs(strike - cmp)
        s["otm_distance_pct"]  = round(abs_distance / cmp * 100, 2) if cmp > 0 else 99
        s["strikes_from_atm"]  = round(abs_distance / strike_interval, 1) if strike_interval > 0 else 99
    near_money = [s for s in qualified if s["strikes_from_atm"] <= 2.0]
    if near_money:
        return min(near_money, key=lambda s: s["strikes_from_atm"])
    return None


def get_cpr_scanner():
    global _cpr_cache, _cpr_cache_time

    cache_ttl = 30 if _is_market_hours() else 60
    if _cpr_cache and (time_module.time() - _cpr_cache_time) < cache_ttl:
        return _cpr_cache

    supabase = get_supabase()
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today = now_ist.date().isoformat()

    if now_ist.hour > 16 or (now_ist.hour == 16 and now_ist.minute >= 30):
        from datetime import timedelta
        next_day = now_ist.date() + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        query_date = next_day.isoformat()
    else:
        query_date = today

    cpr_rows = supabase.from_("cpr_levels")\
        .select("*")\
        .eq("trade_date", query_date)\
        .limit(500)\
        .execute()

    if not cpr_rows.data:
        return _get_cpr_live()

    active_signals = _get_uoa_signals_cached()

    results = []
    for row in cpr_rows.data:
        sym      = row["symbol"]
        tc       = float(row["tc"])
        bc       = float(row["bc"])
        cmp      = float(row["last_cmp"]) if row.get("last_cmp") else float(row["prev_close"])
        position = get_cpr_position(cmp, tc, bc)

        sym_signals   = active_signals.get(sym, [])
        has_oi_signal = len(sym_signals) > 0
        cpr_status = row.get("cpr_status")
        holding_score = {
            "HOLDING_ABOVE": 3,
            "HOLDING_BELOW": 3,
            "BROKEN_UP":     2,
            "BROKEN_DOWN":   2,
            "INSIDE_CPR":    0,
        }.get(cpr_status, 1)

        best_signal = _get_nearest_signal(sym_signals, cmp)
        best_is_near_atm = best_signal is not None and (best_signal.get("strikes_from_atm", 99) <= 2.0)
        confluence = row["width_priority"] <= 2 and has_oi_signal and holding_score >= 2 and best_is_near_atm
        if best_signal:
            pos = position["position"]
            sig_type = best_signal.get("signal_type", "")
            if (pos == "BELOW_CPR" and sig_type == "PUT_WRITING") or \
               (pos == "ABOVE_CPR" and sig_type == "CALL_WRITING"):
                best_signal["alignment"] = "⚠️ Contradicts"
                best_signal["alignment_color"] = "AMBER"
            elif (pos == "BELOW_CPR" and sig_type == "CALL_WRITING") or \
                 (pos == "ABOVE_CPR" and sig_type == "PUT_WRITING"):
                best_signal["alignment"] = "✅ Confirms"
                best_signal["alignment_color"] = "EMERALD"
        alignment = (best_signal or {}).get("alignment", "")
        if "Confirms" in alignment:
            confluence_type = "CONFIRMS"
        elif "Contradicts" in alignment:
            confluence_type = "CONTRADICTS"
        elif confluence:
            confluence_type = "NEUTRAL"
        else:
            confluence_type = None

        trend_labels = {
            "ASCENDING":  {"label": "↑ Ascending", "color": "EMERALD"},
            "DESCENDING": {"label": "↓ Descending","color": "RED"},
            "SIDEWAYS":   {"label": "→ Sideways",  "color": "GRAY"},
            "UNKNOWN":    {"label": "— Unknown",   "color": "GRAY"},
        }
        trend_info = trend_labels.get(row.get("cpr_trend", "UNKNOWN"), trend_labels["UNKNOWN"])

        status_labels = {
            "HOLDING_ABOVE": {"label": "✅ Holding Above TC", "color": "EMERALD"},
            "HOLDING_BELOW": {"label": "🔻 Holding Below BC", "color": "RED"},
            "BROKEN_UP":     {"label": "🚀 Broken Above TC",  "color": "EMERALD"},
            "BROKEN_DOWN":   {"label": "💥 Broken Below BC",  "color": "RED"},
            "INSIDE_CPR":    {"label": "⚠️ Inside CPR",       "color": "AMBER"},
        }
        status_info = status_labels.get(row.get("cpr_status"), None)

        results.append({
            "symbol":         sym,
            "is_index":       row.get("is_index", False),
            "cmp":            round(cmp, 2),
            "prev_high":      float(row["prev_high"]),
            "prev_low":       float(row["prev_low"]),
            "prev_close":     float(row["prev_close"]),
            "pivot":          float(row["pivot"]),
            "tc":             tc,
            "bc":             bc,
            "width_pts":      float(row["width_pts"]),
            "width_pct":      float(row["width_pct"]),
            "width_label":    row["width_label"],
            "width_color":    row["width_color"],
            "width_emoji":    row["width_emoji"],
            "width_priority": row["width_priority"],
            "cpr_trend":      row.get("cpr_trend", "UNKNOWN"),
            "trend_label":    trend_info["label"],
            "trend_color":    trend_info["color"],
            "is_virgin":      row.get("is_virgin", True),
            "cpr_status":     row.get("cpr_status"),
            "status_label":   status_info["label"] if status_info else None,
            "status_color":   status_info["color"] if status_info else None,
            "cpr_position":   position["position"],
            "position_label": position["label"],
            "position_bias":  position["bias"],
            "position_color": position["color"],
            "has_oi_signal":  has_oi_signal,
            "confluence":     confluence,
            "oi_signals":     sym_signals[:3],
            "best_signal":    best_signal,
            "confluence_type": confluence_type,
            "holding_score":  holding_score,
        })

    results.sort(key=lambda x: (
        not x["confluence"],
        -x.get("holding_score", 0),
        x["width_priority"],
        x["width_pct"]
    ))

    result = {
        "data":             results,
        "total":            len(results),
        "trade_date":       query_date,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "table",
    }

    _cpr_cache = result
    _cpr_cache_time = time_module.time()
    return result


def _get_cpr_live():
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        return {"error": str(e), "data": []}

    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

    batch_size = 50
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
                        "close": float(d["ohlc"]["close"]),
                        "cmp":   float(d["last_price"]),
                    }
        except Exception as e:
            print(f"[CPR] Live OHLC batch {i}: {e}")
        time.sleep(0.1)

    cmp_rows = []
    try:
        for offset in range(0, 10000, 1000):
            batch = supabase.from_("cmp_prices")\
                .select("symbol, cmp")\
                .gte("timestamp", f"{today}T00:00:00+00:00")\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            cmp_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
    except:
        pass

    supabase_cmp: dict = {}
    seen = set()
    for r in cmp_rows:
        if r["symbol"] not in seen:
            supabase_cmp[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

    active_signals = _get_uoa_signals_cached()

    results = []
    for sym in all_symbols:
        ohlc = ohlc_map.get(sym)
        if not ohlc:
            continue
        high  = ohlc["high"]
        low   = ohlc["low"]
        close = ohlc["close"]
        cmp   = ohlc.get("cmp") or supabase_cmp.get(sym) or close
        cpr      = compute_cpr(high, low, close)
        label    = get_cpr_label(cpr["width_pct"])
        position = get_cpr_position(cmp, cpr["tc"], cpr["bc"])

        live_status = get_cpr_status(cmp, cpr["tc"], cpr["bc"])
        holding_score = {
            "HOLDING_ABOVE": 3,
            "HOLDING_BELOW": 3,
            "BROKEN_UP":     2,
            "BROKEN_DOWN":   2,
            "INSIDE_CPR":    0,
        }.get(live_status, 1)

        sym_signals   = active_signals.get(sym, [])
        has_oi_signal = len(sym_signals) > 0
        best_signal   = _get_nearest_signal(sym_signals, cmp)
        best_is_near_atm = best_signal is not None and (best_signal.get("strikes_from_atm", 99) <= 2.0)
        confluence    = label["priority"] <= 2 and has_oi_signal and holding_score >= 2 and best_is_near_atm
        if best_signal:
            pos = position["position"]
            sig_type = best_signal.get("signal_type", "")
            if (pos == "BELOW_CPR" and sig_type == "PUT_WRITING") or \
               (pos == "ABOVE_CPR" and sig_type == "CALL_WRITING"):
                best_signal["alignment"] = "⚠️ Contradicts"
                best_signal["alignment_color"] = "AMBER"
            elif (pos == "BELOW_CPR" and sig_type == "CALL_WRITING") or \
                 (pos == "ABOVE_CPR" and sig_type == "PUT_WRITING"):
                best_signal["alignment"] = "✅ Confirms"
                best_signal["alignment_color"] = "EMERALD"
        alignment = (best_signal or {}).get("alignment", "")
        if "Confirms" in alignment:
            confluence_type = "CONFIRMS"
        elif "Contradicts" in alignment:
            confluence_type = "CONTRADICTS"
        elif confluence:
            confluence_type = "NEUTRAL"
        else:
            confluence_type = None

        results.append({
            "symbol":         sym,
            "is_index":       sym in INDICES,
            "cmp":            round(cmp, 2),
            "prev_high":      round(high, 2),
            "prev_low":       round(low, 2),
            "prev_close":     round(close, 2),
            "pivot":          cpr["pivot"],
            "tc":             cpr["tc"],
            "bc":             cpr["bc"],
            "width_pts":      cpr["width_pts"],
            "width_pct":      cpr["width_pct"],
            "width_label":    label["label"],
            "width_color":    label["color"],
            "width_emoji":    label["emoji"],
            "width_priority": label["priority"],
            "cpr_trend":      "UNKNOWN",
            "trend_label":    "— Unknown",
            "trend_color":    "GRAY",
            "is_virgin":      True,
            "cpr_status":     live_status,
            "status_label":   None,
            "status_color":   None,
            "cpr_position":   position["position"],
            "position_label": position["label"],
            "position_bias":  position["bias"],
            "position_color": position["color"],
            "has_oi_signal":  has_oi_signal,
            "confluence":     confluence,
            "confluence_type": confluence_type,
            "holding_score":  holding_score,
            "oi_signals":     sym_signals[:3],
            "best_signal":    best_signal,
        })

    results.sort(key=lambda x: (
        not x["confluence"],
        -x.get("holding_score", 0),
        x["width_priority"],
        x["width_pct"]
    ))

    return {
        "data":             results,
        "total":            len(results),
        "trade_date":       today,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "live",
    }
