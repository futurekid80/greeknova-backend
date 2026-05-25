from utils.db import get_supabase
from datetime import datetime, timezone, timedelta
import calendar

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
    while d.weekday() != 3:
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
    """
    Determine what is driving OI growth — CE writers or PE writers.
    Returns composition analysis with dominant side and interpretation.
    """
    ce_chg_pct = round((ce_oi_end - ce_oi_start) / ce_oi_start * 100, 1) if ce_oi_start > 0 else 0
    pe_chg_pct = round((pe_oi_end - pe_oi_start) / pe_oi_start * 100, 1) if pe_oi_start > 0 else 0

    total_oi = ce_oi_end + pe_oi_end
    pe_pct_of_total = round(pe_oi_end / total_oi * 100) if total_oi > 0 else 50
    ce_pct_of_total = 100 - pe_pct_of_total

    pcr_series = round(pe_oi_end / ce_oi_end, 2) if ce_oi_end > 0 else 0
    pcr_start  = round(pe_oi_start / ce_oi_start, 2) if ce_oi_start > 0 else 0

    # Determine dominance
    if pe_chg_pct > ce_chg_pct * 1.3:
        dominant     = "PE"
        composition  = "PUT_DOMINATED"
        interp       = "Put writers adding — bullish institutional positioning"
        interp_short = "PE dominated"
        bias_confirm = "BULLISH"
    elif ce_chg_pct > pe_chg_pct * 1.3:
        dominant     = "CE"
        composition  = "CALL_DOMINATED"
        interp       = "Call writers adding — bearish institutional positioning"
        interp_short = "CE dominated"
        bias_confirm = "BEARISH"
    else:
        dominant     = "MIXED"
        composition  = "BALANCED"
        interp       = "Both CE and PE growing equally — mixed positioning"
        interp_short = "Balanced"
        bias_confirm = "NEUTRAL"

    return {
        "ce_oi_chg_pct":    ce_chg_pct,
        "pe_oi_chg_pct":    pe_chg_pct,
        "ce_pct_of_total":  ce_pct_of_total,
        "pe_pct_of_total":  pe_pct_of_total,
        "pcr_series":       pcr_series,
        "pcr_start":        pcr_start,
        "dominant":         dominant,
        "composition":      composition,
        "interp":           interp,
        "interp_short":     interp_short,
        "bias_confirm":     bias_confirm,
    }


def get_conviction_level(consec_days: int, vol_rising: bool, accelerating: bool) -> dict:
    if consec_days >= 3 and (vol_rising or accelerating):
        return {"level": "CONVICTION", "label": "Conviction", "emoji": "🟠", "color": "orange", "rank": 1}
    elif consec_days >= 2:
        return {"level": "BUILDING",   "label": "Building",   "emoji": "🟡", "color": "yellow", "rank": 2}
    else:
        return {"level": "RADAR",      "label": "Radar",      "emoji": "🔵", "color": "blue",   "rank": 3}


def get_today_fut_signals(supabase, today_str: str) -> dict:
    fut_signals = {}
    try:
        ts_result = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("option_type", "FUT")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{today_str}T00:00:00+00:00")\
            .lt("timestamp",  f"{today_str}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .limit(500).execute()

        timestamps = sorted(set(r["timestamp"] for r in (ts_result.data or [])))
        if len(timestamps) < 2:
            return fut_signals

        ts_open   = timestamps[0]
        ts_latest = timestamps[-1]

        def fetch_fut_oi(ts):
            result = supabase.from_("oi_snapshots")\
                .select("symbol, oi, volume, last_price")\
                .eq("timestamp", ts)\
                .eq("option_type", "FUT")\
                .limit(500).execute()
            oi_map = {}
            for r in (result.data or []):
                sym = r["symbol"]
                oi_map[sym] = {"oi": r["oi"] or 0, "vol": r["volume"] or 0, "price": r["last_price"] or 0}
            return oi_map

        open_fut   = fetch_fut_oi(ts_open)
        latest_fut = fetch_fut_oi(ts_latest)

        for sym in latest_fut:
            if sym not in open_fut:
                continue
            oi_open   = open_fut[sym]["oi"]
            oi_latest = latest_fut[sym]["oi"]
            pr_open   = open_fut[sym]["price"]
            pr_latest = latest_fut[sym]["price"]

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


