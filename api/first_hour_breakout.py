"""
first_hour_breakout.py - First Hour Range Breakout Scanner

Tracks each stock's opening-hour range (9:15-10:15 AM IST) using the same
5-min cmp_prices captures already running platform-wide, then watches
whether price crosses above that range's HIGH at any point afterward.

Only stocks that have genuinely broken out are surfaced -- this is
deliberately a different population from the Spot Volume Breakout Scanner
(that one requires a 2x+ volume spike after a quiet period; this one is
purely about the classic opening-range breakout, regardless of volume
shape). A stock here has two possible states:

  sustaining -- broke the first-hour high, and has not closed back below
                the first-hour LOW since
  failed     -- broke the first-hour high, but has since closed back
                below the first-hour LOW -- a trapped/failed breakout,
                arguably the more important of the two to flag clearly

Cross-references near-ATM Put Writing (via api.uoa.get_uoa) and FUT Long
Buildup (via the already-fixed nearest-expiry FUT OI helper in
api.oi_pulse) as confirmation signals -- same classification logic
already powering the Alerts feed and Intraday Futures Scanner, so a
stock flagged as "confirmed" here means the same thing it means
everywhere else on the platform.
"""
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

FIRST_HOUR_START = 9 * 60 + 15   # 9:15 AM
FIRST_HOUR_END   = 10 * 60 + 15  # 10:15 AM


def _now_ist():
    return datetime.now(IST)


def _minute_of_day_ist(ts_str: str):
    """Parse a UTC timestamp string and return (minutes-since-midnight IST, ist_datetime)."""
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
    return ts.hour * 60 + ts.minute, ts


def get_first_hour_breakout_scan(supabase, symbols):
    today_str = _now_ist().date().isoformat()

    res = supabase.from_("cmp_prices")\
        .select("symbol, cmp, timestamp")\
        .in_("symbol", symbols)\
        .gte("timestamp", f"{today_str}T00:00:00+00:00")\
        .order("timestamp", desc=False)\
        .limit(30000).execute()

    rows_by_symbol: dict = {}
    for r in (res.data or []):
        rows_by_symbol.setdefault(r["symbol"], []).append(r)

    candidates = []
    for sym, ticks in rows_by_symbol.items():
        first_hour_high = None
        first_hour_low = None
        after_ticks = []
        for t in ticks:
            px = float(t["cmp"] or 0)
            if px <= 0:
                continue
            mins, ts_ist = _minute_of_day_ist(t["timestamp"])
            if FIRST_HOUR_START <= mins < FIRST_HOUR_END:
                first_hour_high = px if first_hour_high is None else max(first_hour_high, px)
                first_hour_low  = px if first_hour_low  is None else min(first_hour_low, px)
            elif mins >= FIRST_HOUR_END:
                after_ticks.append((mins, px, ts_ist))

        if first_hour_high is None or not after_ticks:
            continue

        broke_out = False
        breakout_time = None
        failed = False
        # BUG FIX (Aug 6 2026): a single stray tick momentarily below the
        # first-hour low (a brief data capture glitch -- observed on
        # MOTHERSON today: two isolated ~2% drops that fully reverted the
        # very next tick, not real trading) used to permanently mark a
        # stock "failed" forever, even though its real price action never
        # broke down. Now requires 2 CONSECUTIVE ticks below the low
        # before calling it a genuine failure -- same "persistence"
        # principle already used by the Intraday Signal Log elsewhere on
        # the platform. A real breakdown persists across multiple ticks;
        # a glitch doesn't.
        consecutive_below_low = 0
        for mins, px, ts_ist in after_ticks:
            if not broke_out and px > first_hour_high:
                broke_out = True
                breakout_time = ts_ist
                continue
            if broke_out:
                if px < first_hour_low:
                    consecutive_below_low += 1
                    if consecutive_below_low >= 2:
                        failed = True
                else:
                    consecutive_below_low = 0

        if not broke_out:
            continue

        cmp_now = after_ticks[-1][1]
        candidates.append({
            "symbol": sym,
            "state": "failed" if failed else "sustaining",
            "first_hour_high": round(first_hour_high, 2),
            "first_hour_low": round(first_hour_low, 2),
            "breakout_time": breakout_time.strftime("%H:%M") if breakout_time else None,
            "cmp": round(cmp_now, 2),
        })

    if not candidates:
        return {"scanned": len(rows_by_symbol), "matches": 0, "results": [],
                "generated_at": _now_ist().isoformat()}

    # ── Cross-reference confirmation signals ──────────────────────────────
    # Put Writing: reuse the exact same near-ATM UOA signals already
    # powering the Alerts feed, so "confirmed" means the same thing here.
    put_writing_syms: set = set()
    try:
        from api.uoa import get_uoa
        today = _now_ist().strftime("%Y-%m-%d")
        uoa_data = get_uoa(date=today)
        for sig in (uoa_data.get("signals") or []):
            if sig.get("signal_type") == "PUT_WRITING" and sig.get("score", 0) >= 4:
                put_writing_syms.add(sig["symbol"])
    except Exception as e:
        print(f"[FIRST_HOUR_BREAKOUT] UOA fetch failed, skipping put-writing confirmation: {e}")

    # FUT Long Buildup: reuse the already-fixed (nearest-expiry-only) FUT
    # OI helper from oi_pulse.py, comparing today's open vs latest.
    fut_buildup_syms: set = set()
    try:
        from api.oi_pulse import fetch_fut_oi_for_timestamp
        ts_res = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{today_str}T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(1).execute()
        ts_open = (ts_res.data or [{}])[0].get("timestamp")
        ts_latest_res = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{today_str}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        ts_latest = (ts_latest_res.data or [{}])[0].get("timestamp")

        if ts_open and ts_latest and ts_open != ts_latest:
            fut_oi_open = fetch_fut_oi_for_timestamp(supabase, ts_open)
            fut_oi_new = fetch_fut_oi_for_timestamp(supabase, ts_latest)
            for c in candidates:
                sym = c["symbol"]
                oi_o, oi_n = fut_oi_open.get(sym, 0), fut_oi_new.get(sym, 0)
                if oi_o > 0:
                    fut_oi_chg_pct = (oi_n - oi_o) / oi_o * 100
                    price_chg_pct = (c["cmp"] - c["first_hour_high"]) / c["first_hour_high"] * 100
                    if fut_oi_chg_pct >= 3.0 and price_chg_pct > 0:
                        fut_buildup_syms.add(sym)
    except Exception as e:
        print(f"[FIRST_HOUR_BREAKOUT] FUT OI fetch failed, skipping FUT buildup confirmation: {e}")

    for c in candidates:
        c["put_writing"] = c["symbol"] in put_writing_syms
        c["fut_buildup"] = c["symbol"] in fut_buildup_syms
        c["confirmed"] = c["put_writing"] or c["fut_buildup"]

    _state_rank = {"failed": 0, "sustaining": 1}
    candidates.sort(key=lambda c: (_state_rank.get(c["state"], 2), not c["confirmed"]))

    return {
        "scanned": len(rows_by_symbol),
        "matches": len(candidates),
        "results": candidates,
        "generated_at": _now_ist().isoformat(),
    }
