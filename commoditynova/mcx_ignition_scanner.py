import logging
from datetime import datetime, date
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

# Unwind thresholds — how far from peak before flagging
UNWIND_WARNING_PCT  = 1.5   # rolling_over
UNWIND_CONFIRM_PCT  = 3.0   # unwinding


def get_or_set_session_open_oi(commodity, current_oi, supabase, session_open_oi):
    today = date.today().isoformat()
    if commodity in session_open_oi:
        return session_open_oi[commodity]
    try:
        result = supabase.table("mcx_ignition_signals") \
            .select("session_open_oi, session_date") \
            .eq("commodity", commodity).execute()
        if result.data:
            row = result.data[0]
            if str(row.get("session_date", "")) == today and row.get("session_open_oi", 0) > 0:
                session_open_oi[commodity] = row["session_open_oi"]
                return row["session_open_oi"]
    except Exception as e:
        logger.warning(f"{commodity}: could not load session baseline — {e}")
    session_open_oi[commodity] = current_oi
    return current_oi


def get_or_set_session_open_price(commodity, current_price, supabase, session_open_price_dict):
    """
    Returns today's session open price.
    Persists in Supabase so Railway restarts don't wipe it.
    """
    today = date.today().isoformat()

    if commodity in session_open_price_dict:
        return session_open_price_dict[commodity]

    try:
        result = supabase.table("mcx_ignition_signals")             .select("session_open_price, session_date")             .eq("commodity", commodity).execute()
        if result.data:
            row = result.data[0]
            if str(row.get("session_date", "")) == today and row.get("session_open_price", 0):
                session_open_price_dict[commodity] = float(row["session_open_price"])
                return session_open_price_dict[commodity]
    except Exception as e:
        logger.warning(f"{commodity}: could not load session open price — {e}")

    session_open_price_dict[commodity] = current_price
    return current_price


def get_or_set_session_peak(commodity, current_cumulative_pct, supabase, session_peak_oi):
    """
    Returns the session peak cumulative OI %.
    Updates peak if current is higher.
    Persists in Supabase so Railway restarts don't wipe it.
    """
    today = date.today().isoformat()

    # Load from Supabase if not in memory
    if commodity not in session_peak_oi:
        try:
            result = supabase.table("mcx_ignition_signals") \
                .select("session_peak_oi_pct, session_date") \
                .eq("commodity", commodity).execute()
            if result.data:
                row = result.data[0]
                if str(row.get("session_date", "")) == today:
                    stored_peak = row.get("session_peak_oi_pct", 0) or 0
                    session_peak_oi[commodity] = float(stored_peak)
                else:
                    session_peak_oi[commodity] = 0.0
            else:
                session_peak_oi[commodity] = 0.0
        except Exception as e:
            logger.warning(f"{commodity}: could not load session peak — {e}")
            session_peak_oi[commodity] = 0.0

    # Update peak if current is higher (track absolute value for both directions)
    current_abs = abs(current_cumulative_pct)
    peak_abs    = abs(session_peak_oi.get(commodity, 0))

    if current_abs > peak_abs:
        session_peak_oi[commodity] = current_cumulative_pct
        logger.debug(f"{commodity}: new session peak — {current_cumulative_pct:+.1f}%")

    return session_peak_oi[commodity]


def compute_unwind_status(current_pct: float, peak_pct: float) -> str:
    """
    Compare current cumulative OI vs session peak.
    Returns: 'building' | 'rolling_over' | 'unwinding'
    Drop is calculated as peak - current (not abs) to handle
    cases where OI goes from positive to negative.
    """
    if abs(peak_pct) < 1.0:
        return "building"  # peak too small to matter yet

    drop_from_peak = abs(peak_pct) - current_pct  # no abs() on current

    if drop_from_peak >= UNWIND_CONFIRM_PCT:
        return "unwinding"
    elif drop_from_peak >= UNWIND_WARNING_PCT:
        return "rolling_over"
    else:
        return "building"


