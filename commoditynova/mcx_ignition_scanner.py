import logging
from datetime import datetime, timedelta, date
import pytz
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

THRESHOLDS = {
    "CRUDEOIL":   {"oi": 2.0,  "price": 0.5, "volume": 1.8},
    "GOLD":       {"oi": 1.5,  "price": 0.3, "volume": 1.6},
    "SILVER":     {"oi": 2.0,  "price": 0.6, "volume": 2.0},
    "NATURALGAS": {"oi": 3.0,  "price": 1.0, "volume": 2.5},
}


def get_or_set_session_open_oi(
    commodity: str,
    current_oi: int,
    supabase,
    session_open_oi: dict,
) -> int:
    """
    Returns today's session open OI baseline.
    - If already in memory dict → use it (fastest path)
    - If in Supabase for today → load into memory and use it
    - If neither → this is first scan of day → store in both
    Survives Railway restarts because baseline is in Supabase.
    """
    today = date.today().isoformat()

    # Already in memory for today
    if commodity in session_open_oi:
        return session_open_oi[commodity]

    # Try loading from Supabase
    try:
        result = supabase.table("mcx_ignition_signals") \
            .select("session_open_oi, session_date") \
            .eq("commodity", commodity) \
            .execute()

        if result.data:
            row = result.data[0]
            stored_date = str(row.get("session_date", ""))
            stored_oi   = row.get("session_open_oi", 0)

            if stored_date == today and stored_oi and stored_oi > 0:
                # Valid baseline for today — load into memory
                session_open_oi[commodity] = stored_oi
                logger.info(f"{commodity}: loaded session baseline from Supabase — OI {stored_oi}")
                return stored_oi

    except Exception as e:
        logger.warning(f"{commodity}: could not load session baseline — {e}")

    # No baseline yet — set it now (first scan of day)
    session_open_oi[commodity] = current_oi
    logger.info(f"{commodity}: new session baseline set — OI {current_oi}")
    return current_oi


# ─────────────────────────────────────────────
# PILLAR 1 — OI change (5-min delta + cumulative)
# ─────────────────────────────────────────────
def check_oi_pillar(
    commodity: str,
    option_symbols: list,
    kite: KiteConnect,
    prev_oi: dict,
    session_open_oi: dict,
    supabase,
) -> dict:
    threshold = THRESHOLDS[commodity]["oi"]

    try:
        quotes = kite.quote(option_symbols)

        ce_oi = sum(q.get("oi", 0) for sym, q in quotes.items() if sym.endswith("CE"))
        pe_oi = sum(q.get("oi", 0) for sym, q in quotes.items() if sym.endswith("PE"))
        current_oi = ce_oi + pe_oi

        # ── 5-min delta ──────────────────────────────
        prev = prev_oi.get(commodity, 0)
        if prev == 0:
            prev_oi[commodity] = current_oi
            oi_change_pct = 0.0
            passed = False
        else:
            oi_change_pct = ((current_oi - prev) / prev) * 100 if prev else 0
            prev_oi[commodity] = current_oi
            passed = abs(oi_change_pct) >= threshold

        # ── Cumulative since session open ─────────────
        open_oi = get_or_set_session_open_oi(
            commodity, current_oi, supabase, session_open_oi
        )
        cumulative_oi_pct = ((current_oi - open_oi) / open_oi) * 100 if open_oi else 0

        # ── Directional bias CE vs PE ─────────────────
        if ce_oi > pe_oi * 1.1:
            cumulative_direction = "bearish"
        elif pe_oi > ce_oi * 1.1:
            cumulative_direction = "bullish"
        else:
            cumulative_direction = "neutral"

        return {
            "passed":               passed,
            "oi_change_pct":        round(oi_change_pct, 2),
            "threshold":            threshold,
            "current_oi":           current_oi,
            "cumulative_oi_pct":    round(cumulative_oi_pct, 2),
            "cumulative_direction": cumulative_direction,
            "session_open_oi":      open_oi,
        }

    except Exception as e:
        logger.error(f"{commodity} OI pillar error: {e}")
        return {
            "passed": False, "oi_change_pct": 0.0,
            "threshold": threshold, "current_oi": 0,
            "cumulative_oi_pct": 0.0,
            "cumulative_direction": "neutral",
            "session_open_oi": 0,
        }


# ─────────────────────────────────────────────
# PILLAR 2 — Price breakout
# ─────────────────────────────────────────────
def check_price_pillar(
    commodity: str,
    futures_symbol: str,
    candles: list,
    kite: KiteConnect,
) -> dict:
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
            "range_high": range_high, "range_low": range_low,
            "current_price": ltp, "threshold": threshold,
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
def check_volume_pillar(commodity: str, candles: list) -> dict:
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
# SIGNAL ENGINE
# ─────────────────────────────────────────────
def compute_signal(oi: dict, price: dict, volume: dict) -> dict:
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
    if direction == "up":   direction = "bullish"
    elif direction == "down": direction = "bearish"

    return {"status": status, "pillars_met": pillars_met,
            "signal_score": score, "direction": direction}


