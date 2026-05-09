from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type


def get_eod_analysis(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    supabase = get_supabase()

    # ── Get available dates via per-day probe (avoids row limit issues) ───────
    dates = set()
    base = datetime.now(timezone.utc).date()
    for i in range(60):
        d = (base - timedelta(days=i)).isoformat()
        r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp", f"{d}T23:59:59+00:00")\
            .limit(1)\
            .execute()
        if r.data:
            dates.add(d)
        if len(dates) >= 30:
            break

    if not dates:
        return {"symbol": symbol, "dates": [], "rows": []}

    sorted_dates = sorted(dates)  # ascending for dropdown

    # ── Default to last FULL trading day (5+ snapshots) ──────────────────────
    if date:
        active_date = date
    else:
        active_date = sorted_dates[-1]  # fallback
        for d in reversed(sorted_dates):
            ts_check = supabase.from_("oi_snapshots")\
                .select("timestamp")\
                .eq("symbol", symbol)\
                .gte("timestamp", f"{d}T00:00:00+00:00")\
                .lt("timestamp", f"{d}T23:59:59+00:00")\
                .limit(10)\
                .execute()
            if len(ts_check.data or []) >= 5:
                active_date = d
                break

    # ── Get timestamps for selected date ─────────────────────────────────────
    day_ts = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp",  f"{active_date}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .limit(5000)\
        .execute()

    if not day_ts.data:
        return {"symbol": symbol, "dates": sorted_dates, "date": active_date, "rows": []}

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

    if not expiries:
        expiries = sorted(set(r["expiry"] for r in exp_q.data if r["expiry"]))

    active_expiry = expiry or (expiries[0] if expiries else None)

    # ── Fetch open/close snapshots ────────────────────────────────────────────
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

    # ── Intraday journey ──────────────────────────────────────────────────────
    journey_q = supabase.from_("oi_snapshots")\
        .select("timestamp, option_type, oi")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp", f"{active_date}T23:59:59+00:00")
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
            "strike":    strike,
            "ce_open":   ce_open,
            "ce_close":  ce_close,
            "ce_chg":    ce_chg,
            "pe_open":   pe_open,
            "pe_close":  pe_close,
            "pe_chg":    pe_chg,
            "net_chg":   pe_chg - ce_chg,
        })

    return {
        "symbol":     symbol,
        "date":       active_date,
        "dates":      sorted_dates,
        "expiry":     active_expiry,
        "expiries":   expiries,
        "open_time":  to_ist(first_ts),
        "close_time": to_ist(last_ts),
        "snapshots":  len(timestamps),
        "journey":    journey_data,
        "rows":       rows,
    }
# eod fix
