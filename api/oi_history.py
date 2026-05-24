from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import time as time_module

# ── Per-symbol cache ──────────────────────────────────────────────────────────
_dates_cache: dict = {}
_dates_cache_time: dict = {}
_history_cache: dict = {}
_history_cache_time: dict = {}
CACHE_TTL = 300  # 5 minutes


def get_available_dates(symbol: str = "NIFTY"):
    """Get available trading dates — ONE query instead of 30"""
    global _dates_cache, _dates_cache_time

    cache_key = symbol
    if cache_key in _dates_cache and (time_module.time() - _dates_cache_time.get(cache_key, 0)) < CACHE_TTL:
        return _dates_cache[cache_key]

    supabase = get_supabase()
    base = datetime.now(timezone.utc).date()
    since = (base - timedelta(days=30)).isoformat()

    # Single query — get all timestamps in last 30 days
# Get one timestamp per day — much faster and avoids limit issues
    dates = set()
    check = base
    for _ in range(30):
        d = check.isoformat()
        r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp", f"{d}T23:59:59+00:00")\
            .limit(1).execute()
        if r.data:
            dates.add(d)
        check -= timedelta(days=1)

    result = {"symbol": symbol, "dates": sorted(dates)}
    _dates_cache[cache_key] = result
    _dates_cache_time[cache_key] = time_module.time()
    return result

    # Extract unique dates from timestamps
    dates = set()
    for row in (r.data or []):
        ts = row["timestamp"]
        # Convert UTC timestamp to IST date
        try:
            dt = datetime.fromisoformat(ts.replace('+00:00', '').replace('Z', ''))
            ist_dt = dt + timedelta(hours=5, minutes=30)
            dates.add(ist_dt.date().isoformat())
        except:
            dates.add(ts[:10])

    result = {"symbol": symbol, "dates": sorted(dates)}
    _dates_cache[cache_key] = result
    _dates_cache_time[cache_key] = time_module.time()
    return result


def get_eod_snapshot(symbol: str, date: str, expiry: str = None):
    """Get EOD OI snapshot — paginated"""
    supabase = get_supabase()

    # Get EOD timestamp
    ts_q = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date}T00:00:00+00:00")\
        .lt("timestamp", f"{date}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    if not ts_q.data:
        return {}

    latest_ts = ts_q.data[0]["timestamp"]

    all_rows = []
    for offset in range(0, 10000, 1000):
        q = supabase.from_("oi_snapshots")\
            .select("strike, option_type, oi, expiry")\
            .eq("symbol", symbol)\
            .eq("timestamp", latest_ts)\
            .range(offset, offset + 999)
        if expiry:
            q = q.eq("expiry", expiry)
        batch = q.execute()
        if not batch.data:
            break
        all_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    result = {}
    for r in all_rows:
        key = (r["strike"], r["option_type"])
        result[key] = r["oi"] or 0

    return result


def get_cmp(symbol: str) -> float | None:
    try:
        from services.kite_auth import get_kite_client
        INDEX_NSE_MAP = {
            "NIFTY":     "NSE:NIFTY 50",
            "BANKNIFTY": "NSE:NIFTY BANK",
            "FINNIFTY":  "NSE:NIFTY FIN SERVICE",
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
    if not cmp or not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - cmp))


def get_oi_comparison(symbol: str = "NIFTY", date_a: str = None, date_b: str = None, expiry: str = None):
    global _history_cache, _history_cache_time

    cache_key = f"{symbol}_{date_a}_{date_b}_{expiry}"
    if cache_key in _history_cache and (time_module.time() - _history_cache_time.get(cache_key, 0)) < CACHE_TTL:
        return _history_cache[cache_key]

    supabase = get_supabase()

    dates_result = get_available_dates(symbol)
    dates = dates_result["dates"]

    if not dates:
        return {"symbol": symbol, "dates": [], "rows": []}

    if not date_a:
        date_a = dates[-1] if len(dates) >= 1 else None
    if not date_b:
        date_b = dates[-2] if len(dates) >= 2 else dates[-1]

    # Get expiries from date_a snapshot
    exp_q = supabase.from_("oi_snapshots")\
        .select("expiry")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date_a}T00:00:00+00:00")\
        .lt("timestamp", f"{date_a}T23:59:59+00:00")\
        .limit(500).execute()

    today_str = date_type.today().isoformat()
    expiries = sorted(set(
        r["expiry"] for r in (exp_q.data or [])
        if r["expiry"] and r["expiry"] >= today_str
    ))

    active_expiry = expiry or (expiries[0] if expiries else None)

    # Get snapshots in parallel-ish — both dates
    snap_a = get_eod_snapshot(symbol, date_a, active_expiry)
    snap_b = get_eod_snapshot(symbol, date_b, active_expiry)

    all_strikes = sorted(set(k[0] for k in list(snap_a.keys()) + list(snap_b.keys())))

    rows = []
    total_ce_buildup = 0
    total_ce_unwind  = 0
    total_pe_buildup = 0
    total_pe_unwind  = 0

    for strike in all_strikes:
        ce_a = snap_a.get((strike, "CE"), 0)
        ce_b = snap_b.get((strike, "CE"), 0)
        pe_a = snap_a.get((strike, "PE"), 0)
        pe_b = snap_b.get((strike, "PE"), 0)
        ce_chg = ce_a - ce_b
        pe_chg = pe_a - pe_b

        if ce_chg > 0: total_ce_buildup += ce_chg
        else:          total_ce_unwind  += abs(ce_chg)
        if pe_chg > 0: total_pe_buildup += pe_chg
        else:          total_pe_unwind  += abs(pe_chg)

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

    if total_ce_buildup > total_pe_buildup and total_pe_unwind > total_ce_unwind:
        structure = "BEARISH"
        structure_desc = "More CE buildup + PE unwinding → resistance growing, support easing"
    elif total_pe_buildup > total_ce_buildup and total_ce_unwind > total_pe_unwind:
        structure = "BULLISH"
        structure_desc = "More PE buildup + CE unwinding → support growing, resistance easing"
    elif total_ce_buildup > total_pe_buildup:
        structure = "BEARISH_BIAS"
        structure_desc = "CE buildup dominant → resistance being built"
    elif total_pe_buildup > total_ce_buildup:
        structure = "BULLISH_BIAS"
        structure_desc = "PE buildup dominant → support being built"
    else:
        structure = "NEUTRAL"
        structure_desc = "Balanced OI changes — no clear directional bias"

    cmp        = get_cmp(symbol)
    atm_strike = get_atm_strike(cmp, all_strikes) if cmp else None

    result = {
        "symbol":           symbol,
        "date_a":           date_a,
        "date_b":           date_b,
        "expiry":           active_expiry,
        "expiries":         expiries,
        "dates":            dates,
        "cmp":              cmp,
        "atm_strike":       atm_strike,
        "total_ce_buildup": total_ce_buildup,
        "total_ce_unwind":  total_ce_unwind,
        "total_pe_buildup": total_pe_buildup,
        "total_pe_unwind":  total_pe_unwind,
        "structure":        structure,
        "structure_desc":   structure_desc,
        "rows":             rows,
    }

    _history_cache[cache_key] = result
    _history_cache_time[cache_key] = time_module.time()

    return result
