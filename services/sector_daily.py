"""Nightly sector snapshot (Nov 20 2026).

One row per (trade_date, sector): how many of the sector's F&O stocks showed Elite/Strong Option Strike Stealth
activity that day, how many leaned bullish / bearish, and the sector's average cash price move. Nothing here is
shown to members yet. It builds a history so a 'sector heat' reading (activity unusually high for a sector that
has not moved yet) can be checked against what happened next before anything is shown.

Bullish lean = Call Buying or Put Writing at Elite/Strong tier; bearish lean = Put Buying or Call Writing.
"""
from collections import defaultdict

from services.sector_map import SECTOR_MAP, SYMBOL_SECTOR


def _paged(query_fn):
    rows, lo = [], 0
    while True:
        res = query_fn(lo, lo + 999).execute().data or []
        rows += res
        if len(res) < 1000:
            return rows
        lo += 1000


def compute_sector_rows(events, prices, trade_date):
    """events: option_stealth_events rows for the day; prices: daily_oi_summary rows (symbol, price_chg_pct).
    Pure function: returns the rows to store."""
    strong, elite, bull, bear = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    for e in events:
        sym = e.get("symbol")
        sec = SYMBOL_SECTOR.get(sym)
        tier = e.get("best_tier")
        if not sec or tier not in ("ELITE", "STRONG"):
            continue
        strong[sec].add(sym)
        if tier == "ELITE":
            elite[sec].add(sym)
        ot, act = e.get("option_type"), e.get("activity_label")
        if (ot == "CE" and act == "Call Buying") or (ot == "PE" and act == "Put Writing"):
            bull[sec].add(sym)
        if (ot == "PE" and act == "Put Buying") or (ot == "CE" and act == "Call Writing"):
            bear[sec].add(sym)
    px = defaultdict(list)
    for p in prices:
        sec = SYMBOL_SECTOR.get(p.get("symbol"))
        if sec and p.get("price_chg_pct") is not None:
            px[sec].append(float(p["price_chg_pct"]))
    out = []
    for sec, syms in SECTOR_MAP.items():
        vals = px.get(sec, [])
        out.append({
            "trade_date": trade_date, "sector": sec, "total_stocks": len(syms),
            "strong_stocks": len(strong[sec]), "elite_stocks": len(elite[sec]),
            "bull_stocks": len(bull[sec]), "bear_stocks": len(bear[sec]),
            "avg_price_chg_pct": round(sum(vals) / len(vals), 2) if vals else None,
            "advancers": sum(1 for v in vals if v > 0), "priced_stocks": len(vals),
        })
    return out


def run_sector_daily(supabase, trade_date):
    ev = _paged(lambda a, b: supabase.from_("option_stealth_events")
                .select("symbol,option_type,activity_label,best_tier").eq("scan_date", trade_date).range(a, b))
    pr = _paged(lambda a, b: supabase.from_("daily_oi_summary")
                .select("symbol,price_chg_pct").eq("trade_date", trade_date).range(a, b))
    if not ev and not pr:
        print(f"[SectorDaily] {trade_date}: no events or prices; nothing written")
        return 0
    rows = compute_sector_rows(ev, pr, trade_date)
    supabase.from_("sector_daily").upsert(rows, on_conflict="trade_date,sector").execute()
    print(f"[SectorDaily] {trade_date}: wrote {len(rows)} sector rows ({len(ev)} events, {len(pr)} price rows)")
    return len(rows)
