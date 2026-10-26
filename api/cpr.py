from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import time
import time as time_module

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
# BUG FIX (Aug 26 2026): STOCKS and STOCK_NSE_MAP were both hardcoded,
# independently-maintained copies that would silently drift out of sync
# every time a symbol got added elsewhere. Now derived from the single
# canonical list so they can't drift again.
from api.iv_analysis import SYMBOLS as _ALL_SYMBOLS
STOCKS = [s for s in _ALL_SYMBOLS if s not in INDICES]

INDEX_NSE_MAP = {
    "NIFTY":    "NSE:NIFTY 50",
    "BANKNIFTY":"NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY":"NSE:NIFTY MID SELECT",
    "SENSEX":   "BSE:SENSEX",
}
STOCK_NSE_MAP = {s: f"NSE:{s}" for s in STOCKS}
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


def _get_prev_trading_day(ref_date):
    """Get previous trading day (skip weekends AND NSE holidays).

    BUG FIX (Oct 5 2026): this used to only skip weekends (weekday>=5),
    not exchange holidays like Gandhi Jayanti (Oct 2 2026). When the EOD
    CPR job ran on/after a holiday, market_closed was still computed as
    True (weekday<5 check doesn't know about holidays), so prev_trading_day
    was set to the holiday itself -- a date with no Kite candle. The match
    loop below then fell through to its `candles[-2]` fallback, which
    assumes candles[-1] is "today's partial candle" to be excluded. That
    assumption was wrong here: candles[-1] was already the correct
    previous COMPLETE session (e.g. Thursday Oct 1), so skipping to
    candles[-2] picked a close from ONE TRADING DAY TOO FAR BACK (e.g.
    Wednesday Sep 30) for every single symbol -- producing a stale
    prev_close and therefore wrong day's % change across the board
    (reported by Manish: Kalyan Jewellers, Angel One, BSE all showing
    inflated moves the Monday after Gandhi Jayanti). Delegate to the
    shared, holiday-aware helper in utils/market_calendar.py instead of
    this file's own weekend-only copy -- the exact duplicated-date-logic
    anti-pattern that module's own today_ist() docstring warns about.
    """
    from utils.market_calendar import get_prev_trading_day
    return get_prev_trading_day(ref_date)


def _get_next_trading_day(ref_date):
    """Get next trading day (skip weekends AND NSE holidays).

    Was weekend-only, same as the old _get_prev_trading_day -- only
    "safe" because its one call site (below) separately re-checked
    is_trading_day() in a patch-up loop. Delegating to the shared helper
    removes that fragile duplication; the patch-up loop below is now
    redundant but harmless.
    """
    from utils.market_calendar import get_next_trading_day
    return get_next_trading_day(ref_date)


