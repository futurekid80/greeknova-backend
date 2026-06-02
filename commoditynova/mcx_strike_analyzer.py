import logging
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# How close to ATM to be considered "ATM"
ATM_BAND_PCT = 0.005  # 0.5% either side of current price

# Max distance from current price to consider a strike "meaningful"
# Based on typical daily range per commodity
STRIKE_PROXIMITY = {
    "CRUDEOIL":   500,    # ±₹500 — covers 2-3 days of moves
    "GOLD":       2000,   # ±₹2,000
    "SILVER":     8000,   # ±₹8,000
    "NATURALGAS": 30,     # ±₹30 — covers 2-3 days of moves
}


def classify_moneyness(strike: float, current_price: float, option_type: str) -> str:
    """
    Classify strike as ITM / ATM / OTM relative to current price.
    For CE: ITM = strike below price, OTM = strike above price
    For PE: ITM = strike above price, OTM = strike below price
    """
    band = current_price * ATM_BAND_PCT
    if abs(strike - current_price) <= band:
        return "ATM"
    if option_type == "CE":
        return "ITM" if strike < current_price else "OTM"
    else:  # PE
        return "ITM" if strike > current_price else "OTM"


def classify_activity(oi_delta: int, moneyness: str, option_type: str) -> str:
    """
    Determine if OI change represents writing, buying, or closing.

    Writing (selling options):
    - OTM CE OI increasing = call writers → bearish, defending overhead resistance
    - OTM PE OI increasing = put writers → bullish, defending floor support

    Buying (buying options):
    - OTM CE OI increasing rapidly = call buyers → bullish speculation
    - OTM PE OI increasing rapidly = put buyers → bearish speculation
    - ATM/ITM OI increasing = directional conviction

    We differentiate writing vs buying by:
    - OTM + moderate OI increase = likely writing (premium collection)
    - ATM/ITM + OI increase = likely buying (directional bet)
    - Any OI decrease = closing positions
    """
    if oi_delta == 0:
        return "unchanged"
    if oi_delta < 0:
        return "closing"
    # OI increasing
    if moneyness == "OTM":
        return "writing"   # OTM options being added = likely writing
    else:
        return "buying"    # ATM/ITM options being added = likely buying


def load_prev_strike_oi_from_supabase(commodity: str, prev_strike_oi: dict, supabase):
    """
    Load previous scan's per-strike OI from Supabase into memory dict.
    Called when prev_strike_oi is empty (after restart or first run).
    Keyed as: commodity_TRADINGSYMBOL → previous OI value
    """
    try:
        prev_rows = supabase.table("mcx_strike_oi")             .select("strike, option_type, current_oi, scanned_at")             .eq("commodity", commodity)             .order("scanned_at", desc=True)             .limit(42)             .execute()

        if not prev_rows.data:
            return

        # Get latest scan timestamp
        latest_ts = prev_rows.data[0]["scanned_at"]

        # Only load rows from the most recent scan
        for r in prev_rows.data:
            if r["scanned_at"] != latest_ts:
                break
            # Reconstruct key using strike and option_type
            key = f"{commodity}_{int(r['strike'])}_{r['option_type']}"
            prev_strike_oi[key] = r["current_oi"]

        logger.info(f"{commodity}: loaded {len(prev_strike_oi)} prev strike OI values from Supabase")

    except Exception as e:
        logger.warning(f"{commodity}: could not load prev strike OI — {e}")


