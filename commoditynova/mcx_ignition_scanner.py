import logging
from datetime import datetime, timedelta
import pytz
from kiteconnect import KiteConnect
from supabase import create_client

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Per-commodity thresholds
THRESHOLDS = {
    "CRUDEOIL":   {"oi": 2.0,  "price": 0.5, "volume": 1.8},
    "GOLD":       {"oi": 1.5,  "price": 0.3, "volume": 1.6},
    "SILVER":     {"oi": 2.0,  "price": 0.6, "volume": 2.0},
    "NATURALGAS": {"oi": 3.0,  "price": 1.0, "volume": 2.5},
}


# ─────────────────────────────────────────────
# PILLAR 1 — OI change
# ─────────────────────────────────────────────
def check_oi_pillar(
    commodity: str,
    option_symbols: list,
    kite: KiteConnect,
    prev_oi: dict,
) -> dict:
    """
    Fetch current OI for all ATM±5 option strikes.
    Compare total OI now vs total OI from previous scan (stored in prev_oi dict).
    Returns pct change and whether threshold is met.
    """
    threshold = THRESHOLDS[commodity]["oi"]

    try:
        quotes = kite.quote(option_symbols)
        current_oi = sum(
            q.get("oi", 0) for q in quotes.values()
        )

        prev = prev_oi.get(commodity, 0)
        if prev == 0:
            # First run — store and skip
            prev_oi[commodity] = current_oi
            return {"passed": False, "oi_change_pct": 0.0, "threshold": threshold}

        oi_change_pct = ((current_oi - prev) / prev) * 100 if prev else 0
        prev_oi[commodity] = current_oi

        passed = abs(oi_change_pct) >= threshold
        return {
            "passed": passed,
            "oi_change_pct": round(oi_change_pct, 2),
            "threshold": threshold,
        }

    except Exception as e:
        logger.error(f"{commodity} OI pillar error: {e}")
        return {"passed": False, "oi_change_pct": 0.0, "threshold": threshold}


# ─────────────────────────────────────────────
# PILLAR 2 — Price breakout
# ─────────────────────────────────────────────
def check_price_pillar(
    commodity: str,
    futures_symbol: str,
    candles: list,
    kite: KiteConnect,
) -> dict:
    """
    Get current LTP for futures.
    Compare against high/low of the prior completed 15-min candle.
    Returns direction and whether threshold is met.
    """
    threshold = THRESHOLDS[commodity]["price"]

    try:
        quote = kite.quote([futures_symbol])
        ltp = quote[futures_symbol]["last_price"]

        if not candles or len(candles) < 2:
            return {
                "passed": False, "price_chg_pct": 0.0,
                "range_high": 0, "range_low": 0,
                "current_price": ltp, "threshold": threshold,
                "breakout_direction": None,
            }

        # Use the second-to-last candle (last completed candle)
        ref_candle = candles[-2]
        range_high = ref_candle["high"]
        range_low  = ref_candle["low"]

        breakout_direction = None
        price_chg_pct = 0.0

        if ltp > range_high:
            price_chg_pct = ((ltp - range_high) / range_high) * 100
            breakout_direction = "up"
        elif ltp < range_low:
            price_chg_pct = ((range_low - ltp) / range_low) * 100
            breakout_direction = "down"

        passed = price_chg_pct >= threshold

        return {
            "passed": passed,
            "price_chg_pct": round(price_chg_pct, 2),
            "range_high": range_high,
            "range_low": range_low,
            "current_price": ltp,
            "threshold": threshold,
            "breakout_direction": breakout_direction,
        }

    except Exception as e:
        logger.error(f"{commodity} price pillar error: {e}")
        return {
            "passed": False, "price_chg_pct": 0.0,
            "range_high": 0, "range_low": 0,
            "current_price": 0, "threshold": threshold,
            "breakout_direction": None,
        }


# ─────────────────────────────────────────────
# PILLAR 3 — Volume spike
# ─────────────────────────────────────────────
def check_volume_pillar(
    commodity: str,
    candles: list,
) -> dict:
    """
    Compare latest candle volume against 20-period average.
    Returns ratio and whether threshold is met.
    """
    threshold = THRESHOLDS[commodity]["volume"]

    try:
        if not candles or len(candles) < 5:
            return {"passed": False, "volume_ratio": 0.0,
                    "current_volume": 0, "avg_volume": 0, "threshold": threshold}

        volumes = [c["volume"] for c in candles]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])

        if avg_volume == 0:
            return {"passed": False, "volume_ratio": 0.0,
                    "current_volume": current_volume, "avg_volume": 0,
                    "threshold": threshold}

        volume_ratio = current_volume / avg_volume
        passed = volume_ratio >= threshold

        return {
            "passed": passed,
            "volume_ratio": round(volume_ratio, 2),
            "current_volume": int(current_volume),
            "avg_volume": int(avg_volume),
            "threshold": threshold,
        }

    except Exception as e:
        logger.error(f"{commodity} volume pillar error: {e}")
        return {"passed": False, "volume_ratio": 0.0,
                "current_volume": 0, "avg_volume": 0, "threshold": threshold}


