"""On-demand price chart data for any NSE symbol (stock or index).

Deliberately NOT a backfill-everything design like vix_daily_history — with
120+ tracked symbols, storing candles for all of them continuously would be
wasted Supabase storage and Kite API load for symbols nobody's looking at.
Instead: fetch straight from Kite when a user actually opens a symbol's
chart, with a short in-memory cache so repeat opens/timeframe-switches for
the same symbol don't re-hit Kite every time.

Timeframes (Oct 14 2026): added real candle-interval selection -- 5m / 15m /
30m / 60m / Daily / Weekly / Monthly -- alongside the existing 1D (today's
session, 1-minute candles) view, so the SMC overlays (Order Blocks, FVG,
BOS/CHoCH, liquidity sweeps) are usable on whatever timeframe a trader
actually works on, not just the daily chart. "week" and "month" aren't real
Kite intervals -- Kite's historical API tops out at "day" -- so those are
resampled here from daily candles fetched over a longer window.
"""
import time as time_module

INDEX_NSE_MAP = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE", "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX"}

# (symbol, interval) -> (fetched_at_epoch, candles)
_chart_cache: dict = {}
CACHE_TTL = {
    "minute": 60, "5minute": 60, "15minute": 90, "30minute": 120, "60minute": 180,
    "day": 300, "week": 600, "month": 1800,
}

# Kite's historical-data API caps how far back a single request can reach,
# and the cap shrinks as the candle interval gets finer (more candles per
# day = more rows per request). Values per Kite Connect's documented limits.
INTERVAL_DAY_CAP = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}

# How far back each timeframe fetches by default -- generous enough for
# real pan/zoom use, comfortably under the Kite cap above so requests stay
# fast (nobody needs 400 days of 60-minute candles loaded up front; they can
# zoom out from "Reset" if they want more). "week"/"month" fetch this many
# days of the underlying daily candles before resampling.
# BUG FIX (Oct 5 2026, still applies): "1d"/"minute" used a literal 1-day
# lookback originally, which landed in a dead zone right after a weekend/
# holiday. 5 days always reaches back across the longest realistic NSE gap.
TIMEFRAME_LOOKBACK_DAYS = {
    "minute": 5,
    "5minute": 30,
    "15minute": 60,
    "30minute": 90,
    "60minute": 180,
    "day": 730,
    "week": 1095,
    "month": 1825,
}


def _resample_daily(candles, period: str):
    """Aggregate raw daily Kite candles (dicts with a datetime 'date') into
    weekly (period='week', ISO calendar week, Mon-Sun) or monthly
    (period='month', calendar month) OHLCV bars. Each bucket is labeled
    with its LAST trading day's date -- the convention most Indian broker
    platforms (Kite, Sensibull) use for weekly/monthly candles, rather than
    the bucket's start."""
    if not candles:
        return []
    buckets: dict = {}
    order: list = []
    for c in candles:
        d = c["date"]
        key = (d.isocalendar()[0], d.isocalendar()[1]) if period == "week" else (d.year, d.month)
        if key not in buckets:
            buckets[key] = {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c.get("volume", 0) or 0, "last_date": d}
            order.append(key)
        else:
            b = buckets[key]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c.get("volume", 0) or 0
            b["last_date"] = d
    return [
        {"date": buckets[k]["last_date"], "open": buckets[k]["open"], "high": buckets[k]["high"], "low": buckets[k]["low"], "close": buckets[k]["close"], "volume": buckets[k]["volume"]}
        for k in order
    ]


def get_chart_data(symbol: str, interval: str = "day", range: str = "6m"):
    from services.kite_auth import get_kite_client
    from datetime import datetime, timedelta

    symbol = symbol.upper()
    # `range` is accepted for backward compatibility with older callers but
    # no longer drives the lookback window -- TIMEFRAME_LOOKBACK_DAYS does,
    # keyed purely on the requested interval/timeframe.
    cache_key = (symbol, interval)
    now_epoch = time_module.time()
    cached = _chart_cache.get(cache_key)
    ttl = CACHE_TTL.get(interval, 120)
    if cached and (now_epoch - cached[0]) < ttl:
        return cached[1]

    kite = get_kite_client()
    nse_key = INDEX_NSE_MAP.get(symbol, f"NSE:{symbol}")

    try:
        quote = kite.quote([nse_key])[nse_key]
        token = quote["instrument_token"]
    except Exception as e:
        result = {"symbol": symbol, "candles": [], "error": f"Could not resolve instrument: {e}"}
        _chart_cache[cache_key] = (now_epoch, result)
        return result

    is_daily_like = interval in ("day", "week", "month")
    # "week"/"month" aren't real Kite intervals -- fetch daily candles and
    # resample client-side (well, server-side, but same idea) below.
    fetch_interval = "day" if interval in ("week", "month") else interval

    days = TIMEFRAME_LOOKBACK_DAYS.get(interval, 182)
    days = min(days, INTERVAL_DAY_CAP.get(fetch_interval, 2000))

    # BUG FIX (Oct 5 2026): datetime.now() is naive server time -- Railway
    # runs in UTC, but Kite's historical_data interprets the date strings we
    # send as IST (the exchange timezone). Sending unconverted UTC "now" as
    # "to_date" made Kite think the request ended ~5h30m earlier than it
    # actually did, so it silently returned only up to the last fully-
    # elapsed session and never today's candles, however long the market
    # had been open. Confirmed by cross-checking against the live
    # oi_snapshots capture pipeline, which had today's real-time price the
    # whole time -- this was entirely our own timezone bug, not Kite lag.
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    to_date = datetime.now(ist).replace(tzinfo=None)
    from_date = to_date - timedelta(days=days)

    try:
        candles = kite.historical_data(
            token,
            from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date.strftime("%Y-%m-%d %H:%M:%S"),
            fetch_interval,
        )
    except Exception as e:
        result = {"symbol": symbol, "candles": [], "error": str(e)}
        _chart_cache[cache_key] = (now_epoch, result)
        return result

    if interval in ("week", "month"):
        candles = _resample_daily(candles, interval)

    out = []
    for c in candles:
        ts = c["date"]
        # BUG FIX (Oct 5 2026): lightweight-charts' Time type only accepts a
        # plain "YYYY-MM-DD" string for daily/business-day candles -- for
        # any intraday interval it needs a numeric Unix timestamp (seconds).
        # Sending a full ISO datetime string for intraday candles silently
        # produced garbage bar widths/positions instead of a clean error.
        out.append({
            "time": ts.strftime("%Y-%m-%d") if is_daily_like else int(ts.timestamp()),
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c.get("volume", 0),
        })

    result = {"symbol": symbol, "interval": interval, "range": range, "candles": out}
    _chart_cache[cache_key] = (now_epoch, result)
    return result