def analyze_strikes(
    commodity: str,
    quotes: dict,
    current_price: float,
    prev_strike_oi: dict,
    supabase,
) -> dict:
    """
    Analyze per-strike OI to detect writing vs buying activity.
    Returns summary for frontend display.
    """
    # Load prev OI from Supabase if memory is empty (after restart)
    commodity_keys = [k for k in prev_strike_oi if k.startswith(f"{commodity}_")]
    if not commodity_keys:
        load_prev_strike_oi_from_supabase(commodity, prev_strike_oi, supabase)

    now_ist = datetime.now(IST)
    strike_rows = []

    ce_writing_strikes = []   # OTM CE being written = resistance levels
    pe_writing_strikes = []   # OTM PE being written = support levels
    ce_buying_strikes  = []   # CE being bought = bullish speculation
    pe_buying_strikes  = []   # PE being bought = bearish speculation

    for sym, q in quotes.items():
        try:
            # Parse symbol — e.g. MCX:CRUDEOIL26JUN9000CE
            ts = sym.split(":")[1]
            opt_type = ts[-2:]  # CE or PE

            # Extract strike
            strike_str = ""
            for ch in reversed(ts[:-2]):
                if ch.isdigit():
                    strike_str = ch + strike_str
                else:
                    break
            if not strike_str:
                continue
            strike = float(strike_str)

            current_oi = q.get("oi", 0)

            # Key: commodity_strike_opttype — matches Supabase load key format
            prev_key = f"{commodity}_{int(strike)}_{opt_type}"
            prev_oi  = prev_strike_oi.get(prev_key, current_oi)
            oi_delta = current_oi - prev_oi

            # Update prev OI for next scan
            prev_strike_oi[prev_key] = current_oi

            moneyness = classify_moneyness(strike, current_price, opt_type)
            activity  = classify_activity(oi_delta, moneyness, opt_type)

            strike_rows.append({
                "commodity":     commodity,
                "strike":        strike,
                "option_type":   opt_type,
                "current_oi":    current_oi,
                "oi_delta":      oi_delta,
                "moneyness":     moneyness,
                "activity":      activity,
                "price_at_scan": current_price,
                "scanned_at":    now_ist.isoformat(),
            })

            # Categorise for summary — only include strikes within meaningful range
            max_dist = STRIKE_PROXIMITY.get(commodity, 500)
            within_range = abs(strike - current_price) <= max_dist

            if within_range and oi_delta > 5:
                if activity == "writing" and opt_type == "CE":
                    ce_writing_strikes.append({"strike": strike, "oi_delta": oi_delta})
                elif activity == "writing" and opt_type == "PE":
                    pe_writing_strikes.append({"strike": strike, "oi_delta": oi_delta})
                elif activity == "buying" and opt_type == "CE":
                    ce_buying_strikes.append({"strike": strike, "oi_delta": oi_delta})
                elif activity == "buying" and opt_type == "PE":
                    pe_buying_strikes.append({"strike": strike, "oi_delta": oi_delta})

        except Exception as e:
            logger.debug(f"Strike parse error for {sym}: {e}")
            continue

    # Write to Supabase — keep only last 2 scans per commodity
    try:
        if strike_rows:
            supabase.table("mcx_strike_oi").insert(strike_rows).execute()

            # Delete rows older than last 2 scan timestamps for this commodity
            old_scans = supabase.table("mcx_strike_oi")                 .select("scanned_at")                 .eq("commodity", commodity)                 .order("scanned_at", desc=True)                 .execute()

            timestamps = sorted(set(
                r["scanned_at"] for r in old_scans.data
            ), reverse=True)

            if len(timestamps) > 2:
                cutoff = timestamps[2]
                supabase.table("mcx_strike_oi")                     .delete()                     .eq("commodity", commodity)                     .lte("scanned_at", cutoff)                     .execute()

    except Exception as e:
        logger.warning(f"{commodity} strike OI write error: {e}")

    # Build summary for the ignition signal
    # Sort by oi_delta descending to find most active strikes
    ce_writing_strikes.sort(key=lambda x: x["oi_delta"], reverse=True)
    pe_writing_strikes.sort(key=lambda x: x["oi_delta"], reverse=True)

    # Key resistance = most active CE writing strike above price
    key_resistance = None
    for s in ce_writing_strikes:
        if s["strike"] > current_price:
            key_resistance = s["strike"]
            break

    # Key support = most active PE writing strike below price
    key_support = None
    for s in pe_writing_strikes:
        if s["strike"] < current_price:
            key_support = s["strike"]
            break

    # Rally sustainability check
    # Sustainable rally: CE writing above price (bears defending) + PE buying below (bulls entering)
    # Fake rally: CE writing near ATM (bears not scared) + no real CE buying
    ce_writing_near_atm = any(
        abs(s["strike"] - current_price) / current_price < 0.03
        for s in ce_writing_strikes
    )
    genuine_ce_buying = len(ce_buying_strikes) > 0

    if genuine_ce_buying and not ce_writing_near_atm:
        rally_quality = "genuine"
        rally_note    = "Call buying active — rally has conviction"
    elif ce_writing_near_atm and not genuine_ce_buying:
        rally_quality = "suspect"
        rally_note    = "Call writing near ATM — bears not convinced, rally may fade"
    elif len(pe_buying_strikes) > len(ce_buying_strikes):
        rally_quality = "suspect"
        rally_note    = "More put buying than call buying — bears hedging the rally"
    else:
        rally_quality = "neutral"
        rally_note    = ""

    # Top writing strikes — for display in trade signal
    # Sort by delta descending, take top 4, format as strike labels
    top_ce_writing = sorted(ce_writing_strikes, key=lambda x: x["oi_delta"], reverse=True)[:4]
    top_pe_writing = sorted(pe_writing_strikes, key=lambda x: x["oi_delta"], reverse=True)[:4]
    top_ce_buying  = sorted(ce_buying_strikes,  key=lambda x: x["oi_delta"], reverse=True)[:4]
    top_pe_buying  = sorted(pe_buying_strikes,  key=lambda x: x["oi_delta"], reverse=True)[:4]

    def fmt_strikes(strikes, commodity):
        """Format strike list as readable string e.g. ₹300 · ₹310 · ₹320"""
        formatted = []
        for s in strikes:
            strike = s["strike"]
            if strike >= 1000:
                formatted.append(f"₹{int(strike):,}")
            else:
                formatted.append(f"₹{strike:.0f}")
        return " · ".join(formatted) if formatted else ""

    ce_writing_strikes_str = fmt_strikes(top_ce_writing, commodity)
    pe_writing_strikes_str = fmt_strikes(top_pe_writing, commodity)
    ce_buying_strikes_str  = fmt_strikes(top_ce_buying,  commodity)
    pe_buying_strikes_str  = fmt_strikes(top_pe_buying,  commodity)

    logger.info(
        f"{commodity} strike analysis: "
        f"CE_write={len(ce_writing_strikes)} PE_write={len(pe_writing_strikes)} "
        f"CE_buy={len(ce_buying_strikes)} PE_buy={len(pe_buying_strikes)} "
        f"rally={rally_quality}"
    )

    return {
        "key_resistance":          key_resistance,
        "key_support":             key_support,
        "rally_quality":           rally_quality,
        "rally_note":              rally_note,
        "ce_writing_count":        len(ce_writing_strikes),
        "pe_writing_count":        len(pe_writing_strikes),
        "ce_buying_count":         len(ce_buying_strikes),
        "pe_buying_count":         len(pe_buying_strikes),
        "ce_writing_strikes_str":  ce_writing_strikes_str,
        "pe_writing_strikes_str":  pe_writing_strikes_str,
        "ce_buying_strikes_str":   ce_buying_strikes_str,
        "pe_buying_strikes_str":   pe_buying_strikes_str,
    }
