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
            .limit(10) \
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


@router.get("/signal-accuracy")
def get_signal_accuracy():
    """Aggregate hit-rate per trade_signal type across all graded outcomes."""
    try:
        supabase = get_supabase()
        result = supabase.table("mcx_signal_outcomes").select("*").execute()
        rows = result.data or []

        stats: dict = {}
        for r in rows:
            key = r["trade_signal"]
            stats.setdefault(key, {"count": 0,
                "30": {"correct": 0, "incorrect": 0, "neutral": 0, "checked": 0},
                "60": {"correct": 0, "incorrect": 0, "neutral": 0, "checked": 0},
                "120": {"correct": 0, "incorrect": 0, "neutral": 0, "checked": 0}})
            stats[key]["count"] += 1
            for h in ["30", "60", "120"]:
                outcome = r.get(f"outcome_{h}")
                if r.get(f"checked_{h}") and outcome:
                    stats[key][h]["checked"] += 1
                    if outcome in stats[key][h]:
                        stats[key][h][outcome] += 1

        summary = []
        for signal_type, s in stats.items():
            entry = {"trade_signal": signal_type, "total_fired": s["count"]}
            for h in ["30", "60", "120"]:
                correct, incorrect = s[h]["correct"], s[h]["incorrect"]
                graded = correct + incorrect
                entry[f"checked_{h}min"] = s[h]["checked"]
                entry[f"hit_rate_{h}min"] = round(correct / graded * 100, 1) if graded > 0 else None
            summary.append(entry)

        return {"signals": summary, "raw_count": len(rows)}
    except Exception as e:
        logger.error(f"Signal accuracy fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
