import logging
from datetime import datetime, date
import pytz
from kiteconnect import KiteConnect
from supabase import create_client
import os

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Commodities to track with their config
MCX_COMMODITIES = {
    "CRUDEOIL": {
        "strike_step": 100,       # strikes go 6100, 6200, 6300...
        "atm_range": 5,           # ATM ± 5 strikes
        "lot_size": 1,
        "tick_size": 1.0,
    },
    "GOLD": {
        "strike_step": 500,       # strikes go 91500, 92000, 92500...
        "atm_range": 5,
        "lot_size": 1,
        "tick_size": 1.0,
    },
    "SILVER": {
        "strike_step": 1000,      # strikes go 95000, 96000, 97000...
        "atm_range": 5,
        "lot_size": 1,
        "tick_size": 1.0,
    },
    "NATURALGAS": {
        "strike_step": 10,        # strikes go 310, 320, 330...
        "atm_range": 5,
        "lot_size": 1,
        "tick_size": 0.1,
    },
}

def get_nearest_expiry_futures_symbol(commodity: str, kite: KiteConnect) -> dict | None:
    """Search for the nearest active futures contract for a commodity."""
    try:
        results = kite.instruments("MCX")
        today = date.today()

        futures = [
            inst for inst in results
            if inst["name"] == commodity
            and inst["instrument_type"] == "FUT"
            and inst["segment"] == "MCX-FUT"
            and inst["expiry"] >= today
        ]

        if not futures:
            logger.warning(f"No active futures found for {commodity}")
            return None

        # Pick nearest expiry
        futures.sort(key=lambda x: x["expiry"])
        nearest = futures[0]

        return {
            "futures_symbol": f"MCX:{nearest['tradingsymbol']}",
            "futures_token": nearest["instrument_token"],
            "expiry_date": nearest["expiry"],
            "lot_size": nearest["lot_size"],
            "tick_size": nearest["tick_size"],
        }

    except Exception as e:
        logger.error(f"Error fetching futures for {commodity}: {e}")
        return None


def get_atm_strike(commodity: str, ltp: float, strike_step: int) -> float:
    """Round LTP to nearest valid strike."""
    return round(ltp / strike_step) * strike_step


def get_option_tokens(
    commodity: str,
    expiry: date,
    atm_strike: float,
    strike_step: int,
    atm_range: int,
    kite: KiteConnect
) -> tuple[list, list]:
    """Fetch instrument tokens for ATM ± atm_range strikes (CE + PE)."""
    try:
        results = kite.instruments("MCX")
        expiry_str = expiry.strftime("%Y-%m-%d")

        strikes_needed = [
            atm_strike + (i * strike_step)
            for i in range(-atm_range, atm_range + 1)
        ]

        symbols = []
        tokens = []

        for inst in results:
            if (
                inst["name"] == commodity
                and inst["instrument_type"] in ("CE", "PE")
                and inst["segment"] == "MCX-OPT"
                and str(inst["expiry"]) == expiry_str
                and inst["strike"] in strikes_needed
            ):
                symbols.append(f"MCX:{inst['tradingsymbol']}")
                tokens.append(inst["instrument_token"])

        logger.info(f"{commodity}: found {len(symbols)} option instruments around ATM {atm_strike}")
        return symbols, tokens

    except Exception as e:
        logger.error(f"Error fetching options for {commodity}: {e}")
        return [], []


def seed_mcx_instruments(kite: KiteConnect, supabase) -> bool:
    """
    Main seed function — called at 9:00 AM IST daily.
    Discovers nearest expiry + ATM tokens for all commodities
    and upserts into mcx_instruments_cache.
    """
    logger.info("Starting MCX instruments seed...")
    success_count = 0

    for commodity, config in MCX_COMMODITIES.items():
        try:
            # Step 1: Get nearest futures contract
            futures_info = get_nearest_expiry_futures_symbol(commodity, kite)
            if not futures_info:
                continue

            # Step 2: Get current LTP for futures to find ATM
            quote = kite.quote([futures_info["futures_symbol"]])
            ltp = quote[futures_info["futures_symbol"]]["last_price"]

            if not ltp:
                logger.warning(f"{commodity}: LTP is 0, skipping ATM calculation")
                continue

            # Step 3: Calculate ATM strike
            atm_strike = get_atm_strike(
                commodity, ltp, config["strike_step"]
            )

            # Step 4: Get option tokens for ATM ± range
            option_symbols, option_tokens = get_option_tokens(
                commodity=commodity,
                expiry=futures_info["expiry_date"],
                atm_strike=atm_strike,
                strike_step=config["strike_step"],
                atm_range=config["atm_range"],
                kite=kite,
            )

            # Step 5: Upsert to Supabase
            row = {
                "commodity": commodity,
                "futures_symbol": futures_info["futures_symbol"],
                "futures_token": futures_info["futures_token"],
                "expiry_date": str(futures_info["expiry_date"]),
                "atm_strike": atm_strike,
                "option_symbols": option_symbols,
                "option_tokens": option_tokens,
                "lot_size": config["lot_size"],
                "tick_size": config["tick_size"],
                "updated_at": datetime.now(IST).isoformat(),
            }

            supabase.table("mcx_instruments_cache").upsert(
                row, on_conflict="commodity"
            ).execute()

            logger.info(
                f"{commodity}: seeded — expiry {futures_info['expiry_date']}, "
                f"ATM {atm_strike}, LTP {ltp}, "
                f"{len(option_symbols)} option tokens cached"
            )
            success_count += 1

        except Exception as e:
            logger.error(f"Failed to seed {commodity}: {e}")
            continue

    logger.info(f"MCX seed complete: {success_count}/{len(MCX_COMMODITIES)} commodities seeded")
    return success_count > 0


def get_cached_instruments(supabase) -> dict:
    """
    Load all cached instruments from Supabase.
    Called by the scanner at each 5-min cycle.
    Returns dict keyed by commodity name.
    """
    try:
        result = supabase.table("mcx_instruments_cache").select("*").execute()
        return {row["commodity"]: row for row in result.data}
    except Exception as e:
        logger.error(f"Failed to load cached instruments: {e}")
        return {}
