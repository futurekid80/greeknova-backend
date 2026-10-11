"""
services/custom_scanner.py

Manish's long-standing ask (asked Opstra about this for over a year,
never found elsewhere): let a trader define their OWN multi-condition
scanner instead of a fixed signal vocabulary -- e.g. "ATM Call unwinding
AND ATM Put addition AND/OR next-2-OTM Call unwinding". Oct 11 2026 scope
decisions (Manish's explicit calls):
  - Up to 2-3 conditions, combined strictly LEFT TO RIGHT with AND/OR
    (no parentheses/precedence) -- matches how he phrased the example in
    plain English.
  - Scans the WHOLE F&O universe each run and returns which symbols
    currently match -- not a single-symbol lookup.
  - Manual scanner only for now. No push-alert wiring -- prove the
    matching logic live first, add alerting once it's trusted.

Reuses the same "strikes away from ATM" concept as strike_zones.py (the
ATM/±1/±2/±5/±10 selector), just with a direction-aware OTM/ITM framing
layered on top: for a CALL, OTM is ABOVE the ATM strike; for a PUT, OTM is
BELOW. "ATM Call unwinding" and "next-2-OTM Put writing" are both
expressible as one condition each.
"""
from utils.db import get_supabase
from services.oi_diff import fetch_latest_oi_diff
from services.strike_zones import build_strike_index_map, strikes_away

MIN_OI_PCT = 5.0    # ignore noise below this -- same spirit as Jungle's OI threshold
MIN_OLD_OI = 1000   # same guard Jungle/UOA use before trusting a % change

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}

STRIKE_SELECT_OPTIONS = ["ATM", "OTM1", "OTM2", "OTM5", "ITM1", "ITM2", "ITM5", "ANY"]


def _zone_matches(sa, option_type: str, strike_select: str) -> bool:
    """Does a strikes_away value `sa` fall in the requested zone? Which
    side of the chain counts as OTM depends on option_type: OTM for a
    CALL is strikes ABOVE the ATM strike (positive sa); OTM for a PUT is
    BELOW (negative sa). ITM is the mirror image of whichever OTM is."""
    if sa is None:
        return False
    if strike_select == "ATM":
        return sa == 0
    if strike_select == "ANY":
        return True

    is_otm_side = strike_select.startswith("OTM")
    try:
        n = int(strike_select[3:])
    except (ValueError, IndexError):
        return False

    if option_type == "CE":
        otm_range, itm_range = range(1, n + 1), range(-n, 0)
    else:
        otm_range, itm_range = range(-n, 0), range(1, n + 1)

    return sa in (otm_range if is_otm_side else itm_range)


def _condition_match(condition: dict, rows_by_strike: dict, index_map: dict, symbol: str):
    """rows_by_strike: {(strike, option_type): {old_oi, new_oi, oi_change,
    oi_pct, last_price}}. Returns the first matching strike's detail dict
    if ANY strike in the requested zone+side satisfies the condition, else
    None. "Any strike in the zone" (not "every strike in the zone") --
    matches a trader's intuition of "is there unwinding happening in that
    area", and the match detail surfaces exactly which strike triggered
    it rather than hiding the reasoning."""
    opt_type = condition["option_type"]
    strike_select = condition["strike_select"]
    direction = condition["direction"]  # "BUILD" or "UNWIND"

    for (strike, ot), row in rows_by_strike.items():
        if ot != opt_type:
            continue
        sa = strikes_away(symbol, strike, index_map)
        if not _zone_matches(sa, opt_type, strike_select):
            continue
        if row["old_oi"] < MIN_OLD_OI or abs(row["oi_pct"]) < MIN_OI_PCT:
            continue
        if direction == "BUILD" and row["oi_change"] <= 0:
            continue
        if direction == "UNWIND" and row["oi_change"] >= 0:
            continue
        return {**row, "strike": strike, "option_type": ot, "strikes_away": sa}
    return None


def run_custom_scan(conditions: list, date: str = None) -> dict:
    """conditions: list of 2-3 dicts, each:
        {"strike_select": one of STRIKE_SELECT_OPTIONS,
         "option_type": "CE"|"PE",
         "direction": "BUILD"|"UNWIND",
         "link": "AND"|"OR"}   (link is ignored on the first condition)
    Evaluated strictly left-to-right -- "A and B or C" reads as
    ((A and B) or C) in program terms, but since there's no parentheses
    UI, that's also exactly how a trader reading it left-to-right would
    expect it to behave.
    """
    if not conditions or len(conditions) < 1:
        return {"error": "At least one condition is required", "matches": []}
    if len(conditions) > 3:
        return {"error": "Up to 3 conditions supported", "matches": []}

    supabase = get_supabase()
    new_data, old_map, cmp_map, ts_new, ts_old, timestamps = fetch_latest_oi_diff(supabase, date)

    if not new_data or ts_new is None:
        return {"error": "Need at least 2 snapshots", "matches": [], "total": 0}

    # Per-symbol {(strike, option_type): oi-change detail}, same shape
    # Jungle/UOA build, but unfiltered by any threshold at this stage --
    # the per-condition thresholds (MIN_OI_PCT etc.) apply inside
    # _condition_match instead.
    by_symbol: dict = {}
    for row in new_data:
        key = f"{row['symbol']}_{row['tradingsymbol']}"
        old_row = old_map.get(key)
        if not old_row:
            continue
        opt_type = row["option_type"]
        if opt_type not in ("CE", "PE"):
            continue
        sym = row["symbol"]
        old_oi = old_row["oi"] or 0
        new_oi = row["oi"] or 0
        oi_change = new_oi - old_oi
        oi_pct = round((oi_change / old_oi * 100), 2) if old_oi > 1000 else 0
        by_symbol.setdefault(sym, {})[(row["strike"], opt_type)] = {
            "old_oi": old_oi, "new_oi": new_oi,
            "oi_change": oi_change, "oi_pct": oi_pct,
            "last_price": row.get("last_price"),
        }

    index_map = build_strike_index_map(new_data, cmp_map)

    matches = []
    for symbol, rows_by_strike in by_symbol.items():
        if symbol not in index_map:
            continue  # no usable cmp this cycle -- can't place strikes relative to ATM

        overall = None
        detail = []
        for i, cond in enumerate(conditions):
            hit = _condition_match(cond, rows_by_strike, index_map, symbol)
            if hit:
                detail.append({
                    "condition_index": i,
                    "strike": hit["strike"], "option_type": hit["option_type"],
                    "strikes_away": hit["strikes_away"],
                    "oi_change": hit["oi_change"], "oi_pct": hit["oi_pct"],
                })
            cond_true = hit is not None
            if i == 0:
                overall = cond_true
            else:
                link = cond.get("link", "AND")
                overall = (overall or cond_true) if link == "OR" else (overall and cond_true)

        if overall:
            matches.append({
                "symbol": symbol,
                "cmp": cmp_map.get(symbol),
                "atm_strike": index_map[symbol]["atm_strike"],
                "is_index": symbol in INDEX_SYMBOLS,
                "matched_conditions": detail,
            })

    matches.sort(key=lambda m: (len(m["matched_conditions"]), m["symbol"]), reverse=True)

    return {
        "date": (date or (ts_new[:10] if ts_new else None)),
        "ts_new": ts_new,
        "ts_old": ts_old,
        "conditions": conditions,
        "total": len(matches),
        "matches": matches,
    }