def compute_and_store_cpr(trade_date: str = None):
    """
    EOD CPR computation — uses kite.historical_data() for official NSE settlement close.
    CRITICAL: Always uses YESTERDAY's completed candle as OHLC source.
    Never uses today's partial candle.
    Stores CPR for TOMORROW's trade_date (next trading day).
    """
    supabase = get_supabase()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        from utils.market_calendar import is_trading_day
        next_day = _get_next_trading_day(today)
        # Skip holidays
        while not is_trading_day(next_day):
            next_day = _get_next_trading_day(next_day)
        trade_date = next_day.isoformat()

    print(f"[CPR] Computing for trade_date: {trade_date}")

    # Previous trading day = the OHLC source.
    # BUG FIX (Oct 5 2026): prev_trading_day used to be derived from the
    # REAL wall-clock "now" (today/market_closed), not from `trade_date` --
    # correct only for the implicit nightly-run case (trade_date defaults to
    # tomorrow, so "today" genuinely is the day before it). Any EXPLICIT
    # trade_date call (e.g. a manual recompute for *today's own* date, which
    # is exactly how this gets re-triggered to fix a stale row) broke this:
    # called after 15:40 IST with trade_date=today, market_closed was True,
    # so prev_trading_day was set to `today` itself -- storing TODAY's own
    # close as "previous close" for today's own row. Confirmed against
    # Google Finance: ANGELONE's real previous close (Oct 1) was 276.80, but
    # this bug stored 283.75 -- ANGELONE's own Oct 5 close -- as prev_close,
    # making every later % change computed off it near-zero instead of the
    # real +2.51%. Always derive prev_trading_day from trade_date itself, so
    # behavior no longer depends on when the function happens to be called.
    target_date = date_type.fromisoformat(trade_date)
    prev_trading_day = _get_prev_trading_day(target_date)
    prev_str = prev_trading_day.isoformat()
    print(f"[CPR] Using OHLC from: {prev_str} (for trade_date={trade_date})")

    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

    # BUG FIX (Oct 23 2026): this used to pull previous-day OHLC via
    # kite.historical_data(), which requires Zerodha's separate paid
    # Historical Data add-on. Manish's Kite Connect app is a personal/
    # multiuser API, not the public Connect API, so that call has never
    # had real access -- it worked by coincidence while a cached/trial
    # grant lasted, then started failing every symbol with "Insufficient
    # permission for that call" from 2026-10-22 onward. The job swallowed
    # the per-symbol errors and silently stored ZERO rows for two days
    # straight, so the CPR Scanner kept serving 2026-10-21's stale numbers
    # under every later trade_date with no indication anything was wrong
    # (e.g. OIL kept showing as "Extremely Narrow" days after that was
    # true). Fixed by deriving prev-day high/low/close ourselves from
    # cmp_prices, which is populated independently every ~5 min during
    # market hours via kite.quote() (a call this API key DOES have access
    # to) -- no Historical Data permission needed at all.
    day_start = f"{prev_str}T00:00:00+00:00"
    day_end   = (prev_trading_day + timedelta(days=1)).isoformat() + "T00:00:00+00:00"

    for sym in all_symbols:
        try:
            rows = supabase.from_("cmp_prices")\
                .select("cmp, timestamp")\
                .eq("symbol", sym)\
                .gte("timestamp", day_start)\
                .lt("timestamp", day_end)\
                .order("timestamp")\
                .limit(2000)\
                .execute()
            cmps = [float(r["cmp"]) for r in (rows.data or []) if r.get("cmp")]
            if cmps:
                ohlc_map[sym] = {
                    "high":  max(cmps),
                    "low":   min(cmps),
                    "close": cmps[-1],  # last tick of the day (rows ordered ascending)
                }
        except Exception as e:
            print(f"[CPR] cmp_prices fetch {sym}: {e}")

    print(f"[CPR] Got OHLC for {len(ohlc_map)} symbols (source: cmp_prices for {prev_str})")

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
            "trade_date":        trade_date,
            "symbol":            sym,
            "is_index":          sym in INDICES,
            "prev_high":         high,
            "prev_low":          low,
            "prev_close":        close,
            "pivot":             cpr["pivot"],
            "tc":                cpr["tc"],
            "bc":                cpr["bc"],
            "width_pts":         cpr["width_pts"],
            "width_pct":         cpr["width_pct"],
            "width_label":       label["label"],
            "width_color":       label["color"],
            "width_emoji":       label["emoji"],
            "width_priority":    label["priority"],
            "prev_tc":           prev.get("tc"),
            "prev_bc":           prev.get("bc"),
            "cpr_trend":         trend,
            "is_virgin":         True,
            "cpr_status":        None,
            "last_cmp":          None,
            "status_updated_at": None,
        })

    if records:
        for i in range(0, len(records), 50):
            try:
                supabase.table("cpr_levels")\
                    .upsert(records[i:i+50], on_conflict="trade_date,symbol")\
                    .execute()
            except Exception as e:
                print(f"[CPR] Upsert batch {i} failed: {e}")

    global _cpr_cache, _cpr_cache_time
    _cpr_cache = {}
    _cpr_cache_time = 0

    print(f"[CPR] Stored {len(records)} CPR records for {trade_date}")
    return {"stored": len(records), "trade_date": trade_date}


