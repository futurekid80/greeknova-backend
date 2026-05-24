import logging
from datetime import datetime, timedelta
import pytz
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# How many 15-min candles to fetch per commodity
# 20 candles = ~5 hours of history, enough for volume avg + breakout ref
CANDLE_COUNT = 20


def fetch_candles_for_commodity(
    commodity: str,
    futures_token: int,
    kite: KiteConnect,
    candle_count: int = CANDLE_COUNT,
) -> list:
    """
    Fetch last N x 15-minute candles for a commodity futures contract.
    Returns list of candle dicts: {date, open, high, low, close, volume, oi}
    """
    try:
        now_ist = datetime.now(IST)

        # MCX trades until 11:30 PM — look back far enough to get candle_count candles
        # On evening session days we need to look back across the session gap
        to_date   = now_ist
        from_date = now_ist - timedelta(hours=6)

        candles = kite.historical_data(
            instrument_token=futures_token,
            from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
            interval="15minute",
            oi=True,
        )

        if not candles:
            logger.warning(f"{commodity}: no candle data returned")
            return []

        # Return last candle_count candles only
        result = candles[-candle_count:]
        logger.debug(f"{commodity}: fetched {len(result)} candles")
        return result

    except Exception as e:
        logger.error(f"{commodity}: candle fetch failed — {e}")
        return []


def fetch_all_candles(instruments: dict, kite: KiteConnect) -> dict:
    """
    Fetch 15-min candles for all cached instruments in one pass.
    Called once per 5-min scan cycle, before the scanner runs.

    instruments: dict from get_cached_instruments()
    Returns: dict keyed by commodity name → list of candles
    """
    candles_cache = {}

    for commodity, inst in instruments.items():
        futures_token = inst.get("futures_token")
        if not futures_token:
            logger.warning(f"{commodity}: no futures token in cache, skipping candles")
            continue

        candles = fetch_candles_for_commodity(
            commodity=commodity,
            futures_token=futures_token,
            kite=kite,
        )
        candles_cache[commodity] = candles

    fetched = sum(1 for c in candles_cache.values() if c)
    logger.info(f"Candles fetched for {fetched}/{len(instruments)} commodities")
    return candles_cache


def get_range_high_low(candles: list) -> tuple[float, float]:
    """
    Helper — returns (high, low) of the last completed 15-min candle.
    Used by the price pillar breakout check.
    """
    if not candles or len(candles) < 2:
        return 0.0, 0.0
    ref = candles[-2]  # second-to-last = last completed candle
    return float(ref["high"]), float(ref["low"])


def get_avg_volume(candles: list) -> float:
    """
    Helper — returns average volume across all candles except the last.
    Used by the volume pillar spike check.
    """
    if not candles or len(candles) < 2:
        return 0.0
    vols = [c["volume"] for c in candles[:-1]]
    return sum(vols) / len(vols) if vols else 0.0