# ─────────────────────────────────────────────
# PILLAR 1 — OI change
# ─────────────────────────────────────────────
def check_oi_pillar(
    commodity, option_symbols, oi_symbols, kite, prev_oi,
    session_open_oi, session_peak_oi, supabase, candles=None,
    session_open_price_dict=None
):
    """
    option_symbols — full ATM±10 range, used for support/resistance
    oi_symbols     — tight ATM±5 range, used for OI pillar + cumulative tracking
    candles        — futures candles with OI, used for direction detection
    """
    threshold = THRESHOLDS[commodity]["oi"]

    try:
        # Fetch all symbols (wide range) for SR levels
        quotes = kite.quote(option_symbols)

        # Use only tight range symbols for OI calculation
        oi_quotes = {k: v for k, v in quotes.items() if k in set(oi_symbols)}
        ce_oi  = sum(q.get("oi", 0) for sym, q in oi_quotes.items() if sym.endswith("CE"))
        pe_oi  = sum(q.get("oi", 0) for sym, q in oi_quotes.items() if sym.endswith("PE"))
        current_oi = ce_oi + pe_oi

        # 5-min delta
        prev = prev_oi.get(commodity, 0)
        prev_oi_change_pct = prev_oi.get(f"{commodity}_prev_change", 0.0)
        prev_ce_oi = prev_oi.get(f"{commodity}_ce", 0)
        prev_pe_oi = prev_oi.get(f"{commodity}_pe", 0)

        if prev == 0:
            prev_oi[commodity]          = current_oi
            prev_oi[f"{commodity}_ce"]  = ce_oi
            prev_oi[f"{commodity}_pe"]  = pe_oi
            oi_change_pct = 0.0
            passed = False
            ce_oi_delta = 0
            pe_oi_delta = 0
        else:
            oi_change_pct = ((current_oi - prev) / prev) * 100 if prev else 0
            prev_oi[f"{commodity}_prev_change"] = oi_change_pct
            prev_oi[commodity]          = current_oi
            ce_oi_delta = ce_oi - prev_ce_oi
            pe_oi_delta = pe_oi - prev_pe_oi
            prev_oi[f"{commodity}_ce"]  = ce_oi
            prev_oi[f"{commodity}_pe"]  = pe_oi
            passed = abs(oi_change_pct) >= threshold

        # Cumulative since open
        open_oi = get_or_set_session_open_oi(commodity, current_oi, supabase, session_open_oi)
        cumulative_oi_pct = ((current_oi - open_oi) / open_oi) * 100 if open_oi else 0

        # Intraday direction — price vs session open + options OI
        # MCX futures OI is EOD only, so use price movement from session open
        futures_oi_direction = "neutral"
        price_now  = prev_oi.get(f"{commodity}_price_now", 0)

        # Get session open price — persisted in Supabase to survive restarts
        if session_open_price_dict is not None and price_now > 0:
            price_open = get_or_set_session_open_price(
                commodity, price_now, supabase, session_open_price_dict
            )
        else:
            price_open = prev_oi.get(f"{commodity}_session_open_price", 0)

        if price_open and price_open > 0:
            price_chg_from_open = ((price_now - price_open) / price_open) * 100

            # Classify using price change from open + options OI direction
            price_up   = price_chg_from_open > 0.3
            price_down = price_chg_from_open < -0.3
            oi_up      = cumulative_oi_pct > 1.0
            oi_down    = cumulative_oi_pct < -1.0

            if price_up and oi_up:
                futures_oi_direction = "long buildup"
            elif price_down and oi_up:
                futures_oi_direction = "short buildup"
            elif price_up and oi_down:
                futures_oi_direction = "short covering"
            elif price_down and oi_down:
                futures_oi_direction = "long unwinding"
            elif price_up:
                futures_oi_direction = "short covering"  # price up, OI flat = covering
            elif price_down:
                futures_oi_direction = "short buildup"   # price down, OI flat = building shorts

        # CE/PE ratio for cumulative_direction (kept for compatibility)
        if ce_oi > pe_oi * 1.1:
            cumulative_direction = "bearish"
        elif pe_oi > ce_oi * 1.1:
            cumulative_direction = "bullish"
        else:
            cumulative_direction = "neutral"

        # Peak tracking + unwind status
        peak_pct       = get_or_set_session_peak(commodity, cumulative_oi_pct, supabase, session_peak_oi)
        unwind_status  = compute_unwind_status(cumulative_oi_pct, peak_pct)

        # Support/resistance from strike-level OI
        # PE wall = strike with highest PE OI = support
        # CE wall = strike with highest CE OI = resistance
        support_strike    = 0
        support_oi        = 0
        resistance_strike = 0
        resistance_oi     = 0

        for sym, q in quotes.items():
            oi_val = q.get("oi", 0)
            if oi_val == 0:
                continue
            # Extract strike from symbol e.g. MCX:CRUDEOIL26JUN8300PE
            try:
                ts = sym.split(":")[1]  # CRUDEOIL26JUN8300PE
                opt_type = ts[-2:]      # PE or CE
                # Strike is between last alpha chars and option type
                strike_str = ""
                for ch in reversed(ts[:-2]):
                    if ch.isdigit():
                        strike_str = ch + strike_str
                    else:
                        break
                if not strike_str:
                    continue
                strike_val = float(strike_str)
            except Exception:
                continue

            if opt_type == "PE" and oi_val > support_oi:
                support_oi     = oi_val
                support_strike = strike_val
            elif opt_type == "CE" and oi_val > resistance_oi:
                resistance_oi     = oi_val
                resistance_strike = strike_val

        return {
            "passed":                passed,
            "oi_change_pct":         round(oi_change_pct, 2),
            "prev_oi_change_pct":    round(prev_oi_change_pct, 2),
            "threshold":             threshold,
            "current_oi":            current_oi,
            "cumulative_oi_pct":     round(cumulative_oi_pct, 2),
            "cumulative_direction":  cumulative_direction,
            "futures_oi_direction":  futures_oi_direction,
            "session_open_oi":       open_oi,
            "session_peak_oi_pct":   round(peak_pct, 2),
            "oi_unwind_status":      unwind_status,
            "support_strike":        support_strike,
            "support_oi":            support_oi,
            "resistance_strike":     resistance_strike,
            "resistance_oi":         resistance_oi,
            "ce_oi_delta":           ce_oi_delta,
            "pe_oi_delta":           pe_oi_delta,
        }

    except Exception as e:
        logger.error(f"{commodity} OI pillar error: {e}")
        return {
            "passed": False, "oi_change_pct": 0.0, "prev_oi_change_pct": 0.0,
            "threshold": threshold, "current_oi": 0, "cumulative_oi_pct": 0.0,
            "cumulative_direction": "neutral", "futures_oi_direction": "neutral",
            "session_open_oi": 0, "session_peak_oi_pct": 0.0,
            "oi_unwind_status": "building", "support_strike": 0, "support_oi": 0,
            "resistance_strike": 0, "resistance_oi": 0,
            "ce_oi_delta": 0, "pe_oi_delta": 0,
        }