def compute_and_store_weekly_monthly_cpr(trade_date: str = None):
    """
    Computes weekly and monthly CPR levels.
    Weekly: uses previous week's H/L/C (Mon-Fri)
    Monthly: uses previous month's H/L/C
    Stores in cpr_levels_weekly table.
    """
    supabase = get_supabase()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        from utils.market_calendar import is_trading_day
        next_day = _get_next_trading_day(today)
        while not is_trading_day(next_day):
            next_day = _get_next_trading_day(next_day)
        trade_date = next_day.isoformat()

    all_symbols = INDICES + STOCKS

    # FIX (Oct 26 2026): week/month boundaries must anchor on `trade_date`
    # (the date this CPR row is FOR), not on `today` (the date the job
    # happens to run). They're the same on an ordinary Mon-Thu run, since
    # "today" and "tomorrow" fall in the same week either way -- but the
    # Friday-evening run that produces the FOLLOWING Monday's row computes
    # for a date in the NEW week while `today` is still in the OLD one,
    # so anchoring on `today` subtracts an extra 7 days it shouldn't and
    # lands two weeks back instead of one. Caught via Manish noticing
    # BHARATFORG's weekly CPR (badged "Extremely Narrow") didn't match a
    # visibly volatile week on the chart -- it had quietly used Oct 12-16
    # instead of Oct 19-23. Same root cause would hit monthly CPR once a
    # month at the month-end rollover.
    target_date = date_type.fromisoformat(trade_date)

    # Weekly CPR always uses the PREVIOUS completed week (Mon-Fri)
    # GoCharting convention: prev week H/L/C → current/next week's CPR
    this_week_monday = target_date - timedelta(days=target_date.weekday())  # Mon of trade_date's week
    prev_week_monday = this_week_monday - timedelta(days=7)
    prev_week_friday = this_week_monday - timedelta(days=3)

    week_start = prev_week_monday
    week_end   = prev_week_friday

    first_of_month = target_date.replace(day=1)
    prev_month_end = first_of_month - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)

    print(f"[CPR Weekly] Week range: {week_start} to {week_end}")
    print(f"[CPR Weekly] Month range: {prev_month_start} to {prev_month_end}")

    weekly_ohlc  = {}
    monthly_ohlc = {}

    # BUG FIX (Oct 23 2026): same root cause as compute_and_store_cpr() --
    # kite.historical_data() has no real permission on this API key, so
    # this derives weekly/monthly OHLC from cmp_prices ticks instead.
    week_start_ts  = f"{week_start.isoformat()}T00:00:00+00:00"
    week_end_ts    = (week_end + timedelta(days=1)).isoformat() + "T00:00:00+00:00"
    month_start_ts = f"{prev_month_start.isoformat()}T00:00:00+00:00"
    month_end_ts   = (prev_month_end + timedelta(days=1)).isoformat() + "T00:00:00+00:00"

    for sym in all_symbols:
        try:
            week_rows = supabase.from_("cmp_prices")\
                .select("cmp, timestamp")\
                .eq("symbol", sym)\
                .gte("timestamp", week_start_ts)\
                .lt("timestamp", week_end_ts)\
                .order("timestamp")\
                .limit(5000)\
                .execute()
            week_cmps = [float(r["cmp"]) for r in (week_rows.data or []) if r.get("cmp")]
            if week_cmps:
                weekly_ohlc[sym] = {
                    "high":  max(week_cmps),
                    "low":   min(week_cmps),
                    "close": week_cmps[-1],
                }

            month_rows = supabase.from_("cmp_prices")\
                .select("cmp, timestamp")\
                .eq("symbol", sym)\
                .gte("timestamp", month_start_ts)\
                .lt("timestamp", month_end_ts)\
                .order("timestamp")\
                .limit(20000)\
                .execute()
            month_cmps = [float(r["cmp"]) for r in (month_rows.data or []) if r.get("cmp")]
            if month_cmps:
                monthly_ohlc[sym] = {
                    "high":  max(month_cmps),
                    "low":   min(month_cmps),
                    "close": month_cmps[-1],
                }
        except Exception as e:
            print(f"[CPR Weekly] {sym}: {e}")

    print(f"[CPR Weekly] Got weekly OHLC: {len(weekly_ohlc)}, monthly: {len(monthly_ohlc)} (source: cmp_prices)")

    prev_weekly_map = {}
    prev_monthly_map = {}
    try:
        prev_rows = supabase.from_("cpr_levels_weekly")\
            .select("symbol, tc, bc, timeframe")\
            .lt("trade_date", trade_date)\
            .order("trade_date", desc=True)\
            .limit(len(all_symbols) * 4)\
            .execute()
        seen_w = set()
        seen_m = set()
        for r in (prev_rows.data or []):
            sym = r["symbol"]
            tf  = r.get("timeframe")
            if tf == "weekly" and sym not in seen_w:
                prev_weekly_map[sym] = {"tc": float(r["tc"]), "bc": float(r["bc"])}
                seen_w.add(sym)
            elif tf == "monthly" and sym not in seen_m:
                prev_monthly_map[sym] = {"tc": float(r["tc"]), "bc": float(r["bc"])}
                seen_m.add(sym)
    except Exception as e:
        print(f"[CPR Weekly] Prev fetch: {e}")

    records = []

    for sym in all_symbols:
        ohlc = weekly_ohlc.get(sym)
        if ohlc:
            cpr   = compute_cpr(ohlc["high"], ohlc["low"], ohlc["close"])
            label = get_cpr_label(cpr["width_pct"])
            prev  = prev_weekly_map.get(sym, {})
            trend = get_cpr_trend(cpr["tc"], cpr["bc"], prev.get("tc"), prev.get("bc"))
            records.append({
                "trade_date":     trade_date,
                "symbol":         sym,
                "timeframe":      "weekly",
                "is_index":       sym in INDICES,
                "prev_high":      ohlc["high"],
                "prev_low":       ohlc["low"],
                "prev_close":     ohlc["close"],
                "pivot":          cpr["pivot"],
                "tc":             cpr["tc"],
                "bc":             cpr["bc"],
                "width_pts":      cpr["width_pts"],
                "width_pct":      cpr["width_pct"],
                "width_label":    label["label"],
                "width_color":    label["color"],
                "width_emoji":    label["emoji"],
                "width_priority": label["priority"],
                "cpr_trend":      trend,
                "is_virgin":      True,
                "last_cmp":       None,
            })

        ohlc = monthly_ohlc.get(sym)
        if ohlc:
            cpr   = compute_cpr(ohlc["high"], ohlc["low"], ohlc["close"])
            label = get_cpr_label(cpr["width_pct"])
            prev  = prev_monthly_map.get(sym, {})
            trend = get_cpr_trend(cpr["tc"], cpr["bc"], prev.get("tc"), prev.get("bc"))
            records.append({
                "trade_date":     trade_date,
                "symbol":         sym,
                "timeframe":      "monthly",
                "is_index":       sym in INDICES,
                "prev_high":      ohlc["high"],
                "prev_low":       ohlc["low"],
                "prev_close":     ohlc["close"],
                "pivot":          cpr["pivot"],
                "tc":             cpr["tc"],
                "bc":             cpr["bc"],
                "width_pts":      cpr["width_pts"],
                "width_pct":      cpr["width_pct"],
                "width_label":    label["label"],
                "width_color":    label["color"],
                "width_emoji":    label["emoji"],
                "width_priority": label["priority"],
                "cpr_trend":      trend,
                "is_virgin":      True,
                "last_cmp":       None,
            })

    if records:
        for i in range(0, len(records), 50):
            supabase.table("cpr_levels_weekly")\
                .upsert(records[i:i+50], on_conflict="trade_date,symbol,timeframe")\
                .execute()

    weekly_count  = sum(1 for r in records if r["timeframe"] == "weekly")
    monthly_count = sum(1 for r in records if r["timeframe"] == "monthly")
    print(f"[CPR Weekly] Stored {weekly_count} weekly + {monthly_count} monthly for {trade_date}")
    return {
        "stored_weekly":  weekly_count,
        "stored_monthly": monthly_count,
        "trade_date":     trade_date
    }


