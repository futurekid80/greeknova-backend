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
    """Last Thursday of the month = NSE monthly expiry"""
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day)
    # Walk back to last Thursday (weekday 3)
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')


def get_series_start(expiry_date: str) -> str:
    """
    Monthly series starts day after previous month's expiry.
    Previous expiry = last Thursday of previous month.
    """
    exp_dt = datetime.strptime(expiry_date, '%Y-%m-%d')
    # Previous month
    if exp_dt.month == 1:
        prev_year, prev_month = exp_dt.year - 1, 12
    else:
        prev_year, prev_month = exp_dt.year, exp_dt.month - 1
    prev_expiry = get_monthly_expiry(prev_year, prev_month)
    prev_exp_dt = datetime.strptime(prev_expiry, '%Y-%m-%d')
    # Series starts day after previous expiry
    series_start = prev_exp_dt + timedelta(days=1)
    return series_start.strftime('%Y-%m-%d')


def count_signal_days(oi_series, cmp_series, signal_type):
    """
    Count how many consecutive intervals (day pairs) the signal held
    at the END of the series.
    """
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
    """
    Count ALL days (not just consecutive) where signal condition held.
    Used for consistency % calculation.
    """
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
    """
    Compare OI % change in first half vs second half of series.
    Acceleration = second half growing faster than first half.
    """
    n = len(oi_series)
    if n < 4:
        return False, 0.0, 0.0

    mid = n // 2
    first_half  = oi_series[:mid+1]
    second_half = oi_series[mid:]

    first_chg  = (first_half[-1]  - first_half[0])  / first_half[0]  * 100  if first_half[0]  > 0 else 0
    second_chg = (second_half[-1] - second_half[0]) / second_half[0] * 100 if second_half[0] > 0 else 0

    accelerating = second_chg > first_chg and second_chg > 3.0
    return accelerating, round(first_chg, 2), round(second_chg, 2)


