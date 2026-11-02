"""Nov 2 2026: record each day's War Zone stocks (narrow CPR < 0.3% + an OI signal) the first time they
are seen, so we can later join spot_daily_bars and measure whether War Zone adds anything beyond
narrow CPR alone (range vs previous day, trend efficiency, direction)."""
from datetime import datetime
import pytz


def log_war_zone(supabase) -> dict:
    from api.cpr import get_cpr_scanner
    res = get_cpr_scanner() or {}
    today = datetime.now(pytz.timezone("Asia/Kolkata")).date().isoformat()
    if res.get("trade_date") != today:
        return {"skipped": f"cpr date {res.get('trade_date')} != {today}"}
    rows = []
    for r in res.get("data") or []:
        if r.get("is_index") or not r.get("has_oi_signal"):
            continue
        w = r.get("width_pct")
        if w is None or float(w) >= 0.3:
            continue
        bs = r.get("best_signal") or {}
        rows.append({
            "trade_date": today,
            "symbol": r["symbol"],
            "width_pct": float(w),
            "cpr_position": r.get("cpr_position"),
            "flag_cmp": r.get("last_cmp") or r.get("cmp"),
            "best_signal": (bs.get("signal_type") or bs.get("signal")) if isinstance(bs, dict) else None,
        })
    if rows:
        # first sighting wins: existing (trade_date, symbol) rows are left untouched
        supabase.from_("war_zone_log").upsert(rows, on_conflict="trade_date,symbol", ignore_duplicates=True).execute()
    return {"flagged": len(rows)}
