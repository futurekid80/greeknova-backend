"""
services/strike_zones.py

Index-based ("how many strikes away from ATM") strike-zone classification,
shared by Jungle/UOA/OI Profile. Manish's Oct 11 2026 ask: let traders pick
ATM / ±1 / ±2 / ±5 / ±10 / All instead of a fixed band baked into each page.

Deliberately NOT percentage-based (the existing otm_pct field) -- a stock
with a wide strike interval (e.g. a ₹100 interval) and one with a narrow
interval (e.g. ₹2.5) both have "ATM +1" mean the same thing to a trader (the
very next listed strike), whereas a flat % cutoff doesn't track that at all
and would include a different number of real strikes for each. Computed
from each symbol's own ACTUAL strikes present in that capture cycle (not a
hardcoded per-symbol interval table), so it works uniformly for indices and
stocks without needing a separate interval lookup anywhere -- and silently
keeps working if an index/stock's interval ever changes.
"""


def build_strike_index_map(rows: list, cmp_map: dict) -> dict:
    """
    rows: iterable of dicts with at least 'symbol' and 'strike' (CE/PE/FUT
    rows all fine to pass in -- FUT rows have no real "strike" relevance but
    are harmless since they just contribute a value to the set that's never
    looked up).
    cmp_map: symbol -> cmp (float).

    Returns {symbol: {"sorted_strikes": [...], "atm_strike": float, "atm_idx": int}}
    for every symbol that has a usable cmp and at least one strike.
    """
    by_symbol: dict = {}
    for r in rows:
        sym = r.get("symbol")
        strike = r.get("strike")
        if sym is None or strike is None:
            continue
        by_symbol.setdefault(sym, set()).add(float(strike))

    out = {}
    for sym, strikes in by_symbol.items():
        cmp = cmp_map.get(sym, 0)
        if not cmp or cmp <= 0 or not strikes:
            continue
        sorted_strikes = sorted(strikes)
        atm_strike = min(sorted_strikes, key=lambda s: abs(s - cmp))
        out[sym] = {
            "sorted_strikes": sorted_strikes,
            "atm_strike": atm_strike,
            "atm_idx": sorted_strikes.index(atm_strike),
        }
    return out


def strikes_away(symbol: str, strike, index_map: dict):
    """
    Signed count of strike-steps between `strike` and that symbol's ATM
    strike for this cycle (0 = ATM itself, +N = N strikes above ATM,
    -N = N strikes below ATM). Returns None if the symbol has no usable
    cmp this cycle, or this exact strike isn't among the strikes seen
    (shouldn't normally happen since `rows` passed to build_strike_index_map
    should be the same rows being classified).
    """
    if strike is None:
        return None
    info = index_map.get(symbol)
    if not info:
        return None
    try:
        idx = info["sorted_strikes"].index(float(strike))
    except ValueError:
        return None
    return idx - info["atm_idx"]


# Selectable zone options the frontend shows as buttons, in display order.
# "all" disables the filter entirely -- every other value is the max
# |strikes_away| to include (so "2" means ATM, ATM±1, and ATM±2).
ZONE_OPTIONS = ["ATM", "1", "2", "5", "10", "all"]


def in_zone(n, zone: str) -> bool:
    """Does a strikes_away value `n` (possibly None) fall inside the
    selected `zone`? None (distance unknown -- e.g. cmp unavailable that
    cycle) is excluded from every specific zone so it doesn't silently leak
    into a filtered view; "all" always includes everything regardless."""
    if not zone or zone == "all":
        return True
    if n is None:
        return False
    if zone == "ATM":
        return n == 0
    try:
        bound = int(zone)
    except (TypeError, ValueError):
        return True
    return abs(n) <= bound
