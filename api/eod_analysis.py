from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

def get_eod_analysis(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    supabase = get_supabase()

    # ── Get available dates ───────────────────────────────────────────────────
    # Fetch DESC so latest dates are always in first page — avoids missing
    # recent dates when total rows exceed pagination depth.
    all_dates = set()
    for offset in range(0, 10000, 1000):
        result = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999)\
            .execute()
        if not result.data:
            break
        for r in result.data:
            all_dates.add(r["timestamp"][:10])
        if len(result.data) < 1000:
            break
        # Stop once we have enough distinct dates (no need to scan all history)
        if len(all_dates) >= 30:
            break

    if not all_dates:
        return {"symbol": symbol, "dates": [], "rows": []}

    dates = sorted(all_dates)          # ascending for dropdown display
    active_date = date or dates[-1]    # default = most recent

    # ── Get timestamps for selected date ─────────────────────────────────────
    day_ts = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp",  f"{active_date}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    if not day_ts.data:
        return {"symbol": symbol, "dates": dates, "date": active_date, "rows": []}

    timestamps = sorted(set(r["timestamp"] for r in day_ts.data))
    first_ts = timestamps[0]
    last_ts  = timestamps[-1]

    # ── Get expiries ──────────────────────────────────────────────────────────
    today_str = date_type.today().isoformat()
    exp_q = supabase.from_("oi_snapshots")\
        .select("expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", last_ts)\
        .execute()

    expiries = sorted(set(
        r["expiry"] for r in exp_q.data
        if r["expiry"] and r["expiry"] >= today_str
    )) if exp_q.data else []

    # For past dates include expired expiries too
    if not expiries:
        expiries = sorted(set(r["expiry"] for r in exp_q.data if r["expiry"]))

    active_expiry = expiry or (expiries[0] if expiries else None)

    # ── Fetch a single snapshot (open or close) ───────────────────────────────
    def fetch_snap(ts):
        all_data = []
        for offset in range(0, 10000, 1000):
            q = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts)
            if active_expiry:
                q = q.eq("expiry", active_expiry)
            batch = q.range(offset, offset + 999).execute()
            if not batch.data:
                break
            all_data.extend(batch.data)
            if len(batch.data) < 1000:
                break
        result = {}
        for r in all_data:
            result[(r["strike"], r["option_type"])] = r["oi"] or 0
        return result

    snap_open  = fetch_snap(first_ts)
    snap_close = fetch_snap(last_ts)

    all_strikes = sorted(set(k[0] for k in list(snap_open.keys()) + list(snap_close.keys())))

    # ── Intraday journey — all timestamps in one paginated query ──────────────
    journey_q = supabase.from_("oi_snapshots")\
        .select("timestamp, option_type, oi")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp",  f"{active_date}T23:59:59+00:00")
    if active_expiry:
        journey_q = journey_q.eq("expiry", active_expiry)

    journey_raw = []
    for offset in range(0, 50000, 1000):
        batch = journey_q.range(offset, offset + 999).execute()
        if not batch.data:
            break
        journey_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    ts_groups: dict = {}
    for r in journey_raw:
        ts = r["timestamp"]
        if ts not in ts_groups:
            ts_groups[ts] = {"ce": 0, "pe": 0}
        if r["option_type"] == "CE":
            ts_groups[ts]["ce"] += r["oi"] or 0
        else:
            ts_groups[ts]["pe"] += r["oi"] or 0

    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                clean = f"{base}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist_min = dt.hour * 60 + dt.minute + 330
            return f"{(ist_min//60)%24:02d}:{ist_min%60:02d}"
        except:
            return ts[11:16]

    journey_data = []
    for ts in sorted(ts_groups.keys()):
        ce = ts_groups[ts]["ce"]
        pe = ts_groups[ts]["pe"]
        pcr = round(pe / ce, 3) if ce > 0 else 0
        journey_data.append({"time": to_ist(ts), "ce_oi": ce, "pe_oi": pe, "pcr": pcr})

    rows = []
    for strike in all_strikes:
        ce_open  = snap_open.get((strike, "CE"), 0)
        ce_close = snap_close.get((strike, "CE"), 0)
        pe_open  = snap_open.get((strike, "PE"), 0)
        pe_close = snap_close.get((strike, "PE"), 0)
        ce_chg   = ce_close - ce_open
        pe_chg   = pe_close - pe_open
        rows.append({
            "strike":   strike,
            "ce_open":  ce_open,
            "ce_close": ce_close,
            "ce_chg":   ce_chg,
            "pe_open":  pe_open,
            "pe_close": pe_close,
            "pe_chg":   pe_chg,
            "net_chg":  pe_chg - ce_chg,
        })

    return {
        "symbol":    symbol,
        "date":      active_date,
        "dates":     dates,
        "expiry":    active_expiry,
        "expiries":  expiries,
        "open_time": to_ist(first_ts),
        "close_time": to_ist(last_ts),
        "snapshots": len(timestamps),
        "journey":   journey_data,
        "rows":      rows,
    }