def get_cpr_scanner_timeframe(timeframe: str = "daily"):
    """Get CPR scanner for weekly or monthly timeframe."""
    if timeframe == "daily":
        return get_cpr_scanner()

    supabase = get_supabase()
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)

    # FIX (Oct 26 2026): all three branches here used to compute
    # "next trading day" -- a copy-paste of get_cpr_scanner()'s daily
    # logic that dropped its market-hours case. During live market hours
    # this asked for TOMORROW's weekly/monthly CPR row (not computed yet
    # until tonight's EOD job), so the page showed "No data" even though
    # today's already-computed weekly CPR was sitting right there valid
    # all week. Weekly/monthly CPR storage uses the same "stored under
    # next trading day" convention as daily (see compute_and_store_
    # weekly_monthly_cpr), so the correct read during market hours is
    # TODAY's date, exactly like get_cpr_scanner() does.
    if now_ist.weekday() >= 5:
        query_date = _get_next_trading_day(now_ist.date()).isoformat()
    elif now_ist.hour > 16 or (now_ist.hour == 16 and now_ist.minute >= 30):
        query_date = _get_next_trading_day(now_ist.date()).isoformat()
    else:
        query_date = now_ist.date().isoformat()

    rows = supabase.from_("cpr_levels_weekly")\
        .select("*")\
        .eq("trade_date", query_date)\
        .eq("timeframe", timeframe)\
        .limit(500).execute()

    if not rows.data:
        return {"data": [], "total": 0, "trade_date": query_date,
                "timeframe": timeframe, "message": f"No {timeframe} CPR computed yet"}

    today_str = now_ist.date().isoformat()
    cmp_rows = supabase.from_("cmp_prices")\
        .select("symbol,cmp")\
        .gte("timestamp", f"{today_str}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(500).execute()
    cmp_map: dict = {}
    seen = set()
    for r in (cmp_rows.data or []):
        if r["symbol"] not in seen:
            cmp_map[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

    results = []
    for row in rows.data:
        sym = row["symbol"]
        tc  = float(row["tc"])
        bc  = float(row["bc"])
        cmp = cmp_map.get(sym, float(row.get("last_cmp") or row["prev_close"]))
        position = get_cpr_position(cmp, tc, bc)
        trend_labels = {
            "ASCENDING":  {"label": "↑ Ascending", "color": "EMERALD"},
            "DESCENDING": {"label": "↓ Descending","color": "RED"},
            "SIDEWAYS":   {"label": "→ Sideways",  "color": "GRAY"},
            "UNKNOWN":    {"label": "— Unknown",   "color": "GRAY"},
        }
        trend_info = trend_labels.get(row.get("cpr_trend", "UNKNOWN"), trend_labels["UNKNOWN"])
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
            "cpr_position":   position["position"],
            "position_label": position["label"],
            "position_bias":  position["bias"],
            "position_color": position["color"],
            "timeframe":      timeframe,
        })

    results.sort(key=lambda x: (x["width_priority"], x["width_pct"]))

    return {
        "data":         results,
        "total":        len(results),
        "trade_date":   query_date,
        "timeframe":    timeframe,
        "narrow_count": sum(1 for r in results if r["width_priority"] <= 2),
    }


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
    """
    CPR Scanner — always reads from cpr_levels table.
    NEVER uses kite.ohlc() during market hours (that gives today's intraday OHLC).
    
    Query logic:
    - compute_and_store_cpr() stores CPR for NEXT trading day
    - So during market hours query NEXT trading day's CPR
    - Fallback: previous trading day if next not found yet
    - Last resort: _get_cpr_live() only if table is completely empty
    """
    global _cpr_cache, _cpr_cache_time

    cache_ttl = 30 if _is_market_hours() else 60
    if _cpr_cache and (time_module.time() - _cpr_cache_time) < cache_ttl:
        return _cpr_cache

    supabase = get_supabase()
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)

    # CPR is stored for the trade_date we're trading on.
    # During market hours (before 4:30 PM) → query today's date
    # Post market (after 4:30 PM) → query next trading day (tomorrow's CPR just computed)
    # Weekend → query next trading day
    if now_ist.weekday() >= 5:
        query_date = _get_next_trading_day(now_ist.date()).isoformat()
    elif now_ist.hour > 16 or (now_ist.hour == 16 and now_ist.minute >= 30):
        query_date = _get_next_trading_day(now_ist.date()).isoformat()
    else:
        query_date = now_ist.date().isoformat()

    # BUG FIX (Sep 12 2026): intermittent httpx.RemoteProtocolError
    # (ConnectionTerminated) -- a transient Supabase/PostgREST connection
    # reset, unrelated to this table's size. One retry clears it since the
    # reset doesn't repeat back to back.
    def _q_cpr():
        return supabase.from_("cpr_levels")\
            .select("*")\
            .eq("trade_date", query_date)\
            .limit(500)\
            .execute()
    try:
        cpr_rows = _q_cpr()
    except Exception as e:
        print(f"[CPR] cpr_rows query failed, retrying once: {e}")
        cpr_rows = _q_cpr()

    # FIX (Oct 23 2026): `query_date` is only the INTENDED date -- when the
    # compute job silently fails (as it did Oct 22-23 for the historical_data
    # permission bug) and this falls back to older rows, the response used to
    # still label itself with `query_date`, so the page kept showing "Trade
    # date: today" over data that was actually 2 days stale, with nothing to
    # flag it. `actual_date` now tracks where the served rows really came
    # from, and the response carries `is_stale` so the frontend can warn.
    actual_date = query_date
    if not cpr_rows.data:
        # Query not found — get last available date from DB
        last_res = supabase.from_("cpr_levels")\
            .select("trade_date")\
            .order("trade_date", desc=True)\
            .limit(1).execute()
        fallback_date = last_res.data[0]["trade_date"] if last_res.data else None
        print(f"[CPR] No data for {query_date}, falling back to {fallback_date}")
        if fallback_date:
            actual_date = fallback_date
            cpr_rows = supabase.from_("cpr_levels")\
                .select("*")\
                .eq("trade_date", fallback_date)\
                .limit(500)\
                .execute()
        if not cpr_rows.data:
            # Absolute last resort — should never happen in normal operation
            print(f"[CPR] No table data at all — falling back to live OHLC")
            return _get_cpr_live()

    # Get latest CMP from cmp_prices
    today_str = now_ist.date().isoformat()
    cmp_rows = supabase.from_("cmp_prices")\
        .select("symbol, cmp")\
        .gte("timestamp", f"{today_str}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(500)\
        .execute()
    cmp_map: dict = {}
    seen_cmp = set()
    for r in (cmp_rows.data or []):
        if r["symbol"] not in seen_cmp:
            cmp_map[r["symbol"]] = float(r["cmp"])
            seen_cmp.add(r["symbol"])

    active_signals = _get_uoa_signals_cached()

    results = []
    for row in cpr_rows.data:
        sym      = row["symbol"]
        tc       = float(row["tc"])
        bc       = float(row["bc"])
        # Use live CMP if available, else last_cmp from table, else prev_close
        cmp = cmp_map.get(sym) or (float(row["last_cmp"]) if row.get("last_cmp") else float(row["prev_close"]))
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
        "trade_date":       actual_date,
        "expected_date":    query_date,
        "is_stale":         actual_date != query_date,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "table",
    }

    _cpr_cache = result
    _cpr_cache_time = time_module.time()
    return result


