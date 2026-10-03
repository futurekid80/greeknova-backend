"""On-demand price chart data for any NSE symbol (stock or index).

Deliberately NOT a backfill-everything design like vix_daily_history — with
120+ tracked symbols, storing candles for all of them continuously would be
wasted Supabase storage and Kite API load for symbols nobody's looking at.
Instead: fetch straight from Kite when a user actually opens a symbol's
chart, with a short in-memory cache so repeat opens/timeframe-switches for
the same symbol don't re-hit Kite every time.
"""
import time as time_module

INDEX_NSE_MAP = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE"}

# (symbol, interval) -> (fetched_at_epoch, candles)
_chart_cache: dict = {}
CACHE_TTL = {"day": 300, "minute": 60, "5minute": 60, "15minute": 90}

RANGE_TO_DAYS = {"1d": 1, "1m": 30, "3m": 90, "6m": 182, "1y": 365, "3y": 1095}


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

    to_date = datetime.now()
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
        out.append({
            "time": ts.strftime("%Y-%m-%d") if interval == "day" else ts.isoformat(),
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c.get("volume", 0),
        })

    result = {"symbol": symbol, "interval": interval, "range": range, "candles": out}
    _chart_cache[cache_key] = (now_epoch, result)
    return result
