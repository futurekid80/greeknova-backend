from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type


def get_available_dates(symbol: str = "NIFTY"):
    supabase = get_supabase()
    result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=True)\
        .limit(5000)\
        .execute()
    all_dates = set()
    for r in (result.data or []):
        all_dates.add(r["timestamp"][:10])
    return {"symbol": symbol, "dates": sorted(all_dates)}


def get_eod_snapshot(symbol: str, date: str, expiry: str = None):
    """Get the last snapshot of the day for a given date"""
    supabase = get_supabase()
    ts_q = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date}T00:00:00+00:00")\
        .lt("timestamp", f"{date}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()

    if not ts_q.data:
        return {}

    latest_ts = ts_q.data[0]["timestamp"]
    q = supabase.from_("oi_snapshots")\
        .select("strike, option_type, oi, expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", latest_ts)

    if expiry:
        q = q.eq("expiry", expiry)

    rows = q.execute().data
    result = {}
    for r in rows:
        key = (r["strike"], r["option_type"])
        result[key] = r["oi"] or 0
    return result


def get_cmp(symbol: str) -> float | None:
    """Fetch current market price from Kite"""
    try:
        from services.kite_auth import get_kite_client
        INDEX_NSE_MAP = {
            "NIFTY": "NSE:NIFTY 50",
            "BANKNIFTY": "NSE:NIFTY BANK",
            "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        }
        nse_symbol = INDEX_NSE_MAP.get(symbol, f"NSE:{symbol}")
        kite = get_kite_client()
        quotes = kite.quote([nse_symbol])
        if nse_symbol in quotes:
            return quotes[nse_symbol]["last_price"]
    except Exception as e:
        print(f"CMP fetch failed for {symbol}: {e}")
    return None


def get_atm_strike(cmp: float, strikes: list) -> float | None:
    """Find the closest strike to CMP"""
    if not cmp or not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - cmp))


def get_oi_comparison(symbol: str = "NIFTY", date_a: str = None, date_b: str = None, expiry: str = None):
    supabase = get_supabase()

    dates_result = get_available_dates(symbol)
    dates = dates_result["dates"]

    if not dates:
        return {"symbol": symbol, "dates": [], "rows": []}

    if not date_a:
        date_a = dates[-1] if len(dates) >= 1 else None
    if not date_b:
        date_b = dates[-2] if len(dates) >= 2 else dates[-1]

    exp_q = supabase.from_("oi_snapshots")\
        .select("expiry")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date_a}T00:00:00+00:00")\
        .lt("timestamp", f"{date_a}T23:59:59+00:00")\
        .execute()

    today_str = date_type.today().isoformat()
    expiries = sorted(set(
        r["expiry"] for r in exp_q.data
        if r["expiry"] and r["expiry"] >= today_str
    )) if exp_q.data else []
    active_expiry = expiry or (expiries[0] if expiries else None)

    snap_a = get_eod_snapshot(symbol, date_a, active_expiry)
    snap_b = get_eod_snapshot(symbol, date_b, active_expiry)

    all_strikes = sorted(set(k[0] for k in list(snap_a.keys()) + list(snap_b.keys())))

    rows = []
    for strike in all_strikes:
        ce_a = snap_a.get((strike, "CE"), 0)
        ce_b = snap_b.get((strike, "CE"), 0)
        pe_a = snap_a.get((strike, "PE"), 0)
        pe_b = snap_b.get((strike, "PE"), 0)
        ce_chg = ce_a - ce_b
        pe_chg = pe_a - pe_b
        rows.append({
            "strike":  strike,
            "ce_a":    ce_a,
            "ce_b":    ce_b,
            "ce_chg":  ce_chg,
            "pe_a":    pe_a,
            "pe_b":    pe_b,
            "pe_chg":  pe_chg,
            "net_chg": pe_chg - ce_chg,
        })

    cmp = get_cmp(symbol)
    atm_strike = get_atm_strike(cmp, all_strikes) if cmp else None

    return {
        "symbol":     symbol,
        "date_a":     date_a,
        "date_b":     date_b,
        "expiry":     active_expiry,
        "expiries":   expiries,
        "dates":      dates,
        "cmp":        cmp,
        "atm_strike": atm_strike,
        "rows":       rows,
    }