# ─────────────────────────────────────────────
# PILLAR 2 — Price breakout
# ─────────────────────────────────────────────
def check_price_pillar(commodity, futures_symbol, candles, kite):
    threshold = THRESHOLDS[commodity]["price"]
    try:
        quote = kite.quote([futures_symbol])
        ltp        = quote[futures_symbol]["last_price"]
        futures_oi = quote[futures_symbol].get("oi", 0)

        if not candles or len(candles) < 2:
            return {"passed": False, "price_chg_pct": 0.0, "range_high": 0,
                    "range_low": 0, "current_price": ltp, "threshold": threshold,
                    "breakout_direction": None}

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

        return {
            "passed": price_chg_pct >= threshold,
            "price_chg_pct": round(price_chg_pct, 2),
            "range_high": range_high, "range_low": range_low,
            "current_price": ltp, "threshold": threshold,
            "breakout_direction": breakout_direction,
            "futures_oi": futures_oi,
        }
    except Exception as e:
        logger.error(f"{commodity} price pillar error: {e}")
        return {"passed": False, "price_chg_pct": 0.0, "range_high": 0,
                "range_low": 0, "current_price": 0, "threshold": threshold,
                "breakout_direction": None, "futures_oi": 0}


# ─────────────────────────────────────────────
# PILLAR 3 — Volume spike
# ─────────────────────────────────────────────
def check_volume_pillar(commodity, candles):
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
        return {
            "passed": volume_ratio >= threshold,
            "volume_ratio": round(volume_ratio, 2),
            "current_volume": int(current_volume),
            "avg_volume": int(avg_volume),
            "threshold": threshold,
        }
    except Exception as e:
        logger.error(f"{commodity} volume pillar error: {e}")
        return {"passed": False, "volume_ratio": 0.0,
                "current_volume": 0, "avg_volume": 0, "threshold": threshold}



