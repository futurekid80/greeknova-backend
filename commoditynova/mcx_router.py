import logging
from fastapi import APIRouter, HTTPException
from utils.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/ignition")
def get_ignition_signals():
    """
    Returns latest Trend Ignition signal for each commodity.
    Frontend polls this every 60 seconds.
    """
    try:
        supabase = get_supabase()
        result = supabase.table("mcx_ignition_signals") \
            .select("*") \
            .order("scanned_at", desc=True) \
            .execute()

        if not result.data:
            return {"signals": [], "message": "No signals yet — seed runs at 9 AM IST"}

        # One row per commodity (table has unique index on commodity)
        return {"signals": result.data, "count": len(result.data)}

    except Exception as e:
        logger.error(f"Error fetching ignition signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ignition/history")
def get_ignition_history(commodity: str = None, limit: int = 50):
    """
    Returns historical fired signals — for signal accuracy tracking.
    Optional filter by commodity.
    """
    try:
        supabase = get_supabase()
        query = supabase.table("mcx_ignition_history") \
            .select("*") \
            .order("fired_at", desc=True) \
            .limit(limit)

        if commodity:
            query = query.eq("commodity", commodity.upper())

        result = query.execute()
        return {"history": result.data, "count": len(result.data)}

    except Exception as e:
        logger.error(f"Error fetching ignition history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/instruments")
def get_cached_instruments():
    """
    Returns current cached instrument tokens — useful for debugging.
    Shows what expiry + ATM the morning seed picked.
    """
    try:
        supabase = get_supabase()
        result = supabase.table("mcx_instruments_cache") \
            .select("commodity, futures_symbol, expiry_date, atm_strike, updated_at") \
            .order("commodity") \
            .execute()

        return {"instruments": result.data, "count": len(result.data)}

    except Exception as e:
        logger.error(f"Error fetching cached instruments: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/seed-now")
def trigger_seed_now():
    """
    Manually trigger the morning instrument seed.
    Useful for first-time setup and testing without waiting for 9 AM.
    """
    try:
        from services.kite_auth import get_kite_client
        from utils.db import get_supabase
        from commoditynova.mcx_instruments import seed_mcx_instruments

        kite = get_kite_client()
        supabase = get_supabase()
        success = seed_mcx_instruments(kite, supabase)

        if success:
            return {"status": "seed completed successfully"}
        else:
            return {"status": "seed completed with errors — check Railway logs"}

    except Exception as e:
        logger.error(f"Manual seed failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scan-now")
def trigger_scan_now():
    """
    Manually trigger one ignition scan cycle.
    Useful for testing outside market hours.
    """
    try:
        from services.kite_auth import get_kite_client
        from utils.db import get_supabase
        from commoditynova.mcx_instruments import get_cached_instruments
        from commoditynova.mcx_historical import fetch_all_candles
        from commoditynova.mcx_ignition_scanner import run_ignition_scan

        kite = get_kite_client()
        supabase = get_supabase()

        instruments = get_cached_instruments(supabase)
        if not instruments:
            return {"status": "error", "message": "No instruments cached — run /mcx/seed-now first"}

        candles_cache            = fetch_all_candles(instruments, kite)
        prev_oi:                 dict = {}
        session_open_price_dict: dict = {}

        run_ignition_scan(kite, supabase, candles_cache, prev_oi,
                          session_open_price_dict=session_open_price_dict)

        return {"status": "scan completed — check /mcx/ignition for results"}

    except Exception as e:
        logger.error(f"Manual scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
