"""Nov 5 2026: "Biggest OI added today" for the UOA page.

Plain ranking of where the most MONEY was added to option open interest since the previous
session's close: (OI added) x (current premium). No premium-quiet condition, no tiers -- it is a
different question from Option Strike Stealth ("OI building while the price stays calm"). Built after a
strong trend day (NIFTY 24300-24400 PE: OI up thousands of % with premiums down 60-85%) showed
nothing in Stealth, Jungle or UOA.

Guards reused from Stealth's EOD window: min starting OI, a money floor, and (against a prior-session
baseline) at least 40% of the claimed OI addition must have happened TODAY."""
from datetime import datetime, timezone

from services.oi_diff import fetch_oi_diff_window
from services.strike_zones import build_strike_index_map, strikes_away, in_zone
from services.option_stealth_buildup import _ist_day_start_utc, _first_snapshot_oi

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
ZONE = "10"
MIN_OLD_OI = 10000
MIN_MONEY = 2_500_000      # Rs 25 lakh
TOP_INDEX = 5
TOP_STOCKS = 10


def get_oi_added_today(supabase, date: str = None) -> dict:
    new_data, old_map, cmp_map, ts_new, ts_old, _ts = fetch_oi_diff_window(supabase, date, lookback_minutes=None)
    if not new_data or ts_new is None:
        return {"index": [], "stocks": [], "ts_new": None}

    index_map = build_strike_index_map(new_data, cmp_map)
    cands = []
    for row in new_data:
        opt = row.get("option_type")
        if opt not in ("CE", "PE") or row.get("strike") is None:
            continue
        sym = row.get("symbol")
        if sym not in index_map:
            continue
        sa = strikes_away(sym, row["strike"], index_map)
        if not in_zone(sa, ZONE):
            continue
        orow = old_map.get(f"{sym}_{row['tradingsymbol']}")
        if not orow:
            continue
        old_oi = orow.get("oi") or 0
        new_oi = row.get("oi") or 0
        price = row.get("last_price") or 0
        if old_oi < MIN_OLD_OI or new_oi <= old_oi or price <= 0:
            continue
        added = new_oi - old_oi
        money = added * price
        if money < MIN_MONEY:
            continue
        cands.append({
            "symbol": sym, "tradingsymbol": row["tradingsymbol"], "strike": row["strike"],
            "option_type": opt, "expiry": row.get("expiry"), "strikes_away": sa,
            "cmp": cmp_map.get(sym), "old_oi": old_oi, "new_oi": new_oi, "oi_added": added,
            "oi_pct": round(added / old_oi * 100, 1), "premium": price, "money": round(money),
        })

    cands.sort(key=lambda c: -c["money"])
    cands = cands[:80]  # enough to fill both lists after the stale-open check

    # Against a prior-session baseline, drop jumps that were already there at today's open
    # (exchange overnight OI updates on thin strikes).
    try:
        day_start_utc, _ = _ist_day_start_utc(ts_new)
        old_dt = datetime.fromisoformat(str(ts_old).replace("Z", "+00:00")) if ts_old else None
        prior = bool(old_dt and old_dt < day_start_utc)
        if prior and cands:
            open_oi = _first_snapshot_oi(supabase, sorted({c["tradingsymbol"] for c in cands}), day_start_utc.isoformat(), ts_new)
            kept = []
            for c in cands:
                o = open_oi.get(c["tradingsymbol"])
                if o is not None and (c["new_oi"] - o) < 0.4 * c["oi_added"]:
                    continue
                kept.append(c)
            cands = kept
    except Exception as e:
        print(f"[OIAddedToday] open-OI check failed (kept all): {e}")

    index = [c for c in cands if c["symbol"] in INDICES][:TOP_INDEX]
    stocks = [c for c in cands if c["symbol"] not in INDICES][:TOP_STOCKS]
    return {"ts_new": ts_new, "ts_old": ts_old, "index": index, "stocks": stocks}