def compute_divergence(price_chg_pct, oi_change_pct, prev_oi_change_pct, oi_unwind_status, breakout_direction=None):
    """Price vs OI divergence — continuation / exhaustion / coiling / trap / neutral."""
    # Use breakout_direction to determine if price is actually moving meaningfully
    # breakout_direction is None when price is inside range (flat)
    price_direction = "moving" if (price_chg_pct >= 0.15 and breakout_direction is not None) else "flat"

    if oi_unwind_status in ("unwinding", "rolling_over"):
        oi_momentum = "unwinding"
    elif abs(oi_change_pct) >= 0.5:
        oi_decel = abs(prev_oi_change_pct) - abs(oi_change_pct)
        oi_momentum = "slowing" if oi_decel >= 1.0 else "building"
    else:
        oi_momentum = "quiet"

    if oi_momentum == "unwinding" and price_direction == "moving":
        return {"divergence_label": "trap",
                "divergence_note": "Price moving but OI unwinding — possible fake move"}
    elif price_direction == "moving" and oi_momentum == "building":
        return {"divergence_label": "continuation",
                "divergence_note": "OI confirming price move — trend intact"}
    elif price_direction == "moving" and oi_momentum == "slowing":
        return {"divergence_label": "exhaustion",
                "divergence_note": "Price moving but OI momentum slowing — possible exhaustion"}
    elif price_direction == "flat" and oi_momentum == "building":
        return {"divergence_label": "coiling",
                "divergence_note": "OI building with price flat — breakout likely coming"}
    return {"divergence_label": "neutral", "divergence_note": ""}


# ─────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────
def compute_signal(oi, price, volume):
    pillars_met = sum([oi["passed"], price["passed"], volume["passed"]])
    score = int(
        (min(abs(oi.get("oi_change_pct", 0)) / oi["threshold"], 2) * 0.35 +
         min(price.get("price_chg_pct", 0) / max(price["threshold"], 0.01), 2) * 0.40 +
         min(volume.get("volume_ratio", 0) / volume["threshold"], 2) * 0.25) * 50
    )
    score = min(score, 100)
    status = "fired" if pillars_met == 3 else "watch" if pillars_met == 2 else "quiet"
    direction = price.get("breakout_direction")
    if direction == "up":   direction = "bullish"
    elif direction == "down": direction = "bearish"
    return {"status": status, "pillars_met": pillars_met,
            "signal_score": score, "direction": direction}


def build_scan_note(commodity, signal, price, oi):
    cumulative  = oi.get("cumulative_oi_pct", 0)
    cum_dir     = oi.get("cumulative_direction", "neutral")
    peak        = oi.get("session_peak_oi_pct", 0)
    unwind      = oi.get("oi_unwind_status", "building")
    sign        = "+" if cumulative >= 0 else ""

    unwind_note = ""
    if unwind == "unwinding":
        unwind_note = f" Peak was {'+' if peak >= 0 else ''}{peak:.1f}% — OI unwinding, possible exhaustion."
    elif unwind == "rolling_over":
        unwind_note = f" Peak was {'+' if peak >= 0 else ''}{peak:.1f}% — OI rolling over, watch for reversal."

    if signal["status"] == "fired":
        d   = "above" if signal["direction"] == "bullish" else "below"
        ref = price.get("range_high") if signal["direction"] == "bullish" else price.get("range_low")
        return (f"{signal['direction'].capitalize()} ignition — all 3 conditions met. "
                f"Price {d} {ref}. Session OI {sign}{cumulative:.1f}% ({cum_dir}).{unwind_note}")
    elif signal["status"] == "watch":
        return (f"{signal['pillars_met']}/3 conditions met — monitoring. "
                f"Session OI {sign}{cumulative:.1f}% ({cum_dir}).{unwind_note}")
    else:
        return (f"No signal — market quiet. "
                f"Session OI {sign}{cumulative:.1f}% ({cum_dir}).{unwind_note}")


