"""On-demand price chart data for any NSE symbol (stock or index).

Deliberately NOT a backfill-everything design like vix_daily_history — with
120+ tracked symbols, storing candles for all of them continuously would be
wasted Supabase storage and Kite API load for symbols nobody's looking at.
Instead: fetch straight from Kite when a user actually opens a symbol's
chart, with a short in-memory cache so repeat opens/timeframe-switches for
the same symbol don't re-hit Kite every time.
"""
import time as time_module

INDEX_NSE_MAP = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE", "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX"}

# (symbol, interval) -> (fetched_at_epoch, candles)
_chart_cache: dict = {}
CACHE_TTL = {"day": 300, "minute": 60, "5minute": 60, "15minute": 90}

# BUG FIX (Oct 5 2026): "1d" used a literal 1-calendar-day lookback, so
# right after a weekend/holiday (e.g. Fri close -> Sat/Sun/Mon) the window
# fell entirely in a dead zone with zero market data -- "No chart data
# available" even though the chart itself was fine. 5 calendar days always
# reaches back across the longest realistic NSE gap (a 3-day weekend plus
# one more holiday) to the last real trading session, while still being
# well under Kite's 60-day cap on minute-interval requests.
RANGE_TO_DAYS = {"1d": 5, "1m": 30, "3m": 90, "6m": 182, "1y": 365, "3y": 1095}


def get_chart_data(symbol: str, interval: str = "day", range: str = "6m"):
    from services.kite_auth import get_kite_client
    from datetime import datetime, timedelta

    symbol = symbol.upper()
    cache_key = (symbol, interval, range)
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

    days = RANGE_TO_DAYS.get(range, 182)
    # Kite caps minute-interval requests at 60 days per call regardless of
    # requested range — clamp so we don't silently get an empty/error response
    if interval != "day":
        days = min(days, 59)

    # BUG FIX (Oct 5 2026): datetime.now() is naive server time -- Railway
    # runs in UTC, but Kite's historical_data interprets the date strings we
    # send as IST (the exchange timezone). Sending unconverted UTC "now" as
    # "to_date" made Kite think the request ended ~5h30m earlier than it
    # actually did (e.g. 04:03 UTC sent/read as 04:03 IST, before market
    # open), so it silently returned only up to the last fully-elapsed
    # session and never today's candles, however long the market had been
    # open. This looked exactly like a Kite data-availability lag but was
    # entirely our own timezone bug -- confirmed by cross-checking against
    # the live oi_snapshots capture pipeline, which had today's real-time
    # price the whole time.
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    to_date = datetime.now(ist).replace(tzinfo=None)
    from_date = to_date - timedelta(days=days)

    try:
        candles = kite.historical_data(
            token,
            from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date.strftime("%Y-%m-%d %H:%M:%S"),
            interval,
        )
    except Exception as e:
        result = {"symbol": symbol, "candles": [], "error": str(e)}
        _chart_cache[cache_key] = (now_epoch, result)
        return result

    out = []
    for c in candles:
        ts = c["date"]
        # BUG FIX (Oct 5 2026): lightweight-charts' Time type only accepts a
        # plain "YYYY-MM-DD" string for daily (business-day) candles -- for
        # any intraday interval it needs a numeric Unix timestamp (seconds).
        # Sending a full ISO datetime string ("2026-10-01T09:15:00+05:30")
        # for intraday candles isn't a format the library understands as
        # either type; it silently produced garbage bar widths/positions
        # (giant blown-up candles) instead of a clean error, since the chart
        # always failed before reaching render until the 1D-range fix above.
        out.append({
            "time": ts.strftime("%Y-%m-%d") if interval == "day" else int(ts.timestamp()),
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c.get("volume", 0),
        })

    result = {"symbol": symbol, "interval": interval, "range": range, "candles": out}
    _chart_cache[cache_key] = (now_epoch, result)
    return result