def get_positional_radar(min_consec: int = 0):
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

    trading_dates = []
    check_date = datetime.strptime(series_start, '%Y-%m-%d').date()
    while check_date <= today:
        d = check_date.isoformat()
        check = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .limit(1).execute()
        if check.data:
            trading_dates.append(d)
        check_date += timedelta(days=1)

    if len(trading_dates) < 3:
        return {"error": "Not enough trading days", "series_start": series_start, "expiry": current_expiry, "results": []}

    # ── Batch fetch EOD data per day — now split CE/PE ────────────────────────
    oi_by_date:    dict = {}   # total OI per symbol per day
    ce_oi_by_date: dict = {}   # CE OI per symbol per day
    pe_oi_by_date: dict = {}   # PE OI per symbol per day
    vol_by_date:   dict = {}
    cmp_by_date:   dict = {}

    for d in trading_dates:
        ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()

        if not ts_q.data:
            continue

        eod_ts = ts_q.data[0]["timestamp"]

        raw = []
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("symbol, oi, volume, option_type")\
                .eq("timestamp", eod_ts)\
                .in_("option_type", ["CE", "PE"])\
                .range(offset, offset + 999).execute()
            if not batch.data:
                break
            raw.extend(batch.data)
            if len(batch.data) < 1000:
                break

        sym_oi:    dict = {}
        sym_ce_oi: dict = {}
        sym_pe_oi: dict = {}
        sym_vol:   dict = {}

        for r in raw:
            sym = r["symbol"]
            oi  = r["oi"] or 0
            vol = r["volume"] or 0
            opt = r["option_type"]

            sym_oi[sym]  = sym_oi.get(sym, 0) + oi
            sym_vol[sym] = sym_vol.get(sym, 0) + vol

            if opt == "CE":
                sym_ce_oi[sym] = sym_ce_oi.get(sym, 0) + oi
            elif opt == "PE":
                sym_pe_oi[sym] = sym_pe_oi.get(sym, 0) + oi

        oi_by_date[d]    = sym_oi
        ce_oi_by_date[d] = sym_ce_oi
        pe_oi_by_date[d] = sym_pe_oi
        vol_by_date[d]   = sym_vol

        cmp_q = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(500).execute().data or []

        day_cmp: dict = {}
        seen: set = set()
        for r in cmp_q:
            if r["symbol"] not in seen:
                day_cmp[r["symbol"]] = float(r["cmp"])
                seen.add(r["symbol"])
        cmp_by_date[d] = day_cmp

    available_dates = [d for d in trading_dates if d in oi_by_date]
    total_trading_days = len(available_dates)

    if total_trading_days < 3:
        return {"error": "Insufficient data", "results": []}

    today_fut_signals = get_today_fut_signals(supabase, today_str)

    results = []

    for sym in SYMBOLS:
        oi_series:    list = []
        ce_oi_series: list = []
        pe_oi_series: list = []
        vol_series:   list = []
        cmp_series:   list = []
        date_labels:  list = []

        for d in available_dates:
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

        vol_window   = vol_series[-7:] if len(vol_series) >= 7 else vol_series[:-1]
        vol_avg_7d   = sum(vol_window) / len(vol_window) if vol_window else 0
        vol_today    = vol_series[-1]
        vol_chg_pct  = round((vol_today - vol_avg_7d) / vol_avg_7d * 100, 2) if vol_avg_7d > 0 else 0
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
        consistency_pct = round(match_days / total_intervals * 100) if total_intervals > 0 else 0
        consistency_label = "HIGH" if consistency_pct >= 70 else "MEDIUM" if consistency_pct >= 50 else "LOW"

        accelerating, first_half_chg, second_half_chg = check_acceleration(oi_series, date_labels)

        vol_consec = 0
        for i in range(len(vol_series) - 1, 0, -1):
            if vol_series[i] > vol_series[i-1]: vol_consec += 1
            else: break

        # ── CE/PE composition analysis ────────────────────────────────────────
        composition = get_oi_composition(
            ce_oi_series[0],  ce_oi_series[-1],
            pe_oi_series[0],  pe_oi_series[-1],
        )

        # ── Does composition confirm the signal bias? ─────────────────────────
        # Long Buildup should be PE dominated (put writers = bullish)
        # Short Buildup should be CE dominated (call writers = bearish)
        bias_confirmed = (
            (bias == "BULLISH" and composition["dominant"] == "PE") or
            (bias == "BEARISH" and composition["dominant"] == "CE") or
            composition["dominant"] == "MIXED"
        )

        # ── Conviction level ──────────────────────────────────────────────────
        conviction = get_conviction_level(consec_days, vol_rising, accelerating)

        # ── Ignition ─────────────────────────────────────────────────────────
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

        results.append({
            "symbol":              sym,
            "is_index":            sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "signal":              signal,
            "bias":                bias,

            # Conviction
            "conviction_level":    conviction_display["level"],
            "conviction_label":    conviction_display["label"],
            "conviction_emoji":    conviction_display["emoji"],
            "conviction_color":    conviction_display["color"],
            "conviction_rank":     conviction_display["rank"],
            "ignition":            ignition,
            "fut_signal_today":    fut_signal_today,

            # CE/PE composition — the key new addition
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

            # Backward compat
            "triple_confirm":      triple_confirm,
            "accelerating":        accelerating,

            # Consistency
            "consistency_pct":     consistency_pct,
            "consistency_label":   consistency_label,
            "match_days":          match_days,
            "total_days":          total_intervals,
            "consec_days":         consec_days,

            # Acceleration
            "oi_first_half_chg":   first_half_chg,
            "oi_second_half_chg":  second_half_chg,
            "vol_consec":          vol_consec,

            # % changes
            "oi_chg_pct":          oi_chg_pct,
            "vol_chg_pct":         vol_chg_pct,
            "vol_series_chg":      vol_series_chg,
            "vol_avg_7d":          round(vol_avg_7d / 100000, 1),
            "cmp_chg_pct":         cmp_chg_pct,

            # Sparklines
            "oi_series":           [round(x / 100000, 1) for x in oi_series],
            "ce_oi_series":        [round(x / 100000, 1) for x in ce_oi_series],
            "pe_oi_series":        [round(x / 100000, 1) for x in pe_oi_series],
            "vol_series":          [round(x / 100000, 1) for x in vol_series],
            "cmp_series":          cmp_series,
            "date_labels":         date_labels,
            "cmp":                 cmp_series[-1],
            "series_days":         len(oi_series) - 1,
        })

    # Sort: Ignition first, then by conviction rank, then consistency, then OI%
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

    return {
        "expiry":             current_expiry,
        "series_start":       series_start,
        "total_trading_days": total_trading_days,
        "min_consec":         min_consec,
        "total":              len(results),
        "summary":            summary,
        "results":            results,
    }
