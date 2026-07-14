"""
adx_analysis.py
ADX(14) — Average Directional Index, Wilder's original smoothing method.
Measures trend STRENGTH (not direction) — a high ADX means a real trending
move is underway (up or down), a low ADX means the stock is choppy/range-bound,
regardless of what any OI or volume signal says. Useful as a confirmation
filter: a "Long Buildup" signal on a stock with rising ADX is a genuinely
different, stronger setup than the same signal on a choppy stock.

Data source: cpr_levels.prev_high / prev_low / prev_close, which already
store each day's OHLC per symbol (used for existing CPR pivot calculations)
— no new data collection needed.

Needs ~28 days of history minimum for a stable reading (14 to seed the
initial average, another 14 for Wilder's smoothing to stabilize). Newer
symbols with less history return None rather than an unreliable number.
"""
from datetime import datetime, timedelta

PERIOD = 14
MIN_DAYS = PERIOD * 2  # 28 — minimum for a stable ADX reading


def _wilder_smooth(values: list) -> list:
    """Wilder's smoothing: first value = simple sum, then each next = prev - prev/period + current."""
    if len(values) < PERIOD:
        return []
    smoothed = [sum(values[:PERIOD])]
    for v in values[PERIOD:]:
        smoothed.append(smoothed[-1] - (smoothed[-1] / PERIOD) + v)
    return smoothed


def _compute_adx_for_symbol(rows: list) -> dict | None:
    """rows: list of {trade_date, high, low, close} sorted oldest -> newest."""
    if len(rows) < MIN_DAYS + 1:
        return None

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(rows)):
        prev = rows[i - 1]
        cur = rows[i]
        high, low, prev_close = cur["high"], cur["low"], prev["close"]
        if high is None or low is None or prev_close is None:
            return None

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up_move = high - prev["high"] if prev["high"] is not None else 0
        down_move = prev["low"] - low if prev["low"] is not None else 0

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0

        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    smoothed_tr = _wilder_smooth(trs)
    smoothed_plus_dm = _wilder_smooth(plus_dms)
    smoothed_minus_dm = _wilder_smooth(minus_dms)

    if not smoothed_tr or len(smoothed_tr) < PERIOD:
        return None

    dx_values = []
    for i in range(len(smoothed_tr)):
        str_v = smoothed_tr[i]
        if str_v == 0:
            continue
        plus_di = 100 * smoothed_plus_dm[i] / str_v
        minus_di = 100 * smoothed_minus_dm[i] / str_v
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_values.append(dx)

    if len(dx_values) < PERIOD:
        return None

    adx_series = _wilder_smooth_simple_avg(dx_values)
    if not adx_series:
        return None

    latest_adx = round(adx_series[-1], 1)
    return {
        "adx": latest_adx,
        "trending": latest_adx >= 25,
        "watch": 20 <= latest_adx < 25,
        "history_days": len(rows),
    }


def _wilder_smooth_simple_avg(dx_values: list) -> list:
    """ADX itself: first value = simple average of first 14 DX, then Wilder-smoothed."""
    if len(dx_values) < PERIOD:
        return []
    adx = [sum(dx_values[:PERIOD]) / PERIOD]
    for dx in dx_values[PERIOD:]:
        adx.append((adx[-1] * (PERIOD - 1) + dx) / PERIOD)
    return adx


def get_adx_map(supabase, symbols: list = None) -> dict:
    """
    Returns {symbol: {"adx": float, "trending": bool, "watch": bool, "history_days": int}}
    for every symbol with enough history. Symbols with insufficient history
    are simply omitted (caller should treat missing = "building history").
    """
    today = datetime.now().date()
    lookback_start = (today - timedelta(days=int(MIN_DAYS * 1.6) + 10)).isoformat()

    try:
        q = supabase.from_("cpr_levels") \
            .select("symbol, trade_date, prev_high, prev_low, prev_close") \
            .gte("trade_date", lookback_start) \
            .order("trade_date", desc=False)
        if symbols:
            q = q.in_("symbol", symbols)
        res = q.limit(20000).execute()
    except Exception as e:
        print(f"[ADX] Fetch failed: {e}")
        return {}

    by_symbol: dict = {}
    for r in (res.data or []):
        by_symbol.setdefault(r["symbol"], []).append({
            "trade_date": r["trade_date"],
            "high": float(r["prev_high"]) if r.get("prev_high") is not None else None,
            "low": float(r["prev_low"]) if r.get("prev_low") is not None else None,
            "close": float(r["prev_close"]) if r.get("prev_close") is not None else None,
        })

    result = {}
    for sym, rows in by_symbol.items():
        rows_sorted = sorted(rows, key=lambda r: r["trade_date"])
        adx_data = _compute_adx_for_symbol(rows_sorted)
        if adx_data:
            result[sym] = adx_data

    return result


def get_hourly_adx_map(supabase, symbols: list = None, lookback_days: int = 15) -> dict:
    """
    Same ADX(14) math, but on HOURLY bars built from cmp_prices' 5-min ticks
    instead of daily bars — no new data collection needed, just a different
    aggregation of data we already capture. Needs ~28 hourly bars (roughly
    5 trading days, since each day has ~6 hourly buckets during market hours)
    for a stable reading.
    """
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()
    lookback_start = (today - timedelta(days=lookback_days)).isoformat()

    try:
        rows_res = []
        q_base = supabase.from_("cmp_prices").select("symbol, timestamp, cmp").gte("timestamp", f"{lookback_start}T00:00:00+00:00").order("timestamp", desc=False)
        if symbols:
            q_base = q_base.in_("symbol", symbols)
        for offset in range(0, 50000, 1000):
            batch = q_base.range(offset, offset + 999).execute()
            if not batch.data:
                break
            rows_res.extend(batch.data)
            if len(batch.data) < 1000:
                break
    except Exception as e:
        print(f"[ADX Hourly] Fetch failed: {e}")
        return {}

    # Bucket into hourly bars per symbol, in IST
    buckets: dict = {}  # (symbol, hour_bucket_iso) -> list of prices in order
    for r in rows_res:
        sym = r["symbol"]
        cmp_val = r.get("cmp")
        if cmp_val is None:
            continue
        ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).astimezone(ist)
        hour_bucket = ts.replace(minute=0, second=0, microsecond=0)
        key = (sym, hour_bucket.isoformat())
        buckets.setdefault(key, []).append(float(cmp_val))

    # Build per-symbol hourly OHLC bar series
    sym_bars: dict = {}
    for (sym, bucket_iso), prices in buckets.items():
        sym_bars.setdefault(sym, []).append({
            "trade_date": bucket_iso,  # reused as the sort/date key by _compute_adx_for_symbol
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
        })

    result = {}
    for sym, bars in sym_bars.items():
        bars_sorted = sorted(bars, key=lambda b: b["trade_date"])
        adx_data = _compute_adx_for_symbol(bars_sorted)
        if adx_data:
            result[sym] = adx_data

    return result