def get_positional_radar(min_consec: int = 0):
    """
    Monthly expiry-based positional analysis.
    Covers entire current expiry series (prev expiry end → today).
    min_consec: minimum consecutive days filter (0 = show all)
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()

    # ── Current monthly expiry ────────────────────────────────────────────────
    current_expiry = get_monthly_expiry(today.year, today.month)
    # If today is past this month's expiry, use next month
    if today.strftime('%Y-%m-%d') > current_expiry:
        if today.month == 12:
            current_expiry = get_monthly_expiry(today.year + 1, 1)
        else:
            current_expiry = get_monthly_expiry(today.year, today.month + 1)

    series_start = get_series_start(current_expiry)

    # ── Find all trading dates in this series ─────────────────────────────────
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
        return {
            "error": "Not enough trading days in current series",
            "series_start": series_start,
            "expiry": current_expiry,
            "results": []
        }

    # ── Batch fetch EOD data per day ──────────────────────────────────────────
    oi_by_date:  dict = {}
    vol_by_date: dict = {}
    cmp_by_date: dict = {}

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
                .select("symbol, oi, volume")\
                .eq("timestamp", eod_ts)\
                .range(offset, offset + 999).execute()
            if not batch.data:
                break
            raw.extend(batch.data)
            if len(batch.data) < 1000:
                break

        sym_oi:  dict = {}
        sym_vol: dict = {}
        for r in raw:
            sym = r["symbol"]
            sym_oi[sym]  = sym_oi.get(sym, 0)  + (r["oi"]     or 0)
            sym_vol[sym] = sym_vol.get(sym, 0) + (r["volume"] or 0)

        oi_by_date[d]  = sym_oi
        vol_by_date[d] = sym_vol

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

    # ── Per-symbol analysis ───────────────────────────────────────────────────
    results = []

    for sym in SYMBOLS:
        oi_series  = []
        vol_series = []
        cmp_series = []
        date_labels = []

        for d in available_dates:
            oi_val  = oi_by_date.get(d, {}).get(sym, 0)
            vol_val = vol_by_date.get(d, {}).get(sym, 0)
            cmp_val = cmp_by_date.get(d, {}).get(sym, 0)
            if oi_val > 0 and cmp_val > 0:
                oi_series.append(oi_val)
                vol_series.append(vol_val)
                cmp_series.append(cmp_val)
                date_labels.append(d)

        if len(oi_series) < 3:
            continue

        # Overall % changes (series start → today)
        oi_chg_pct  = round((oi_series[-1]  - oi_series[0])  / oi_series[0]  * 100, 2) if oi_series[0]  > 0 else 0
        vol_chg_pct = round((vol_series[-1] - vol_series[0]) / vol_series[0] * 100, 2) if vol_series[0] > 0 else 0
        cmp_chg_pct = round((cmp_series[-1] - cmp_series[0]) / cmp_series[0] * 100, 2) if cmp_series[0] > 0 else 0

        # ── Classify overall signal ───────────────────────────────────────────
        oi_rising    = oi_chg_pct  >  3.0
        oi_falling   = oi_chg_pct  < -3.0
        vol_rising   = vol_chg_pct >  5.0
        price_rising = cmp_chg_pct >  0.5
        price_falling= cmp_chg_pct < -0.5

        if oi_rising and price_rising:
            signal = "LONG_BUILDUP"
            bias   = "BULLISH"
        elif oi_rising and price_falling:
            signal = "SHORT_BUILDUP"
            bias   = "BEARISH"
        elif oi_falling and price_rising:
            signal = "SHORT_COVERING"
            bias   = "BULLISH"
        elif oi_falling and price_falling:
            signal = "LONG_UNWINDING"
            bias   = "BEARISH"
        else:
            continue

        # ── Consecutive days at end ───────────────────────────────────────────
        consec_days = count_signal_days(oi_series, cmp_series, signal)

        # Apply min_consec filter
        if min_consec > 0 and consec_days < min_consec:
            continue

        # ── Consistency score ─────────────────────────────────────────────────
        match_days, total_intervals = count_consistent_days(
            oi_series, cmp_series, vol_series, signal
        )
        consistency_pct = round(match_days / total_intervals * 100) if total_intervals > 0 else 0

        # Consistency label
        if consistency_pct >= 70:
            consistency_label = "HIGH"
        elif consistency_pct >= 50:
            consistency_label = "MEDIUM"
        else:
            consistency_label = "LOW"

        # ── OI Acceleration ───────────────────────────────────────────────────
        accelerating, first_half_chg, second_half_chg = check_acceleration(
            oi_series, date_labels
        )

        # ── Triple confirmation ───────────────────────────────────────────────
        vol_consec = 0
        for i in range(len(vol_series) - 1, 0, -1):
            if vol_series[i] > vol_series[i-1]:
                vol_consec += 1
            else:
                break

        triple_confirm = (
            signal == "LONG_BUILDUP" and
            vol_rising and
            vol_consec >= 2
        )

        results.append({
            "symbol":             sym,
            "is_index":           sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "signal":             signal,
            "bias":               bias,

            # Consistency
            "consistency_pct":   consistency_pct,
            "consistency_label": consistency_label,
            "match_days":        match_days,
            "total_days":        total_intervals,

            # Consecutive (recent streak)
            "consec_days":       consec_days,

            # Acceleration
            "accelerating":      accelerating,
            "oi_first_half_chg": first_half_chg,
            "oi_second_half_chg":second_half_chg,

            # Triple
            "triple_confirm":    triple_confirm,
            "vol_consec":        vol_consec,

            # Overall % changes
            "oi_chg_pct":        oi_chg_pct,
            "vol_chg_pct":       vol_chg_pct,
            "cmp_chg_pct":       cmp_chg_pct,

            # Sparklines
            "oi_series":         [round(x / 100000, 1) for x in oi_series],
            "vol_series":        [round(x / 100000, 1) for x in vol_series],
            "cmp_series":        cmp_series,
            "date_labels":       date_labels,

            # Latest
            "cmp":               cmp_series[-1],
            "series_days":       len(oi_series) - 1,
        })

    # Sort: triple+accelerating first, then consistency, then |OI%|
    results.sort(key=lambda x: (
        0 if (x["triple_confirm"] and x["accelerating"]) else
        1 if x["triple_confirm"] else
        2 if x["accelerating"] else 3,
        {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["consistency_label"]],
        -abs(x["oi_chg_pct"])
    ))

    summary = {
        "long_buildup":    sum(1 for r in results if r["signal"] == "LONG_BUILDUP"),
        "short_buildup":   sum(1 for r in results if r["signal"] == "SHORT_BUILDUP"),
        "short_covering":  sum(1 for r in results if r["signal"] == "SHORT_COVERING"),
        "long_unwinding":  sum(1 for r in results if r["signal"] == "LONG_UNWINDING"),
        "high_consistency":sum(1 for r in results if r["consistency_label"] == "HIGH"),
        "triple_confirm":  sum(1 for r in results if r["triple_confirm"]),
        "accelerating":    sum(1 for r in results if r["accelerating"]),
    }

    return {
        "expiry":          current_expiry,
        "series_start":    series_start,
        "total_trading_days": total_trading_days,
        "min_consec":      min_consec,
        "total":           len(results),
        "summary":         summary,
        "results":         results,
    }