# ─────────────────────────────────────────────
# SIGNAL ENGINE — combine all 3 pillars
# ─────────────────────────────────────────────
def compute_signal(oi: dict, price: dict, volume: dict) -> dict:
    """
    Combine 3 pillar results into a final signal.
    All 3 must pass for 'fired'. Exactly 2 = 'watch'. Else 'quiet'.
    Score = weighted sum (OI 35%, price 40%, volume 25%).
    """
    pillars_met = sum([oi["passed"], price["passed"], volume["passed"]])

    score = int(
        (min(abs(oi.get("oi_change_pct", 0)) / oi["threshold"], 2) * 0.35 +
         min(price.get("price_chg_pct", 0) / max(price["threshold"], 0.01), 2) * 0.40 +
         min(volume.get("volume_ratio", 0) / volume["threshold"], 2) * 0.25) * 50
    )
    score = min(score, 100)

    if pillars_met == 3:
        status = "fired"
    elif pillars_met == 2:
        status = "watch"
    else:
        status = "quiet"

    direction = price.get("breakout_direction")
    if direction == "up":
        direction = "bullish"
    elif direction == "down":
        direction = "bearish"

    return {"status": status, "pillars_met": pillars_met,
            "signal_score": score, "direction": direction}


def build_scan_note(commodity: str, signal: dict, price: dict) -> str:
    """Generate a human-readable summary of what triggered."""
    if signal["status"] == "fired":
        d = "above" if signal["direction"] == "bullish" else "below"
        ref = price.get("range_high") if signal["direction"] == "bullish" else price.get("range_low")
        return (f"{signal['direction'].capitalize()} ignition — all 3 conditions met. "
                f"Price {d} {ref} range level.")
    elif signal["status"] == "watch":
        return f"2/3 conditions met — monitoring for full ignition."
    else:
        return "No signal — market quiet."


# ─────────────────────────────────────────────
# MAIN SCAN FUNCTION
# ─────────────────────────────────────────────
def run_ignition_scan(kite: KiteConnect, supabase, candles_cache: dict, prev_oi: dict):
    """
    Main function called every 5 minutes by the scheduler.
    candles_cache: dict passed in from mcx_historical (pre-fetched)
    prev_oi: dict maintained across scans to track OI change
    """
    from commoditynova.mcx_instruments import get_cached_instruments

    instruments = get_cached_instruments(supabase)
    if not instruments:
        logger.warning("No cached instruments found — skipping scan. Run seed first.")
        return

    now_ist = datetime.now(IST)
    fired_commodities = []

    for commodity, inst in instruments.items():
        try:
            option_symbols = inst.get("option_symbols", [])
            futures_symbol = inst["futures_symbol"]
            candles = candles_cache.get(commodity, [])
            atm_strike = inst.get("atm_strike")
            expiry_date = inst.get("expiry_date")

            # Run 3 pillars
            oi_result     = check_oi_pillar(commodity, option_symbols, kite, prev_oi)
            price_result  = check_price_pillar(commodity, futures_symbol, candles, kite)
            volume_result = check_volume_pillar(commodity, candles)

            # Combine into signal
            signal = compute_signal(oi_result, price_result, volume_result)
            note   = build_scan_note(commodity, signal, price_result)

            if signal["status"] == "fired":
                fired_commodities.append(commodity)

            # Upsert current signal row
            row = {
                "commodity":          commodity,
                "status":             signal["status"],
                "direction":          signal["direction"],
                "signal_score":       signal["signal_score"],
                "pillars_met":        signal["pillars_met"],
                "oi_change_pct":      oi_result["oi_change_pct"],
                "oi_threshold":       oi_result["threshold"],
                "oi_passed":          oi_result["passed"],
                "current_price":      price_result["current_price"],
                "range_high":         price_result["range_high"],
                "range_low":          price_result["range_low"],
                "price_chg_pct":      price_result["price_chg_pct"],
                "price_threshold":    price_result["threshold"],
                "price_passed":       price_result["passed"],
                "breakout_direction": price_result["breakout_direction"],
                "current_volume":     volume_result["current_volume"],
                "avg_volume":         volume_result["avg_volume"],
                "volume_ratio":       volume_result["volume_ratio"],
                "volume_threshold":   volume_result["threshold"],
                "volume_passed":      volume_result["passed"],
                "expiry_date":        str(expiry_date) if expiry_date else None,
                "atm_strike":         atm_strike,
                "scan_note":          note,
                "scanned_at":         now_ist.isoformat(),
                "updated_at":         now_ist.isoformat(),
            }

            supabase.table("mcx_ignition_signals").upsert(
                row, on_conflict="commodity"
            ).execute()

            # If fired — also write to history log
            if signal["status"] == "fired":
                history_row = {
                    "commodity":     commodity,
                    "status":        signal["status"],
                    "direction":     signal["direction"],
                    "signal_score":  signal["signal_score"],
                    "current_price": price_result["current_price"],
                    "oi_change_pct": oi_result["oi_change_pct"],
                    "price_chg_pct": price_result["price_chg_pct"],
                    "volume_ratio":  volume_result["volume_ratio"],
                    "scan_note":     note,
                    "fired_at":      now_ist.isoformat(),
                }
                supabase.table("mcx_ignition_history").insert(history_row).execute()

            logger.info(
                f"{commodity}: {signal['status'].upper()} "
                f"(score={signal['signal_score']}, "
                f"pillars={signal['pillars_met']}/3, "
                f"price={price_result['current_price']})"
            )

        except Exception as e:
            logger.error(f"Scan failed for {commodity}: {e}")
            continue

    if fired_commodities:
        logger.info(f"IGNITION FIRED: {', '.join(fired_commodities)}")
    else:
        logger.info(f"Scan complete — no ignitions. {now_ist.strftime('%H:%M')} IST")
