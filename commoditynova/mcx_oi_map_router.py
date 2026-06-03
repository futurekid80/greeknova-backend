import logging
from datetime import date
from fastapi import APIRouter
from utils.db import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/oi-map/{commodity}")
def get_oi_map(commodity: str):
    """
    Returns OI map data for a commodity:
    - strike_oi: per-strike CE/PE OI for the OI map butterfly chart
    - oi_history: per-scan OI change % for the accumulation bars
    - current_price: latest futures price
    - session_summary: cumulative OI, peak, direction
    """
    try:
        supabase = get_supabase()
        today = date.today().isoformat()
        commodity = commodity.upper()

        # 1. Latest signal for price + session summary
        signal_res = supabase.table("mcx_ignition_signals") \
            .select("current_price, cumulative_oi_pct, session_peak_oi_pct, "
                    "oi_unwind_status, futures_oi_direction, cumulative_direction, "
                    "support_strike, resistance_strike, atm_strike") \
            .eq("commodity", commodity) \
            .execute()

        signal = signal_res.data[0] if signal_res.data else {}
        current_price = signal.get("current_price", 0)
        atm_strike    = signal.get("atm_strike", 0)

        # 2. Strike OI from mcx_strike_oi — latest scan only
        strike_res = supabase.table("mcx_strike_oi") \
            .select("strike, option_type, current_oi, oi_delta, moneyness, activity") \
            .eq("commodity", commodity) \
            .order("scanned_at", desc=True) \
            .limit(84) \
            .execute()

        # Group by strike — keep latest scan per strike
        strikes_seen = set()
        strike_oi = []
        for row in strike_res.data:
            key = f"{row['strike']}_{row['option_type']}"
            if key not in strikes_seen:
                strikes_seen.add(key)
                strike_oi.append(row)

        # Build butterfly structure: {strike: {ce_oi, pe_oi, ce_delta, pe_delta}}
        strike_map = {}
        for row in strike_oi:
            s = float(row["strike"])
            if s not in strike_map:
                strike_map[s] = {"strike": s, "ce_oi": 0, "pe_oi": 0,
                                  "ce_delta": 0, "pe_delta": 0}
            if row["option_type"] == "CE":
                strike_map[s]["ce_oi"]    = row["current_oi"]
                strike_map[s]["ce_delta"] = row["oi_delta"]
            else:
                strike_map[s]["pe_oi"]    = row["current_oi"]
                strike_map[s]["pe_delta"] = row["oi_delta"]

        # Sort strikes descending (highest at top)
        sorted_strikes = sorted(strike_map.values(), key=lambda x: x["strike"], reverse=True)

        # 3. OI history for today — accumulation bars
        history_res = supabase.table("mcx_oi_history") \
            .select("scanned_at, oi_change_pct, cumulative_oi_pct, current_oi") \
            .eq("commodity", commodity) \
            .eq("session_date", today) \
            .order("scanned_at", desc=False) \
            .limit(100) \
            .execute()

        return {
            "commodity":      commodity,
            "current_price":  current_price,
            "atm_strike":     atm_strike,
            "session_summary": {
                "cumulative_oi_pct":  signal.get("cumulative_oi_pct", 0),
                "session_peak_oi_pct": signal.get("session_peak_oi_pct", 0),
                "oi_unwind_status":   signal.get("oi_unwind_status", "building"),
                "futures_oi_direction": signal.get("futures_oi_direction", "neutral"),
                "support_strike":     signal.get("support_strike", 0),
                "resistance_strike":  signal.get("resistance_strike", 0),
            },
            "strike_oi":   sorted_strikes,
            "oi_history":  history_res.data,
        }

    except Exception as e:
        logger.error(f"OI map error for {commodity}: {e}")
        return {"error": str(e)}
