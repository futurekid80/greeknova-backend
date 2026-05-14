from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type


def get_stock_oi(symbol: str):
    supabase = get_supabase()

    # Get latest market-hours timestamp for this symbol
    latest = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()

    if not latest.data:
        return {"symbol": symbol, "strikes": [], "cmp": 0}

    ts = latest.data[0]["timestamp"]

    # Fetch all data for this timestamp
    data = supabase.from_("oi_snapshots")\
        .select("*")\
        .eq("symbol", symbol)\
        .eq("timestamp", ts)\
        .order("strike", desc=False)\
        .limit(5000)\
        .execute()

    if not data.data:
        return {"symbol": symbol, "strikes": [], "cmp": 0}

    # Get CMP
    cmp_data = supabase.from_("cmp_prices")\
        .select("cmp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()
    cmp = float(cmp_data.data[0]["cmp"]) if cmp_data.data else 0

    # ── Find nearest expiry ───────────────────────────────────────────────────
    today_str = date_type.today().isoformat()
    all_expiries = sorted(set(
        r["expiry"] for r in data.data
        if r["expiry"] and r["expiry"] >= today_str
    ))
    nearest_expiry = all_expiries[0] if all_expiries else None

    # Filter to nearest expiry only for display
    if nearest_expiry:
        display_rows = [r for r in data.data if r["expiry"] == nearest_expiry]
    else:
        display_rows = data.data

    ce_rows = [r for r in display_rows if r["option_type"] == "CE"]
    pe_rows = [r for r in display_rows if r["option_type"] == "PE"]
    strikes = sorted(set(r["strike"] for r in display_rows))

    # Build strike data for display (all strikes)
    strike_data = []
    for strike in strikes:
        ce = next((r for r in ce_rows if r["strike"] == strike), None)
        pe = next((r for r in pe_rows if r["strike"] == strike), None)
        strike_data.append({
            "strike":    strike,
            "ce_oi":     ce["oi"] if ce else 0,
            "pe_oi":     pe["oi"] if pe else 0,
            "ce_ltp":    ce["last_price"] if ce else 0,
            "pe_ltp":    pe["last_price"] if pe else 0,
            "ce_volume": ce["volume"] if ce else 0,
            "pe_volume": pe["volume"] if pe else 0,
            "is_atm":    abs(strike - cmp) == min(abs(s - cmp) for s in strikes) if cmp > 0 else False,
        })

    # ── PCR: use ATM ±10 strikes only (matches Sensibull methodology) ─────────
    if cmp > 0 and strikes:
        atm_strike = min(strikes, key=lambda s: abs(s - cmp))
        atm_idx = strikes.index(atm_strike)
        pcr_strike_set = set(strikes[max(0, atm_idx - 10):atm_idx + 11])
        total_ce = sum(r["ce_oi"] for r in strike_data if r["strike"] in pcr_strike_set)
        total_pe = sum(r["pe_oi"] for r in strike_data if r["strike"] in pcr_strike_set)
    else:
        total_ce = sum(r["ce_oi"] for r in strike_data)
        total_pe = sum(r["pe_oi"] for r in strike_data)

    pcr = round(total_pe / total_ce, 3) if total_ce > 0 else 0

    # IV calculations
    try:
        from api.iv_calc import add_iv_to_strikes
        expiry_str = nearest_expiry or (data.data[0]["expiry"] if data.data else None)
        if expiry_str and cmp > 0:
            strike_data = add_iv_to_strikes(strike_data, cmp, expiry_str)
    except Exception as e:
        print(f"IV calc error: {e}")

    return {
        "symbol":       symbol,
        "timestamp":    ts,
        "cmp":          cmp,
        "pcr":          pcr,
        "expiry":       nearest_expiry,
        "all_expiries": all_expiries,
        "total_ce_oi":  total_ce,
        "total_pe_oi":  total_pe,
        "strikes":      strike_data,
    }
