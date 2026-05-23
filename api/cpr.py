from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
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


def compute_cpr(high: float, low: float, close: float) -> dict:
    """Compute CPR levels per Frank Ochoa's Pivot Boss."""
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
    """
    CPR Trend vs previous day per Pivot Boss:
    - ASCENDING: today's CPR entirely above yesterday's
    - DESCENDING: today's CPR entirely below yesterday's
    - SIDEWAYS: CPRs overlap
    """
    if prev_tc is None or prev_bc is None:
        return "UNKNOWN"
    if bc > prev_tc:
        return "ASCENDING"
    elif tc < prev_bc:
        return "DESCENDING"
    else:
        return "SIDEWAYS"


def get_cpr_status(cmp: float, tc: float, bc: float) -> str:
    """Intraday CPR persistence status."""
    if cmp > tc * 1.002:    # price clearly above TC
        return "HOLDING_ABOVE"
    elif cmp < bc * 0.998:  # price clearly below BC
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


def compute_and_store_cpr(trade_date: str = None):
    """
    Compute CPR for all symbols using previous day OHLC from Kite.
    Store in cpr_levels table. Called by EOD routine at 3:35 PM.
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

    # trade_date = next trading day (tomorrow or Monday)
    if not trade_date:
        next_day = today + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        trade_date = next_day.isoformat()

    all_symbols = INDICES + STOCKS

    # ── Fetch today's completed OHLC from Kite historical API ────────────────
    # Use historical_data with day interval to get today's final candle
    # This ensures we use completed OHLC not intraday partial
    ohlc_map: dict = {}

    # First try instruments list to get tokens
    try:
        instruments = kite.instruments("NSE")
        token_map: dict = {}
        for inst in instruments:
            if inst["tradingsymbol"] in STOCKS:
                token_map[inst["tradingsymbol"]] = inst["instrument_token"]
        # Add index tokens
        index_tokens = {
            "NIFTY":    256265,
            "BANKNIFTY":260105,
            "FINNIFTY": 257801,
        }
        token_map.update(index_tokens)
    except Exception as e:
        print(f"[CPR] Instruments fetch failed: {e}")
        token_map = {}

    # Fetch historical data for each symbol
    today_str = today.isoformat()
    for sym in all_symbols:
        token = token_map.get(sym)
        if not token:
            continue
        try:
            hist = kite.historical_data(
                instrument_token=token,
                from_date=today_str,
                to_date=today_str,
                interval="day"
            )
            if hist:
                candle = hist[-1]
                ohlc_map[sym] = {
                    "high":  float(candle["high"]),
                    "low":   float(candle["low"]),
                    "close": float(candle["close"]),
                }
            time.sleep(0.05)
        except Exception as e:
            print(f"[CPR] Historical {sym}: {e}")

    # Fallback to ohlc() for any missing symbols
    missing = [s for s in all_symbols if s not in ohlc_map]
    if missing:
        batch_size = 20
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            nse_keys = [ALL_NSE_MAP[s] for s in batch if s in ALL_NSE_MAP]
            try:
                ohlc_data = kite.ohlc(nse_keys)
                for sym in batch:
                    nse_key = ALL_NSE_MAP.get(sym)
                    if nse_key and nse_key in ohlc_data:
                        d = ohlc_data[nse_key]
                        if sym not in ohlc_map:
                            ohlc_map[sym] = {
                                "high":  float(d["ohlc"]["high"]),
                                "low":   float(d["ohlc"]["low"]),
                                "close": float(d["ohlc"]["close"]),
                            }
            except Exception as e:
                print(f"[CPR] OHLC fallback batch {i}: {e}")
            time.sleep(0.2)

    # ── Get previous day's CPR for trend calculation ──────────────────────────
    prev_cpr_map: dict = {}
    try:
        prev_rows = supabase.from_("cpr_levels")\
            .select("symbol, tc, bc")\
            .lt("trade_date", trade_date)\
            .order("trade_date", desc=True)\
            .limit(len(all_symbols) * 2)\
            .execute()
        seen_prev = set()
        for r in (prev_rows.data or []):
            if r["symbol"] not in seen_prev:
                prev_cpr_map[r["symbol"]] = {"tc": float(r["tc"]), "bc": float(r["bc"])}
                seen_prev.add(r["symbol"])
    except Exception as e:
        print(f"[CPR] Prev CPR fetch: {e}")

    # ── Compute and store CPR for each symbol ─────────────────────────────────
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
        trend = get_cpr_trend(
            cpr["tc"], cpr["bc"],
            prev.get("tc"), prev.get("bc")
        )

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

    # Upsert to Supabase
    if records:
        for i in range(0, len(records), 50):
            supabase.table("cpr_levels")\
                .upsert(records[i:i+50], on_conflict="trade_date,symbol")\
                .execute()

    print(f"[CPR] Stored {len(records)} CPR records for {trade_date}")
    return {"stored": len(records), "trade_date": trade_date}


def update_cpr_status():
    """
    Update intraday CPR persistence status.
    Called every 5 mins by capture cycle during market hours.
    """
    supabase = get_supabase()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date().isoformat()

    # Get today's CPR levels
    cpr_rows = supabase.from_("cpr_levels")\
    .select("*")\
    .gte("trade_date", today)\
    .order("trade_date", desc=False)\
    .limit(500)\
    .execute()

    if not cpr_rows.data:
        return

    # Get latest CMP
    cmp_rows = supabase.from_("cmp_prices")\
        .select("symbol, cmp")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
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

        status   = get_cpr_status(cmp, tc, bc)
        is_virgin = row.get("is_virgin", True)

        # Once price enters CPR zone, it's no longer virgin
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
                .eq("trade_date", today)\
                .eq("symbol", sym)\
                .execute()
        except Exception as e:
            print(f"[CPR] Status update {sym}: {e}")


def get_cpr_scanner():
    """
    Read CPR from Supabase table — fast, accurate.
    Falls back to live computation if table empty.
    """
    supabase = get_supabase()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date().isoformat()

    # Try reading from table first
    # Get today's CPR levels
    cpr_rows = supabase.from_("cpr_levels")\
    .select("*")\
    .gte("trade_date", today)\
    .order("trade_date", desc=False)\
    .limit(500)\
    .execute()

    if not cpr_rows.data:
        return

    if not cpr_rows.data:
        # Fallback — compute live from Kite
        print("[CPR] No table data for today — computing live")
        return _get_cpr_live()

    # ── Get active UOA signals for confluence ─────────────────────────────────
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
                "strike":      sig["strike"],
                "score":       sig["score"],
            })
    except Exception as e:
        print(f"[CPR] UOA fetch failed: {e}")

    results = []
    for row in cpr_rows.data:
        sym      = row["symbol"]
        tc       = float(row["tc"])
        bc       = float(row["bc"])
        cmp      = float(row["last_cmp"]) if row.get("last_cmp") else float(row["prev_close"])
        position = get_cpr_position(cmp, tc, bc)

        sym_signals   = active_signals.get(sym, [])
        has_oi_signal = len(sym_signals) > 0
        confluence    = row["width_priority"] <= 2 and has_oi_signal
        best_signal   = max(sym_signals, key=lambda s: s["score"]) if sym_signals else None

        # CPR trend label
        trend_labels = {
            "ASCENDING":  {"label": "↑ Ascending", "color": "EMERALD"},
            "DESCENDING": {"label": "↓ Descending","color": "RED"},
            "SIDEWAYS":   {"label": "→ Sideways",  "color": "GRAY"},
            "UNKNOWN":    {"label": "— Unknown",   "color": "GRAY"},
        }
        trend_info = trend_labels.get(row.get("cpr_trend", "UNKNOWN"), trend_labels["UNKNOWN"])

        # CPR status label
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
        })

    results.sort(key=lambda x: (not x["confluence"], x["width_priority"], x["width_pct"]))

    return {
        "data":             results,
        "total":            len(results),
        "trade_date":       today,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "table",
    }


def _get_cpr_live():
    """Fallback — compute CPR live from Kite when table is empty."""
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        return {"error": str(e), "data": []}

    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

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
                        "close": float(d["ohlc"]["close"]),
                        "cmp":   float(d["last_price"]),
                    }
        except Exception as e:
            print(f"[CPR] Live OHLC batch {i}: {e}")
        time.sleep(0.2)

    # Get CMP from Supabase
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
    except:
        pass

    supabase_cmp: dict = {}
    seen = set()
    for r in cmp_rows:
        if r["symbol"] not in seen:
            supabase_cmp[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

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
                "strike":      sig["strike"],
                "score":       sig["score"],
            })
    except:
        pass

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

        sym_signals   = active_signals.get(sym, [])
        has_oi_signal = len(sym_signals) > 0
        confluence    = label["priority"] <= 2 and has_oi_signal
        best_signal   = max(sym_signals, key=lambda s: s["score"]) if sym_signals else None

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
            "cpr_status":     None,
            "status_label":   None,
            "status_color":   None,
            "cpr_position":   position["position"],
            "position_label": position["label"],
            "position_bias":  position["bias"],
            "position_color": position["color"],
            "has_oi_signal":  has_oi_signal,
            "confluence":     confluence,
            "oi_signals":     sym_signals[:3],
            "best_signal":    best_signal,
        })

    results.sort(key=lambda x: (not x["confluence"], x["width_priority"], x["width_pct"]))

    return {
        "data":             results,
        "total":            len(results),
        "trade_date":       today,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "live",
    }
