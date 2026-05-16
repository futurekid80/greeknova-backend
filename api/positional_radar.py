from utils.db import get_supabase
from datetime import datetime, timezone, timedelta


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

def fmtoi(n):
    if abs(n) >= 10000000: return f"{n/10000000:.2f}Cr"
    if abs(n) >= 100000:   return f"{n/100000:.1f}L"
    return str(n)


def get_positional_radar(days: int = 5):
    """
    For each symbol, compare OI and price over last N trading days.
    Returns trend direction (rising/falling) and % change for each.
    Signals: LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING, LONG_UNWINDING
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()

    # ── Step 1: Find last N+1 trading dates ──────────────────────────────────
    # We need N days of change so we need N+1 data points
    trading_dates = []
    for i in range(60):  # look back up to 60 calendar days
        d = (today - timedelta(days=i)).isoformat()
        check = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .limit(1).execute()
        if check.data:
            trading_dates.append(d)
        if len(trading_dates) >= days + 1:
            break

    if len(trading_dates) < 2:
        return {"error": "Not enough trading days data", "results": []}

    # Most recent first → reverse for chronological order
    trading_dates = list(reversed(trading_dates))
    # We use last N+1 dates: index 0 = oldest, index -1 = today
    analysis_dates = trading_dates[-(days + 1):]

    # ── Step 2: Get EOD OI for each date (one timestamp per day) ─────────────
    # {date -> {symbol -> total_oi}}
    oi_by_date: dict = {}

    for d in analysis_dates:
        # Get last timestamp of the day (EOD snapshot)
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

        # Fetch total OI per symbol at EOD
        oi_q = []
        for offset in range(0, 50000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("symbol, oi")\
                .eq("timestamp", eod_ts)\
                .range(offset, offset + 999).execute()
            if not batch.data:
                break
            oi_q.extend(batch.data)
            if len(batch.data) < 1000:
                break

        sym_oi: dict = {}
        for r in oi_q:
            sym = r["symbol"]
            sym_oi[sym] = sym_oi.get(sym, 0) + (r["oi"] or 0)

        oi_by_date[d] = sym_oi

    # ── Step 3: Get EOD CMP for each date ────────────────────────────────────
    # {date -> {symbol -> cmp}}
    cmp_by_date: dict = {}

    for d in analysis_dates:
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

    # ── Step 4: Build daily OI and price series per symbol ───────────────────
    results = []
    available_dates = [d for d in analysis_dates if d in oi_by_date]

    if len(available_dates) < 2:
        return {"error": "Insufficient data points", "results": []}

    for sym in SYMBOLS:
        # Build series
        oi_series  = []
        cmp_series = []
        date_labels = []

        for d in available_dates:
            oi_val  = oi_by_date.get(d, {}).get(sym, 0)
            cmp_val = cmp_by_date.get(d, {}).get(sym, 0)
            if oi_val > 0:
                oi_series.append(oi_val)
                cmp_series.append(cmp_val)
                date_labels.append(d)

        if len(oi_series) < 2:
            continue

        # ── OI trend ─────────────────────────────────────────────────────────
        oi_start  = oi_series[0]
        oi_end    = oi_series[-1]
        oi_chg_pct = round((oi_end - oi_start) / oi_start * 100, 2) if oi_start > 0 else 0

        # Count how many consecutive days OI was rising
        oi_rising_days = 0
        for i in range(len(oi_series) - 1, 0, -1):
            if oi_series[i] > oi_series[i-1]:
                oi_rising_days += 1
            else:
                break

        oi_falling_days = 0
        for i in range(len(oi_series) - 1, 0, -1):
            if oi_series[i] < oi_series[i-1]:
                oi_falling_days += 1
            else:
                break

        # ── Price trend ───────────────────────────────────────────────────────
        cmp_start = cmp_series[0]
        cmp_end   = cmp_series[-1]
        cmp_chg_pct = round((cmp_end - cmp_start) / cmp_start * 100, 2) if cmp_start > 0 else 0

        price_rising_days = 0
        for i in range(len(cmp_series) - 1, 0, -1):
            if cmp_series[i] > cmp_series[i-1]:
                price_rising_days += 1
            else:
                break

        price_falling_days = 0
        for i in range(len(cmp_series) - 1, 0, -1):
            if cmp_series[i] < cmp_series[i-1]:
                price_falling_days += 1
            else:
                break

        # ── Signal classification ─────────────────────────────────────────────
        oi_rising   = oi_chg_pct > 1.0
        oi_falling  = oi_chg_pct < -1.0
        price_rising  = cmp_chg_pct > 0.5
        price_falling = cmp_chg_pct < -0.5

        if oi_rising and price_rising:
            signal      = "LONG_BUILDUP"
            signal_desc = "OI + price both rising — long positions being built"
            bias        = "BULLISH"
            strength    = min(oi_rising_days, price_rising_days)
        elif oi_rising and price_falling:
            signal      = "SHORT_BUILDUP"
            signal_desc = "OI rising, price falling — short positions being built"
            bias        = "BEARISH"
            strength    = min(oi_rising_days, price_falling_days)
        elif oi_falling and price_rising:
            signal      = "SHORT_COVERING"
            signal_desc = "OI falling, price rising — shorts being covered"
            bias        = "BULLISH"
            strength    = min(oi_falling_days, price_rising_days)
        elif oi_falling and price_falling:
            signal      = "LONG_UNWINDING"
            signal_desc = "OI falling, price falling — longs being exited"
            bias        = "BEARISH"
            strength    = min(oi_falling_days, price_falling_days)
        else:
            signal      = "SIDEWAYS"
            signal_desc = "No clear directional OI trend"
            bias        = "NEUTRAL"
            strength    = 0

        if signal == "SIDEWAYS":
            continue  # Skip neutral — not actionable for positional

        # Consecutive days score — higher = stronger signal
        conviction = "HIGH" if strength >= days - 1 else "MEDIUM" if strength >= 2 else "LOW"

        results.append({
            "symbol":            sym,
            "is_index":          sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "signal":            signal,
            "signal_desc":       signal_desc,
            "bias":              bias,
            "conviction":        conviction,
            "strength_days":     strength,        # consecutive days signal held
            "oi_chg_pct":        oi_chg_pct,      # % OI change over period
            "price_chg_pct":     cmp_chg_pct,     # % price change over period
            "oi_rising_days":    oi_rising_days,
            "price_rising_days": price_rising_days,
            "oi_series":         [round(x/100000, 1) for x in oi_series],  # in Lakhs
            "cmp_series":        cmp_series,
            "date_labels":       date_labels,
            "cmp":               cmp_end,
            "oi_now":            oi_end,
            "oi_start":          oi_start,
            "days_analyzed":     len(oi_series),
        })

    # Sort: conviction HIGH first, then by absolute OI change
    conviction_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    results.sort(key=lambda x: (
        conviction_order[x["conviction"]],
        -abs(x["oi_chg_pct"])
    ))

    # Summary stats
    long_buildup   = sum(1 for r in results if r["signal"] == "LONG_BUILDUP")
    short_buildup  = sum(1 for r in results if r["signal"] == "SHORT_BUILDUP")
    short_covering = sum(1 for r in results if r["signal"] == "SHORT_COVERING")
    long_unwinding = sum(1 for r in results if r["signal"] == "LONG_UNWINDING")
    high_conv      = sum(1 for r in results if r["conviction"] == "HIGH")

    return {
        "days":           days,
        "dates_analyzed": available_dates,
        "total":          len(results),
        "summary": {
            "long_buildup":   long_buildup,
            "short_buildup":  short_buildup,
            "short_covering": short_covering,
            "long_unwinding": long_unwinding,
            "high_conviction": high_conv,
        },
        "results": results,
    }