# ─────────────────────────────────────────────
# MAIN SCAN FUNCTION
# ─────────────────────────────────────────────
def run_ignition_scan(kite, supabase, candles_cache, prev_oi,
                      session_open_oi=None, session_peak_oi=None,
                      prev_strike_oi=None, session_open_price_dict=None):
    from commoditynova.mcx_instruments import get_cached_instruments
    from commoditynova.mcx_strike_analyzer import analyze_strikes

    if session_open_oi is None:         session_open_oi = {}
    if session_peak_oi is None:         session_peak_oi = {}
    if prev_strike_oi is None:          prev_strike_oi  = {}
    if session_open_price_dict is None: session_open_price_dict = {}

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

            # Build tight OI symbols (ATM±oi_range) from cached instruments
            atm            = inst.get("atm_strike", 0)
            strike_step    = inst.get("tick_size", 1)  # reuse tick_size as proxy
            oi_range       = inst.get("oi_range", 5)
            all_syms       = inst.get("option_symbols", [])

            # Filter to ATM±oi_range strikes for OI pillar calculation
            # Extract strike from symbol and keep only those within oi_range
            oi_symbols = []
            from commoditynova.mcx_instruments import MCX_COMMODITIES
            cfg = MCX_COMMODITIES.get(commodity, {})
            s_step = cfg.get("strike_step", 100)
            o_range = cfg.get("oi_range", 5)
            for sym in all_syms:
                try:
                    ts = sym.split(":")[1]
                    strike_str = ""
                    for ch in reversed(ts[:-2]):
                        if ch.isdigit():
                            strike_str = ch + strike_str
                        else:
                            break
                    if strike_str:
                        sv = float(strike_str)
                        if abs(sv - atm) <= o_range * s_step:
                            oi_symbols.append(sym)
                except Exception:
                    continue

            if not oi_symbols:
                oi_symbols = option_symbols  # fallback to all

            price_result  = check_price_pillar(commodity, futures_symbol, candles, kite)

            # Store price BEFORE calling check_oi_pillar so direction logic has current price
            prev_oi[f"{commodity}_price_prev"]  = prev_oi.get(f"{commodity}_price_now", price_result["current_price"])
            prev_oi[f"{commodity}_price_now"]   = price_result["current_price"]

            # Set session open price on first scan of day
            if f"{commodity}_session_open_price" not in prev_oi:
                prev_oi[f"{commodity}_session_open_price"] = price_result["current_price"]

            oi_result     = check_oi_pillar(
                commodity, option_symbols, oi_symbols, kite,
                prev_oi, session_open_oi, session_peak_oi, supabase, candles,
                session_open_price_dict
            )
            # Get overnight direction from instruments cache
            overnight_oi_direction = inst.get("overnight_direction", "neutral") or "neutral"
            volume_result = check_volume_pillar(commodity, candles)

            signal    = compute_signal(oi_result, price_result, volume_result)
            divergence = compute_divergence(
                price_result["price_chg_pct"],
                oi_result["oi_change_pct"],
                oi_result["prev_oi_change_pct"],
                oi_result["oi_unwind_status"],
                price_result["breakout_direction"],
            )
            note   = build_scan_note(commodity, signal, price_result, oi_result)

            # Strike-level analysis — writing vs buying
            strike_analysis = analyze_strikes(
                commodity=commodity,
                quotes=kite.quote(option_symbols),
                current_price=price_result["current_price"],
                prev_strike_oi=prev_strike_oi,
                supabase=supabase,
            )

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
                "session_peak_oi_pct":  oi_result["session_peak_oi_pct"],
                "oi_unwind_status":     oi_result["oi_unwind_status"],
                "prev_oi_change_pct":   oi_result["prev_oi_change_pct"],
                "divergence_label":     divergence["divergence_label"],
                "divergence_note":      divergence["divergence_note"],
                "support_strike":       oi_result["support_strike"],
                "support_oi":           oi_result["support_oi"],
                "resistance_strike":    oi_result["resistance_strike"],
                "resistance_oi":        oi_result["resistance_oi"],
                "ce_oi_delta":          oi_result["ce_oi_delta"],
                "pe_oi_delta":          oi_result["pe_oi_delta"],
                "rally_quality":        strike_analysis["rally_quality"],
                "rally_note":           strike_analysis["rally_note"],
                "ce_writing_count":     strike_analysis["ce_writing_count"],
                "pe_writing_count":     strike_analysis["pe_writing_count"],
                "ce_buying_count":      strike_analysis["ce_buying_count"],
                "pe_buying_count":      strike_analysis["pe_buying_count"],
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
                f"cum_oi={oi_result['cumulative_oi_pct']:+.1f}% "
                f"peak={oi_result['session_peak_oi_pct']:+.1f}% "
                f"unwind={oi_result['oi_unwind_status']})"
            )

        except Exception as e:
            logger.error(f"Scan failed for {commodity}: {e}")
            continue

    if fired_commodities:
        logger.info(f"IGNITION FIRED: {', '.join(fired_commodities)}")
    else:
        logger.info(f"Scan complete — no ignitions. {now_ist.strftime('%H:%M')} IST")