def _get_cpr_live():
    """
    LAST RESORT ONLY — only called when cpr_levels table has no data at all.
    Uses kite.ohlc() which returns today's intraday OHLC — NOT ideal for CPR.
    Should never be called during normal operation.
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    print(f"[CPR] WARNING: Using live OHLC fallback — table data missing!")

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
        batch = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(500)\
            .execute()
        cmp_rows = batch.data or []
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
            "HOLDING_ABOVE": 3, "HOLDING_BELOW": 3,
            "BROKEN_UP": 2, "BROKEN_DOWN": 2, "INSIDE_CPR": 0,
        }.get(live_status, 1)
        sym_signals   = active_signals.get(sym, [])
        has_oi_signal = len(sym_signals) > 0
        best_signal   = _get_nearest_signal(sym_signals, cmp)
        best_is_near_atm = best_signal is not None and (best_signal.get("strikes_from_atm", 99) <= 2.0)
        confluence    = label["priority"] <= 2 and has_oi_signal and holding_score >= 2 and best_is_near_atm
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
        "source":           "live_fallback",
    }


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


def get_cpr_levels_for_symbol(symbol: str):
    """Lightweight per-symbol CPR fetch for chart overlays — same
    query-date logic as get_cpr_scanner() (today during market hours,
    next trading day after 4:30pm/weekend) but a single-row lookup
    instead of pulling all ~120 symbols."""
    symbol = symbol.upper()
    supabase = get_supabase()
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)

    if now_ist.weekday() >= 5:
        query_date = _get_next_trading_day(now_ist.date()).isoformat()
    elif now_ist.hour > 16 or (now_ist.hour == 16 and now_ist.minute >= 30):
        query_date = _get_next_trading_day(now_ist.date()).isoformat()
    else:
        query_date = now_ist.date().isoformat()

    row = (
        supabase.from_("cpr_levels")
        .select("pivot, tc, bc, trade_date")
        .eq("symbol", symbol)
        .eq("trade_date", query_date)
        .limit(1)
        .execute()
    )
    if not row.data:
        # fall back to the most recent available row for this symbol
        row = (
            supabase.from_("cpr_levels")
            .select("pivot, tc, bc, trade_date")
            .eq("symbol", symbol)
            .order("trade_date", desc=True)
            .limit(1)
            .execute()
        )
    if not row.data:
        return {"symbol": symbol, "cpr": None}
    r = row.data[0]
    return {"symbol": symbol, "cpr": {"pivot": r["pivot"], "tc": r["tc"], "bc": r["bc"], "trade_date": r["trade_date"]}}
