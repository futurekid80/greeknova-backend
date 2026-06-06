from utils.db import get_supabase
from datetime import datetime, timezone, timedelta
import calendar
import time as time_module

# ── Cache — positional radar changes at most once per day ─────────────────────
_radar_cache = {}
_radar_cache_time = 0.0
_CACHE_TTL = 300  # 5 minutes during market hours, longer post-market

SYMBOLS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY",
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH","TATACONSUM",
    "TATASTEEL","ADANIENT","POWERGRID","NTPC","ONGC","JSWSTEEL","COALINDIA",
    "BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA",
    "DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE",
    "INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT",
    "ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS",
    "HAL","INDIGO","PFC","RECLTD","SAIL","TATAPOWER","VEDL",
]


def get_monthly_expiry(year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')


def get_series_start(expiry_date: str) -> str:
    exp_dt = datetime.strptime(expiry_date, '%Y-%m-%d')
    if exp_dt.month == 1:
        prev_year, prev_month = exp_dt.year - 1, 12
    else:
        prev_year, prev_month = exp_dt.year, exp_dt.month - 1
    prev_expiry = get_monthly_expiry(prev_year, prev_month)
    prev_exp_dt = datetime.strptime(prev_expiry, '%Y-%m-%d')
    series_start = prev_exp_dt + timedelta(days=1)
    return series_start.strftime('%Y-%m-%d')


def count_signal_days(oi_series, cmp_series, signal_type):
    count = 0
    for i in range(len(oi_series) - 1, 0, -1):
        oi_up    = oi_series[i]  > oi_series[i-1]
        oi_down  = oi_series[i]  < oi_series[i-1]
        cmp_up   = cmp_series[i] > cmp_series[i-1]
        cmp_down = cmp_series[i] < cmp_series[i-1]
        if signal_type == "LONG_BUILDUP"   and oi_up   and cmp_up:   count += 1
        elif signal_type == "SHORT_BUILDUP"  and oi_up   and cmp_down: count += 1
        elif signal_type == "SHORT_COVERING" and oi_down and cmp_up:   count += 1
        elif signal_type == "LONG_UNWINDING" and oi_down and cmp_down: count += 1
        else: break
    return count


def count_consistent_days(oi_series, cmp_series, vol_series, signal_type):
    total_intervals = len(oi_series) - 1
    if total_intervals <= 0:
        return 0, 0
    match_count = 0
    for i in range(1, len(oi_series)):
        oi_up    = oi_series[i]  > oi_series[i-1]
        oi_down  = oi_series[i]  < oi_series[i-1]
        cmp_up   = cmp_series[i] > cmp_series[i-1]
        cmp_down = cmp_series[i] < cmp_series[i-1]
        if signal_type == "LONG_BUILDUP"   and oi_up   and cmp_up:   match_count += 1
        elif signal_type == "SHORT_BUILDUP"  and oi_up   and cmp_down: match_count += 1
        elif signal_type == "SHORT_COVERING" and oi_down and cmp_up:   match_count += 1
        elif signal_type == "LONG_UNWINDING" and oi_down and cmp_down: match_count += 1
    return match_count, total_intervals


def check_acceleration(oi_series, dates):
    n = len(oi_series)
    if n < 4:
        return False, 0.0, 0.0
    mid = n // 2
    first_half  = oi_series[:mid+1]
    second_half = oi_series[mid:]
    first_chg  = (first_half[-1]  - first_half[0])  / first_half[0]  * 100 if first_half[0]  > 0 else 0
    second_chg = (second_half[-1] - second_half[0]) / second_half[0] * 100 if second_half[0] > 0 else 0
    accelerating = second_chg > first_chg and second_chg > 3.0
    return accelerating, round(first_chg, 2), round(second_chg, 2)


def get_oi_composition(ce_oi_start, ce_oi_end, pe_oi_start, pe_oi_end) -> dict:
    ce_chg_pct = round((ce_oi_end - ce_oi_start) / ce_oi_start * 100, 1) if ce_oi_start > 0 else 0
    pe_chg_pct = round((pe_oi_end - pe_oi_start) / pe_oi_start * 100, 1) if pe_oi_start > 0 else 0
    total_oi = ce_oi_end + pe_oi_end
    pe_pct_of_total = round(pe_oi_end / total_oi * 100) if total_oi > 0 else 50
    ce_pct_of_total = 100 - pe_pct_of_total
    pcr_series = round(pe_oi_end / ce_oi_end, 2) if ce_oi_end > 0 else 0
    pcr_start  = round(pe_oi_start / ce_oi_start, 2) if ce_oi_start > 0 else 0
    if pe_chg_pct > ce_chg_pct * 1.3:
        dominant, composition = "PE", "PUT_DOMINATED"
        interp = "Put writers adding — bullish institutional positioning"
        interp_short, bias_confirm = "PE dominated", "BULLISH"
    elif ce_chg_pct > pe_chg_pct * 1.3:
        dominant, composition = "CE", "CALL_DOMINATED"
        interp = "Call writers adding — bearish institutional positioning"
        interp_short, bias_confirm = "CE dominated", "BEARISH"
    else:
        dominant, composition = "MIXED", "BALANCED"
        interp = "Both CE and PE growing equally — mixed positioning"
        interp_short, bias_confirm = "Balanced", "NEUTRAL"
    return {
        "ce_oi_chg_pct": ce_chg_pct, "pe_oi_chg_pct": pe_chg_pct,
        "ce_pct_of_total": ce_pct_of_total, "pe_pct_of_total": pe_pct_of_total,
        "pcr_series": pcr_series, "pcr_start": pcr_start,
        "dominant": dominant, "composition": composition,
        "interp": interp, "interp_short": interp_short, "bias_confirm": bias_confirm,
    }


def get_conviction_level(consec_days: int, vol_rising: bool, accelerating: bool) -> dict:
    if consec_days >= 3 and (vol_rising or accelerating):
        return {"level": "CONVICTION", "label": "Conviction", "emoji": "🟠", "color": "orange", "rank": 1}
    elif consec_days >= 2:
        return {"level": "BUILDING",   "label": "Building",   "emoji": "🟡", "color": "yellow", "rank": 2}
    else:
        return {"level": "RADAR",      "label": "Radar",      "emoji": "🔵", "color": "blue",   "rank": 3}


def get_today_fut_signals(supabase, today_str: str) -> dict:
    """Fetch today's FUT signals in 2 queries instead of N queries."""
    fut_signals = {}
    try:
        # Get all today's FUT snapshots in one query (include expiry for nearest-expiry filter)
        result = supabase.from_("oi_snapshots")\
            .select("symbol, oi, volume, last_price, timestamp, expiry")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{today_str}T00:00:00+00:00")\
            .lt("timestamp",  f"{today_str}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .limit(15000).execute()

        if not result.data:
            # Fallback to yesterday
            import pytz
            ist = pytz.timezone('Asia/Kolkata')
            check = datetime.now(ist).date() - timedelta(days=1)
            while check.weekday() >= 5:
                check -= timedelta(days=1)
            yesterday = check.isoformat()
            result = supabase.from_("oi_snapshots")\
                .select("symbol, oi, volume, last_price, timestamp, expiry")\
                .eq("option_type", "FUT")\
                .gte("timestamp", f"{yesterday}T00:00:00+00:00")\
                .lt("timestamp",  f"{yesterday}T23:59:59+00:00")\
                .order("timestamp", desc=False)\
                .limit(15000).execute()
            if not result.data:
                return fut_signals

        # Group by (symbol, expiry) to avoid mixing current/next month FUT
        sym_expiry_rows = {}
        for r in (result.data or []):
            sym    = r["symbol"]
            expiry = str(r.get("expiry") or "UNKNOWN")
            key    = (sym, expiry)
            if key not in sym_expiry_rows:
                sym_expiry_rows[key] = []
            sym_expiry_rows[key].append(r)

        # For each symbol pick nearest expiry rows only
        sym_nearest = {}
        for (sym, expiry), exp_rows in sym_expiry_rows.items():
            if sym not in sym_nearest or (expiry != "UNKNOWN" and expiry < sym_nearest[sym][0]):
                sym_nearest[sym] = (expiry, exp_rows)

        for sym, (expiry, sym_exp_rows) in sym_nearest.items():
            rows_sorted = sorted(sym_exp_rows, key=lambda r: r["timestamp"])
            if len(rows_sorted) < 2:
                continue
            open_row   = rows_sorted[0]
            latest_row = rows_sorted[-1]
            oi_open    = open_row["oi"] or 0
            oi_latest  = latest_row["oi"] or 0
            pr_open    = open_row["last_price"] or 0
            pr_latest  = latest_row["last_price"] or 0
            if oi_open <= 0 or pr_open <= 0:
                continue
            oi_chg_pct    = (oi_latest - oi_open) / oi_open * 100
            price_chg_pct = (pr_latest - pr_open) / pr_open * 100
            if abs(oi_chg_pct) < 2.0 or abs(price_chg_pct) < 0.2:
                continue
            if oi_chg_pct > 0 and price_chg_pct > 0:
                fut_signals[sym] = "LONG_BUILDUP"
            elif oi_chg_pct > 0 and price_chg_pct < 0:
                fut_signals[sym] = "SHORT_BUILDUP"
            elif oi_chg_pct < 0 and price_chg_pct > 0:
                fut_signals[sym] = "SHORT_COVERING"
            elif oi_chg_pct < 0 and price_chg_pct < 0:
                fut_signals[sym] = "LONG_UNWINDING"

    except Exception as e:
        print(f"[Positional Radar] FUT signal fetch failed: {e}")
    return fut_signals


from utils.oi_walls import get_oi_walls, get_all_oi_walls


def get_positional_radar(min_consec: int = 0):
    global _radar_cache, _radar_cache_time

    # Serve cache if fresh
    cache_key = str(min_consec)
    if _radar_cache.get(cache_key) and (time_module.time() - _radar_cache_time) < _CACHE_TTL:
        print(f"[Positional Radar] Serving cached result")
        return _radar_cache[cache_key]

    t0 = time_module.time()
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()

    current_expiry = get_monthly_expiry(today.year, today.month)
    if today.strftime('%Y-%m-%d') > current_expiry:
        if today.month == 12:
            current_expiry = get_monthly_expiry(today.year + 1, 1)
        else:
            current_expiry = get_monthly_expiry(today.year, today.month + 1)

    series_start = get_series_start(current_expiry)

    # ── SERVER-SIDE AGGREGATION via RPC ──────────────────────────────────────
    # PostgreSQL aggregates EOD OI per symbol per day — avoids fetching 1M+ rows
    print(f"[Positional Radar] Fetching EOD OI via RPC from {series_start}...")
    rpc_result = supabase.rpc("get_positional_radar_eod", {
        "p_expiry":       current_expiry,
        "p_series_start": series_start,
        "p_series_end":   today_str,
    }).execute()
    all_oi_rows = rpc_result.data or []
    print(f"[Positional Radar] RPC returned {len(all_oi_rows)} rows in {time_module.time()-t0:.1f}s")

    # ── CMP data — EOD CMP per symbol per day via RPC ────────────────────────
    cmp_rpc = supabase.rpc("get_eod_cmp", {
        "p_series_start": series_start,
        "p_series_end":   today_str,
    }).execute()
    all_cmp_rows = cmp_rpc.data or []
    print(f"[Positional Radar] Loaded {len(all_cmp_rows)} CMP rows via RPC in {time_module.time()-t0:.1f}s")

    # ── Build OI maps from RPC result ─────────────────────────────────────────
    # RPC returns: trade_date, symbol, ce_oi, pe_oi, total_oi, total_vol
    oi_by_date    = {}
    ce_oi_by_date = {}
    pe_oi_by_date = {}
    vol_by_date   = {}

    for r in all_oi_rows:
        d   = str(r["trade_date"])
        sym = r["symbol"]
        if d not in oi_by_date:
            oi_by_date[d]    = {}
            ce_oi_by_date[d] = {}
            pe_oi_by_date[d] = {}
            vol_by_date[d]   = {}
        oi_by_date[d][sym]    = int(r["total_oi"] or 0)
        ce_oi_by_date[d][sym] = int(r["ce_oi"] or 0)
        pe_oi_by_date[d][sym] = int(r["pe_oi"] or 0)
        vol_by_date[d][sym]   = int(r["total_vol"] or 0)

    trading_dates = sorted(oi_by_date.keys())
    if len(trading_dates) < 3:
        return {"error": "Not enough trading days", "series_start": series_start, "expiry": current_expiry, "results": []}

    # ── Build CMP map: date -> sym -> eod cmp ────────────────────────────────
    # RPC returns trade_date (already a date string), symbol, cmp
    cmp_by_date = {}
    for r in all_cmp_rows:
        date_str = str(r["trade_date"])
        sym = r["symbol"]
        if date_str not in cmp_by_date:
            cmp_by_date[date_str] = {}
        cmp_by_date[date_str][sym] = float(r["cmp"])

    available_dates = [d for d in trading_dates if d in oi_by_date and d in cmp_by_date]
    total_trading_days = len(available_dates)

    print(f"[Positional Radar] trading_dates={trading_dates}")
    print(f"[Positional Radar] oi_by_date keys={sorted(oi_by_date.keys())}")
    print(f"[Positional Radar] cmp_by_date keys={sorted(cmp_by_date.keys())}")
    print(f"[Positional Radar] available_dates={available_dates}")

    if total_trading_days < 3:
        return {"error": "Insufficient data", "results": [], "debug": {
            "trading_dates": trading_dates,
            "oi_dates": sorted(oi_by_date.keys()),
            "cmp_dates": sorted(cmp_by_date.keys()),
        }}

    print(f"[Positional Radar] {total_trading_days} trading days, processing symbols...")


    # ── Today's FUT signals (2 queries instead of N) ──────────────────────────
    today_fut_signals = get_today_fut_signals(supabase, today_str)

    # ── UOA symbols ───────────────────────────────────────────────────────────
    uoa_symbols: set = set()
    try:
        from api.uoa import get_uoa
        uoa_data = get_uoa()
        for sig in uoa_data.get("signals", []):
            if sig.get("score", 0) >= 3:
                uoa_symbols.add(sig["symbol"])
    except Exception as e:
        print(f"[Positional Radar] UOA check failed: {e}")

    # ── OI walls — fetch once for all symbols ─────────────────────────────────
    # Get latest timestamp for walls
    latest_ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("option_type", "FUT")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today_str}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()
    has_today_data = bool(latest_ts_result.data)

    results = []

    # ── OI Walls — bulk fetch all symbols in ONE query ─────────────────────────
    cmp_latest = {sym: cmp_by_date.get(available_dates[-1], {}).get(sym, 0) for sym in SYMBOLS}
    all_walls = get_all_oi_walls(supabase, cmp_latest)
    print(f"[Positional Radar] OI walls fetched for {len(all_walls)} symbols")

    # ── CPR data — for ignition quality score ─────────────────────────────────
    cpr_rows = supabase.from_("cpr_levels")        .select("symbol, tc, bc, cpr_trend, cpr_position, is_virgin")        .eq("trade_date", today_str)        .limit(200).execute()
    cpr_map = {r["symbol"]: r for r in (cpr_rows.data or [])}
    print(f"[Positional Radar] CPR data fetched for {len(cpr_map)} symbols")

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    ist_today = datetime.now(ist).date()
    cutoff = ist_today if ist_today.isoformat() < current_expiry else (ist_today - timedelta(days=1))

    for sym in SYMBOLS:
        oi_series    = []
        ce_oi_series = []
        pe_oi_series = []
        vol_series   = []
        cmp_series   = []
        date_labels  = []

        for d in available_dates:
            if d > cutoff.isoformat():
                continue
            oi_val  = oi_by_date.get(d, {}).get(sym, 0)
            ce_val  = ce_oi_by_date.get(d, {}).get(sym, 0)
            pe_val  = pe_oi_by_date.get(d, {}).get(sym, 0)
            vol_val = vol_by_date.get(d, {}).get(sym, 0)
            cmp_val = cmp_by_date.get(d, {}).get(sym, 0)
            if oi_val > 0 and cmp_val > 0:
                oi_series.append(oi_val)
                ce_oi_series.append(ce_val)
                pe_oi_series.append(pe_val)
                vol_series.append(vol_val)
                cmp_series.append(cmp_val)
                date_labels.append(d)

        if len(oi_series) < 3:
            continue

        oi_chg_pct  = round((oi_series[-1]  - oi_series[0])  / oi_series[0]  * 100, 2) if oi_series[0]  > 0 else 0
        cmp_chg_pct = round((cmp_series[-1] - cmp_series[0]) / cmp_series[0] * 100, 2) if cmp_series[0] > 0 else 0

        vol_window  = vol_series[-7:] if len(vol_series) >= 7 else vol_series[:-1]
        vol_avg_7d  = sum(vol_window) / len(vol_window) if vol_window else 0
        vol_today   = vol_series[-1]
        vol_chg_pct = round((vol_today - vol_avg_7d) / vol_avg_7d * 100, 2) if vol_avg_7d > 0 else 0
        vol_series_chg = round((vol_series[-1] - vol_series[0]) / vol_series[0] * 100, 2) if vol_series[0] > 0 else 0

        oi_rising    = oi_chg_pct  >  3.0
        oi_falling   = oi_chg_pct  < -3.0
        vol_rising   = vol_chg_pct >  20.0
        price_rising = cmp_chg_pct >  0.5
        price_falling= cmp_chg_pct < -0.5

        if oi_rising and price_rising:
            signal = "LONG_BUILDUP";   bias = "BULLISH"
        elif oi_rising and price_falling:
            signal = "SHORT_BUILDUP";  bias = "BEARISH"
        elif oi_falling and price_rising:
            signal = "SHORT_COVERING"; bias = "BULLISH"
        elif oi_falling and price_falling:
            signal = "LONG_UNWINDING"; bias = "BEARISH"
        else:
            continue

        consec_days = count_signal_days(oi_series, cmp_series, signal)
        if min_consec > 0 and consec_days < min_consec:
            continue

        match_days, total_intervals = count_consistent_days(oi_series, cmp_series, vol_series, signal)
        consistency_pct   = round(match_days / total_intervals * 100) if total_intervals > 0 else 0
        consistency_label = "HIGH" if consistency_pct >= 70 else "MEDIUM" if consistency_pct >= 50 else "LOW"

        accelerating, first_half_chg, second_half_chg = check_acceleration(oi_series, date_labels)

        vol_consec = 0
        for i in range(len(vol_series) - 1, 0, -1):
            if vol_series[i] > vol_series[i-1]: vol_consec += 1
            else: break

        composition = get_oi_composition(
            ce_oi_series[0], ce_oi_series[-1],
            pe_oi_series[0], pe_oi_series[-1],
        )

        bias_confirmed = (
            (bias == "BULLISH" and composition["dominant"] == "PE") or
            (bias == "BEARISH" and composition["dominant"] == "CE") or
            composition["dominant"] == "MIXED"
        )

        conviction = get_conviction_level(consec_days, vol_rising, accelerating)

        fut_signal_today = today_fut_signals.get(sym)
        ignition = False
        if conviction["level"] == "CONVICTION" and fut_signal_today:
            fut_bullish = fut_signal_today in ("LONG_BUILDUP", "SHORT_COVERING")
            fut_bearish = fut_signal_today in ("SHORT_BUILDUP", "LONG_UNWINDING")
            if (bias == "BULLISH" and fut_bullish) or (bias == "BEARISH" and fut_bearish):
                ignition = True

        conviction_display = {
            "level": "IGNITION", "label": "Ignition", "emoji": "🟢", "color": "emerald", "rank": 0
        } if ignition else conviction

        triple_confirm = conviction["level"] == "CONVICTION"

        # ── Ignition Quality Score (only computed for Ignition stocks) ─────────
        ignition_score = 0
        ignition_score_breakdown = {}
        if ignition:
            cpr = cpr_map.get(sym, {})
            cpr_position = cpr.get("cpr_position", "")
            cpr_trend    = cpr.get("cpr_trend", "")

            # 1. Sustained direction (consec_days >= 3) — baseline for ignition
            s1 = consec_days >= 3
            ignition_score += 1 if s1 else 0

            # 2. High consistency (>= 70% of series days match signal)
            s2 = consistency_label == "HIGH"
            ignition_score += 1 if s2 else 0

            # 3. Bias confirmed by CE/PE composition
            s3 = bias_confirmed
            ignition_score += 1 if s3 else 0

            # 4. CPR position AND trend confirms bias
            if bias == "BEARISH":
                s4 = cpr_position in ("BELOW_CPR",) or cpr_trend in ("DESCENDING",)
            else:
                s4 = cpr_position in ("ABOVE_CPR",) or cpr_trend in ("ASCENDING",)
            ignition_score += 1 if s4 else 0

            # 5. Volume building for 2+ consecutive days (accumulation not spike)
            s5 = vol_consec >= 2
            ignition_score += 1 if s5 else 0

            ignition_score_breakdown = {
                "consec_3plus":    s1,
                "high_consistency": s2,
                "bias_confirmed":  s3,
                "cpr_confirms":    s4,
                "vol_building":    s5,
            }

        results.append({
            "symbol":              sym,
            "is_index":            sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "signal":              signal,
            "bias":                bias,
            "conviction_level":    conviction_display["level"],
            "conviction_label":    conviction_display["label"],
            "conviction_emoji":    conviction_display["emoji"],
            "conviction_color":    conviction_display["color"],
            "conviction_rank":     conviction_display["rank"],
            "ignition":            ignition,
            "fut_signal_today":    fut_signal_today,
            "ce_oi_chg_pct":       composition["ce_oi_chg_pct"],
            "pe_oi_chg_pct":       composition["pe_oi_chg_pct"],
            "ce_pct_of_total":     composition["ce_pct_of_total"],
            "pe_pct_of_total":     composition["pe_pct_of_total"],
            "pcr_series":          composition["pcr_series"],
            "pcr_start":           composition["pcr_start"],
            "dominant":            composition["dominant"],
            "composition":         composition["composition"],
            "composition_interp":  composition["interp"],
            "composition_short":   composition["interp_short"],
            "bias_confirmed":      bias_confirmed,
            "triple_confirm":      triple_confirm,
            "accelerating":        accelerating,
            "consistency_pct":     consistency_pct,
            "consistency_label":   consistency_label,
            "match_days":          match_days,
            "total_days":          total_intervals,
            "consec_days":         consec_days,
            "oi_first_half_chg":   first_half_chg,
            "oi_second_half_chg":  second_half_chg,
            "vol_consec":          vol_consec,
            "oi_chg_pct":          oi_chg_pct,
            "vol_chg_pct":         vol_chg_pct,
            "vol_series_chg":      vol_series_chg,
            "vol_avg_7d":          round(vol_avg_7d / 100000, 1),
            "cmp_chg_pct":         cmp_chg_pct,
            "oi_series":           [round(x / 100000, 1) for x in oi_series],
            "ce_oi_series":        [round(x / 100000, 1) for x in ce_oi_series],
            "pe_oi_series":        [round(x / 100000, 1) for x in pe_oi_series],
            "vol_series":          [round(x / 100000, 1) for x in vol_series],
            "cmp_series":          cmp_series,
            "date_labels":         date_labels,
            "cmp":                 cmp_series[-1],
            "series_days":         len(oi_series) - 1,
            "has_uoa":             sym in uoa_symbols,
            "ignition_score":      ignition_score,
            "ignition_score_breakdown": ignition_score_breakdown,
            **all_walls.get(sym, {}),
        })

    results.sort(key=lambda x: (
        x["conviction_rank"],
        {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["consistency_label"]],
        0 if x["bias_confirmed"] else 1,
        -abs(x["oi_chg_pct"])
    ))

    summary = {
        "long_buildup":     sum(1 for r in results if r["signal"] == "LONG_BUILDUP"),
        "short_buildup":    sum(1 for r in results if r["signal"] == "SHORT_BUILDUP"),
        "short_covering":   sum(1 for r in results if r["signal"] == "SHORT_COVERING"),
        "long_unwinding":   sum(1 for r in results if r["signal"] == "LONG_UNWINDING"),
        "high_consistency": sum(1 for r in results if r["consistency_label"] == "HIGH"),
        "triple_confirm":   sum(1 for r in results if r["triple_confirm"]),
        "accelerating":     sum(1 for r in results if r["accelerating"]),
        "conviction":       sum(1 for r in results if r["conviction_level"] == "CONVICTION"),
        "ignition":         sum(1 for r in results if r["conviction_level"] == "IGNITION"),
        "building":         sum(1 for r in results if r["conviction_level"] == "BUILDING"),
        "radar":            sum(1 for r in results if r["conviction_level"] == "RADAR"),
        "bias_confirmed":   sum(1 for r in results if r["bias_confirmed"]),
        "pe_dominated":     sum(1 for r in results if r["dominant"] == "PE"),
        "ce_dominated":     sum(1 for r in results if r["dominant"] == "CE"),
    }

    result = {
        "expiry":             current_expiry,
        "series_start":       series_start,
        "total_trading_days": total_trading_days,
        "min_consec":         min_consec,
        "total":              len(results),
        "summary":            summary,
        "results":            results,
    }

    # Cache result
    _radar_cache[cache_key] = result
    _radar_cache_time = time_module.time()
    print(f"[Positional Radar] Done in {time_module.time()-t0:.1f}s — {len(results)} signals")
    return result