def build_scan_note(commodity: str, signal: dict, price: dict, oi: dict) -> str:
    cumulative = oi.get("cumulative_oi_pct", 0)
    cum_dir    = oi.get("cumulative_direction", "neutral")
    sign       = "+" if cumulative >= 0 else ""

    if signal["status"] == "fired":
        d   = "above" if signal["direction"] == "bullish" else "below"
        ref = price.get("range_high") if signal["direction"] == "bullish" else price.get("range_low")
        return (f"{signal['direction'].capitalize()} ignition — all 3 conditions met. "
                f"Price {d} {ref}. Session OI {sign}{cumulative:.1f}% ({cum_dir}).")
    elif signal["status"] == "watch":
        return (f"{signal['pillars_met']}/3 conditions met — monitoring. "
                f"Session OI {sign}{cumulative:.1f}% ({cum_dir}).")
    else:
        return (f"No signal — market quiet. "
                f"Session OI {sign}{cumulative:.1f}% ({cum_dir}).")


# ─────────────────────────────────────────────
# MAIN SCAN FUNCTION
# ─────────────────────────────────────────────
def run_ignition_scan(
    kite: KiteConnect,
    supabase,
    candles_cache: dict,
    prev_oi: dict,
    session_open_oi: dict = None,
):
    from commoditynova.mcx_instruments import get_cached_instruments

    if session_open_oi is None:
        session_open_oi = {}

    instruments = get_cached_instruments(supabase)
    if not instruments:
        logger.warning("No cached instruments — skipping scan.")
        return

    now_ist   = datetime.now(IST)
    today_str = now_ist.date().isoformat()
    fired_commodities = []

    for commodity, inst in instruments.items():
        try:
            option_symbols = inst.get("option_symbols", [])
            futures_symbol = inst["futures_symbol"]
            candles        = candles_cache.get(commodity, [])
            atm_strike     = inst.get("atm_strike")
            expiry_date    = inst.get("expiry_date")

            oi_result     = check_oi_pillar(
                commodity, option_symbols, kite, prev_oi, session_open_oi, supabase
            )
            price_result  = check_price_pillar(commodity, futures_symbol, candles, kite)
            volume_result = check_volume_pillar(commodity, candles)

            signal = compute_signal(oi_result, price_result, volume_result)
            note   = build_scan_note(commodity, signal, price_result, oi_result)

            if signal["status"] == "fired":
                fired_commodities.append(commodity)

            row = {
                "commodity":            commodity,
                "status":               signal["status"],
                "direction":            signal["direction"],
                "signal_score":         signal["signal_score"],
                "pillars_met":          signal["pillars_met"],
                "oi_change_pct":        oi_result["oi_change_pct"],
                "oi_threshold":         oi_result["threshold"],
                "oi_passed":            oi_result["passed"],
                "current_price":        price_result["current_price"],
                "range_high":           price_result["range_high"],
                "range_low":            price_result["range_low"],
                "price_chg_pct":        price_result["price_chg_pct"],
                "price_threshold":      price_result["threshold"],
                "price_passed":         price_result["passed"],
                "breakout_direction":   price_result["breakout_direction"],
                "current_volume":       volume_result["current_volume"],
                "avg_volume":           volume_result["avg_volume"],
                "volume_ratio":         volume_result["volume_ratio"],
                "volume_threshold":     volume_result["threshold"],
                "volume_passed":        volume_result["passed"],
                "expiry_date":          str(expiry_date) if expiry_date else None,
                "atm_strike":           atm_strike,
                "scan_note":            note,
                "session_open_oi":      oi_result["session_open_oi"],
                "cumulative_oi_pct":    oi_result["cumulative_oi_pct"],
                "cumulative_direction": oi_result["cumulative_direction"],
                "session_date":         today_str,
                "scanned_at":           now_ist.isoformat(),
                "updated_at":           now_ist.isoformat(),
            }

            supabase.table("mcx_ignition_signals").upsert(
                row, on_conflict="commodity"
            ).execute()

            if signal["status"] == "fired":
                supabase.table("mcx_ignition_history").insert({
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
                }).execute()

            logger.info(
                f"{commodity}: {signal['status'].upper()} "
                f"(score={signal['signal_score']}, pillars={signal['pillars_met']}/3, "
                f"price={price_result['current_price']}, "
                f"cum_oi={oi_result['cumulative_oi_pct']:+.1f}% {oi_result['cumulative_direction']})"
            )

        except Exception as e:
            logger.error(f"Scan failed for {commodity}: {e}")
            continue

    if fired_commodities:
        logger.info(f"IGNITION FIRED: {', '.join(fired_commodities)}")
    else:
        logger.info(f"Scan complete — no ignitions. {now_ist.strftime('%H:%M')} IST")
