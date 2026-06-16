"""
oi_walls.py - OI wall computation
Optimized: bulk fetch all symbols in one query, compute walls in Python.
"""
from datetime import datetime
import pytz

_walls_cache = {}
_walls_cache_time = 0.0
_WALLS_TTL = 300  # 5 minutes

def get_all_oi_walls(supabase, cmp_map: dict) -> dict:
    """
    Fetch OI walls for ALL symbols in one query.
    cmp_map: {symbol -> cmp}
    Returns: {symbol -> wall_dict}
    """
    import time as time_module
    global _walls_cache, _walls_cache_time

    if _walls_cache and (time_module.time() - _walls_cache_time) < _WALLS_TTL:
        return _walls_cache

    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    # Use last trading day (skip weekends)
    from datetime import timedelta
    check = today
    for _ in range(5):
        if check.weekday() < 5:
            break
        check -= timedelta(days=1)
    today_str = check.isoformat()

    try:
        # ONE query for all symbols
        rows = supabase.from_("oi_snapshots")\
            .select("symbol, strike, option_type, oi, timestamp")\
            .in_("option_type", ["CE", "PE"])\
            .gte("timestamp", f"{today_str}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(10000).execute()

        if not rows.data:
            return {}

        # Group by symbol, find latest timestamp per symbol
        sym_rows: dict = {}
        sym_latest_ts: dict = {}
        for r in rows.data:
            sym = r["symbol"]
            ts  = r["timestamp"]
            if sym not in sym_latest_ts or ts > sym_latest_ts[sym]:
                sym_latest_ts[sym] = ts
            if sym not in sym_rows:
                sym_rows[sym] = []
            sym_rows[sym].append(r)

        result = {}
        for sym, sym_data in sym_rows.items():
            latest_ts = sym_latest_ts[sym]
            latest = [r for r in sym_data if r["timestamp"] == latest_ts]
            cmp = cmp_map.get(sym, 0)
            wall = _compute_walls(sym, latest, cmp)
            if wall:
                result[sym] = wall

        _walls_cache = result
        _walls_cache_time = time_module.time()
        print(f"[OI_WALLS] Bulk computed {len(result)} symbols")
        return result

    except Exception as e:
        print(f"[OI_WALLS] Bulk fetch failed: {e}")
        return {}


def _compute_walls(symbol: str, rows: list, cmp: float) -> dict:
    ce_oi: dict = {}
    pe_oi: dict = {}
    for r in rows:
        strike = float(r["strike"])
        oi = int(r["oi"] or 0)
        if r["option_type"] == "CE":
            ce_oi[strike] = ce_oi.get(strike, 0) + oi
        elif r["option_type"] == "PE":
            pe_oi[strike] = pe_oi.get(strike, 0) + oi

    if not ce_oi or not pe_oi:
        return {}

    # REPLACE WITH:
    max_ce = max(ce_oi.values(), default=1)
    max_pe = max(pe_oi.values(), default=1)
    ce_sig = {s: v for s, v in ce_oi.items() if v >= max_ce * 0.10} or ce_oi
    pe_sig = {s: v for s, v in pe_oi.items() if v >= max_pe * 0.10} or pe_oi

    ce_wall = max(ce_sig, key=ce_sig.get)
    pe_wall = max(pe_sig, key=pe_sig.get)
    ce_wall_oi_L = round(ce_oi[ce_wall] / 100000, 1)
    pe_wall_oi_L = round(pe_oi[pe_wall] / 100000, 1)
    trade_range = round(abs(ce_wall - pe_wall), 1)
    trade_range_pct = round(trade_range / cmp * 100, 1) if cmp > 0 else 0
    range_label = "Tight" if trade_range_pct < 2 else "Moderate" if trade_range_pct < 5 else "Wide"

    return {
        "ce_wall":         ce_wall,
        "pe_wall":         pe_wall,
        "ce_wall_oi_L":    ce_wall_oi_L,
        "pe_wall_oi_L":    pe_wall_oi_L,
        "trade_range":     trade_range,
        "trade_range_pct": trade_range_pct,
        "range_label":     range_label,
    }


def get_oi_walls(symbol: str, supabase, cmp: float = 0) -> dict:
    """
    Single-symbol fallback — used by other endpoints.
    Falls back to last trading day if no today data (weekends/holidays).
    """
    from datetime import timedelta
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    # Walk back up to 5 days to find last trading day with data
    for days_back in range(0, 5):
        check = today - timedelta(days=days_back)
        if check.weekday() >= 5:
            continue  # skip weekends
        check_str = check.isoformat()
        try:
            rows = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi, timestamp")\
                .eq("symbol", symbol)\
                .in_("option_type", ["CE", "PE"])\
                .gte("timestamp", f"{check_str}T00:00:00+00:00")\
                .lt("timestamp",  f"{check_str}T23:59:59+00:00")\
                .order("timestamp", desc=True)\
                .limit(2000).execute()

            if rows.data:
                latest_ts = rows.data[0].get("timestamp")
                latest = [r for r in rows.data if r.get("timestamp") == latest_ts]
                return _compute_walls(symbol, latest, cmp)
        except Exception as e:
            print(f"[OI_WALLS] {symbol}: {e}")
    return {}
