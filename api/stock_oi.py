from utils.db import get_supabase
from datetime import datetime, timezone

def get_stock_oi(symbol: str):
    supabase = get_supabase()

    # Get latest timestamp for this symbol
    latest = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()

    if not latest.data:
        return {"symbol": symbol, "strikes": [], "cmp": 0}

    ts = latest.data[0]["timestamp"]

    data = supabase.from_("oi_snapshots")\
        .select("*")\
        .eq("symbol", symbol)\
        .eq("timestamp", ts)\
        .order("strike", desc=False)\
        .execute()

    cmp_data = supabase.from_("cmp_prices")\
        .select("cmp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()

    cmp = cmp_data.data[0]["cmp"] if cmp_data.data else 0

    ce_rows = [r for r in data.data if r["option_type"] == "CE"]
    pe_rows = [r for r in data.data if r["option_type"] == "PE"]

    strikes = sorted(set(r["strike"] for r in data.data))
    strike_data = []
    for strike in strikes:
        ce = next((r for r in ce_rows if r["strike"] == strike), None)
        pe = next((r for r in pe_rows if r["strike"] == strike), None)
        strike_data.append({
            "strike": strike,
            "ce_oi": ce["oi"] if ce else 0,
            "pe_oi": pe["oi"] if pe else 0,
            "ce_ltp": ce["last_price"] if ce else 0,
            "pe_ltp": pe["last_price"] if pe else 0,
            "ce_volume": ce["volume"] if ce else 0,
            "pe_volume": pe["volume"] if pe else 0,
            "is_atm": abs(strike - cmp) == min(abs(s - cmp) for s in strikes) if cmp > 0 else False,
        })

    total_ce = sum(r["ce_oi"] for r in strike_data)
    total_pe = sum(r["pe_oi"] for r in strike_data)
    pcr = round(total_pe / total_ce, 3) if total_ce > 0 else 0

    # Add IV calculations
    try:
        from api.iv_calc import add_iv_to_strikes
        expiry_str = data.data[0]["expiry"] if data.data else None
        if expiry_str and float(cmp) > 0:
            strike_data = add_iv_to_strikes(strike_data, float(cmp), expiry_str)
    except Exception as e:
        print(f"IV calc error: {e}")

    return {
        "symbol": symbol,
        "timestamp": ts,
        "cmp": float(cmp),
        "pcr": pcr,
        "total_ce_oi": total_ce,
        "total_pe_oi": total_pe,
        "strikes": strike_data,
    }
