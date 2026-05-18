from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type


def get_oi_heatmap(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    """
    Returns OI data across all intraday snapshots for a given day.
    Used to build the Strike Heatmap Timeline.
    
    Returns:
    - timestamps: list of snapshot times (IST)
    - strikes: list of strikes
    - ce_heatmap: dict[strike][timestamp] = OI value
    - pe_heatmap: dict[strike][timestamp] = OI value
    - cmp_series: list of {timestamp, cmp} for price overlay
    """
    supabase = get_supabase()
    today    = datetime.now(timezone.utc).date()

    # ── Resolve date ──────────────────────────────────────────────────────────
    if not date:
        for i in range(7):
            check = (today - timedelta(days=i)).isoformat()
            r = supabase.from_("oi_snapshots")\
                .select("timestamp")\
                .eq("symbol", symbol)\
                .gte("timestamp", f"{check}T00:00:00+00:00")\
                .lt("timestamp",  f"{check}T23:59:59+00:00")\
                .limit(1).execute()
            if r.data:
                date = check
                break

    if not date:
        return {"error": "No data found"}

    # ── Get all timestamps for this day ───────────────────────────────────────
    all_ts_q = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date}T00:00:00+00:00")\
        .lt("timestamp",  f"{date}T23:59:59+00:00")\
        .order("timestamp").execute()

    if not all_ts_q.data:
        return {"error": f"No snapshots for {symbol} on {date}"}

    # Get unique timestamps
    all_timestamps = sorted(set(r["timestamp"] for r in all_ts_q.data))

    # Limit to max 30 snapshots evenly spaced for performance
    if len(all_timestamps) > 30:
        step = len(all_timestamps) // 30
        all_timestamps = all_timestamps[::step]

    # ── Get nearest expiry ────────────────────────────────────────────────────
    exp_q = supabase.from_("oi_snapshots")\
        .select("expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", all_timestamps[-1])\
        .limit(200).execute()

    today_str = date_type.today().isoformat()
    expiries = sorted(set(
        r["expiry"] for r in (exp_q.data or [])
        if r["expiry"] and r["expiry"] >= today_str
    ))
    active_expiry = expiry or (expiries[0] if expiries else None)

    # ── Get CMP for this day ──────────────────────────────────────────────────
    cmp_q = supabase.from_("cmp_prices")\
        .select("timestamp, cmp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date}T00:00:00+00:00")\
        .lt("timestamp",  f"{date}T23:59:59+00:00")\
        .order("timestamp").execute().data or []

    # Sample CMP at each snapshot time (nearest)
    cmp_series = []
    for ts in all_timestamps:
        nearest_cmp = None
        min_diff = float('inf')
        for c in cmp_q:
            diff = abs((datetime.fromisoformat(c["timestamp"].replace('+00:00','')) -
                       datetime.fromisoformat(ts.replace('+00:00',''))).total_seconds())
            if diff < min_diff:
                min_diff = diff
                nearest_cmp = c["cmp"]
        cmp_series.append({"timestamp": ts, "cmp": nearest_cmp})

    # ── Get CMP range for strike filter ───────────────────────────────────────
    cmps = [c["cmp"] for c in cmp_series if c["cmp"]]
    mid_cmp = sum(cmps) / len(cmps) if cmps else None

    lower = (mid_cmp * 0.90) if mid_cmp else None
    upper = (mid_cmp * 1.10) if mid_cmp else None

    # ── Fetch OI for each timestamp ───────────────────────────────────────────
    ce_heatmap: dict = {}  # strike -> {timestamp -> oi}
    pe_heatmap: dict = {}

    for ts in all_timestamps:
        rows = []
        for offset in range(0, 5000, 1000):
            q = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts)
            if active_expiry:
                q = q.eq("expiry", active_expiry)
            if lower and upper:
                q = q.gte("strike", lower).lte("strike", upper)
            batch = q.range(offset, offset+999).execute()
            if not batch.data:
                break
            rows.extend(batch.data)
            if len(batch.data) < 1000:
                break

        for r in rows:
            s = float(r["strike"])
            oi = r["oi"] or 0
            if r["option_type"] == "CE":
                if s not in ce_heatmap:
                    ce_heatmap[s] = {}
                ce_heatmap[s][ts] = oi
            else:
                if s not in pe_heatmap:
                    pe_heatmap[s] = {}
                pe_heatmap[s][ts] = oi

    # ── Build sorted strikes ──────────────────────────────────────────────────
    all_strikes = sorted(set(list(ce_heatmap.keys()) + list(pe_heatmap.keys())))

    # ── Normalize OI for color intensity (0-100) ──────────────────────────────
    max_ce_global = max(
        (v for s in ce_heatmap.values() for v in s.values()), default=1
    )
    max_pe_global = max(
        (v for s in pe_heatmap.values() for v in s.values()), default=1
    )

    # Convert to array format for frontend
    ce_data = []
    pe_data = []

    for s in all_strikes:
        ce_row = {"strike": s, "values": []}
        pe_row = {"strike": s, "values": []}
        for ts in all_timestamps:
            ce_oi = ce_heatmap.get(s, {}).get(ts, 0)
            pe_oi = pe_heatmap.get(s, {}).get(ts, 0)
            ce_row["values"].append({
                "ts":        ts,
                "oi":        ce_oi,
                "intensity": round(ce_oi / max_ce_global * 100) if max_ce_global > 0 else 0
            })
            pe_row["values"].append({
                "ts":        ts,
                "oi":        pe_oi,
                "intensity": round(pe_oi / max_pe_global * 100) if max_pe_global > 0 else 0
            })
        ce_data.append(ce_row)
        pe_data.append(pe_row)

    # Convert timestamps to IST for display
    def to_ist(ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts.replace('+00:00', ''))
            ist = dt + timedelta(hours=5, minutes=30)
            return ist.strftime('%H:%M')
        except:
            return ts

    time_labels = [to_ist(ts) for ts in all_timestamps]

    return {
        "symbol":        symbol,
        "date":          date,
        "expiry":        active_expiry,
        "expiries":      expiries,
        "timestamps":    all_timestamps,
        "time_labels":   time_labels,
        "strikes":       all_strikes,
        "ce_data":       ce_data,
        "pe_data":       pe_data,
        "cmp_series":    cmp_series,
        "mid_cmp":       mid_cmp,
        "snapshot_count": len(all_timestamps),
    }
