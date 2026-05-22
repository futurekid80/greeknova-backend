from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import calendar


def get_oi_profile(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()

    if not date:
        date = today.isoformat()

    # Find last available trading date with data
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

    # Get EOD timestamp
    ts_q = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{date}T00:00:00+00:00")\
        .lt("timestamp",  f"{date}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    if not ts_q.data:
        return {"error": f"No data for {symbol} on {date}"}

    eod_ts = ts_q.data[0]["timestamp"]

    # Fetch all strikes with pagination
    all_rows = []
    for offset in range(0, 50000, 1000):
        q = supabase.from_("oi_snapshots")\
            .select("strike, option_type, oi, expiry")\
            .eq("symbol", symbol)\
            .eq("timestamp", eod_ts)\
            .range(offset, offset + 999)
        if expiry:
            q = q.eq("expiry", expiry)
        batch = q.execute()
        if not batch.data:
            break
        all_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not all_rows:
        return {"error": "No OI data found"}

    # Get available expiries — include ALL expiries for dropdown
    today_str = date_type.today().isoformat()
    expiries = sorted(set(
        r["expiry"] for r in all_rows
        if r["expiry"] and r["expiry"] >= today_str
    ))

    active_expiry = expiry or (expiries[0] if expiries else None)

    if active_expiry:
        all_rows = [r for r in all_rows if r["expiry"] == active_expiry]

    # Build strike profile
    ce_oi: dict = {}
    pe_oi: dict = {}

    for r in all_rows:
        strike = float(r["strike"])
        oi     = r["oi"] or 0
        if r["option_type"] == "CE":
            ce_oi[strike] = ce_oi.get(strike, 0) + oi
        else:
            pe_oi[strike] = pe_oi.get(strike, 0) + oi

    all_strikes_raw = sorted(set(list(ce_oi.keys()) + list(pe_oi.keys())))

    # Get CMP for ATM
    cmp = None
    try:
        cmp_q = supabase.from_("cmp_prices")\
            .select("cmp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{date}T00:00:00+00:00")\
            .lt("timestamp",  f"{date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if cmp_q.data:
            cmp = float(cmp_q.data[0]["cmp"])
    except:
        pass

    # Filter to meaningful strike range ±5% of CMP
    if cmp and cmp > 0:
        lower_bound = cmp * 0.95
        upper_bound = cmp * 1.05
        all_strikes = [s for s in all_strikes_raw if lower_bound <= s <= upper_bound]
        if len(all_strikes) < 10:
            lower_bound = cmp * 0.93
            upper_bound = cmp * 1.07
            all_strikes = [s for s in all_strikes_raw if lower_bound <= s <= upper_bound]
    else:
        max_total_raw = max(
            (ce_oi.get(s, 0) + pe_oi.get(s, 0)) for s in all_strikes_raw
        ) if all_strikes_raw else 1
        threshold = max_total_raw * 0.01
        all_strikes = [s for s in all_strikes_raw
                       if (ce_oi.get(s, 0) + pe_oi.get(s, 0)) >= threshold]

    if not all_strikes:
        return {"error": "No strike data"}

    atm_strike = min(all_strikes, key=lambda s: abs(s - cmp)) if cmp else None

    # Calculate metrics
    total_ce = sum(ce_oi.values())
    total_pe = sum(pe_oi.values())
    total_oi = total_ce + total_pe

    poc_strike = max(all_strikes, key=lambda s: (ce_oi.get(s, 0) + pe_oi.get(s, 0)))
    ce_wall = max(ce_oi, key=ce_oi.get) if ce_oi else None
    pe_wall = max(pe_oi, key=pe_oi.get) if pe_oi else None

    # Value area
    va_threshold = total_oi * 0.70
    sorted_by_oi = sorted(all_strikes, key=lambda s: (ce_oi.get(s,0)+pe_oi.get(s,0)), reverse=True)
    va_strikes = []
    va_cum = 0
    for s in sorted_by_oi:
        va_cum += ce_oi.get(s, 0) + pe_oi.get(s, 0)
        va_strikes.append(s)
        if va_cum >= va_threshold:
            break
    vah = max(va_strikes) if va_strikes else None
    val = min(va_strikes) if va_strikes else None

    max_ce = max(ce_oi.values()) if ce_oi else 1
    max_pe = max(pe_oi.values()) if pe_oi else 1
    max_total = max(ce_oi.get(s,0)+pe_oi.get(s,0) for s in all_strikes)

    vac_ce_threshold = max_ce * 0.05
    vac_pe_threshold = max_pe * 0.05

    profile = []
    for s in all_strikes:
        ce = ce_oi.get(s, 0)
        pe = pe_oi.get(s, 0)
        total = ce + pe
        imbalance = round((ce - pe) / total * 100) if total > 0 else 0
        is_vacuum = (ce < vac_ce_threshold and pe < vac_pe_threshold and total > 0)
        in_value_area = (val <= s <= vah) if (val and vah) else False

        profile.append({
            "strike":        s,
            "ce_oi":         ce,
            "pe_oi":         pe,
            "total_oi":      total,
            "ce_pct":        round(ce / max_ce  * 100) if max_ce  > 0 else 0,
            "pe_pct":        round(pe / max_pe  * 100) if max_pe  > 0 else 0,
            "total_pct":     round(total / max_total * 100) if max_total > 0 else 0,
            "imbalance":     imbalance,
            "is_vacuum":     is_vacuum,
            "is_poc":        s == poc_strike,
            "is_ce_wall":    s == ce_wall,
            "is_pe_wall":    s == pe_wall,
            "is_atm":        s == atm_strike,
            "in_value_area": in_value_area,
        })

    # ── OPTIMIZED Wall Migration — 3 queries instead of 40+ ──────────────────
    # Step 1: Get one EOD timestamp per day for last 20 days in ONE query
    start_date = (today - timedelta(days=20)).isoformat()

    # Fetch all EOD timestamps in date range — get latest per day
    all_ts_rows = []
    for offset in range(0, 5000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{start_date}T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_ts_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # Group timestamps by date, keep latest per day
    day_ts_map: dict = {}
    for row in all_ts_rows:
        ts = row["timestamp"]
        d = ts[:10]  # YYYY-MM-DD
        if d not in day_ts_map or ts > day_ts_map[d]:
            day_ts_map[d] = ts

    eod_timestamps = list(day_ts_map.values())[-15:]  # limit to 15

    # Step 2: Fetch ALL OI data for all EOD timestamps in ONE query
    all_migration_rows = []
    if eod_timestamps:
        for offset in range(0, 200000, 1000):
            q = supabase.from_("oi_snapshots")\
                .select("timestamp, strike, option_type, oi")\
                .eq("symbol", symbol)\
                .in_("timestamp", eod_timestamps)\
                .range(offset, offset + 999)
            if active_expiry:
                q = q.eq("expiry", active_expiry)
            batch = q.execute()
            if not batch.data:
                break
            all_migration_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break

    # Step 3: Fetch ALL CMP data for date range in ONE query
    all_cmp_rows = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("timestamp, cmp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{start_date}T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_cmp_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # Build CMP map — latest CMP per day
    day_cmp_map: dict = {}
    for row in all_cmp_rows:
        d = row["timestamp"][:10]
        if d not in day_cmp_map or row["timestamp"] > day_cmp_map[d]["ts"]:
            day_cmp_map[d] = {"ts": row["timestamp"], "cmp": float(row["cmp"])}

    # Group migration rows by timestamp
    from collections import defaultdict
    ts_rows_map: dict = defaultdict(list)
    for row in all_migration_rows:
        ts_rows_map[row["timestamp"]].append(row)

    # Compute walls per day
    wall_migration = []
    lo = cmp * 0.90 if cmp and cmp > 0 else 0
    hi = cmp * 1.10 if cmp and cmp > 0 else float('inf')

    for d in sorted(day_ts_map.keys()):
        day_ts = day_ts_map[d]
        day_rows = ts_rows_map.get(day_ts, [])
        if not day_rows:
            continue

        day_ce: dict = {}
        day_pe: dict = {}
        for r in day_rows:
            strike = float(r["strike"])
            oi     = r["oi"] or 0
            if cmp and cmp > 0 and not (lo <= strike <= hi):
                continue
            if r["option_type"] == "CE":
                day_ce[strike] = day_ce.get(strike, 0) + oi
            else:
                day_pe[strike] = day_pe.get(strike, 0) + oi

        if day_ce and day_pe:
            day_cmp_val = day_cmp_map.get(d, {}).get("cmp")
            wall_migration.append({
                "date":       d,
                "ce_wall":    max(day_ce, key=day_ce.get),
                "pe_wall":    max(day_pe, key=day_pe.get),
                "ce_wall_oi": day_ce[max(day_ce, key=day_ce.get)],
                "pe_wall_oi": day_pe[max(day_pe, key=day_pe.get)],
                "cmp":        day_cmp_val,
            })

    return {
        "symbol":          symbol,
        "date":            date,
        "expiry":          active_expiry,
        "expiries":        expiries,
        "cmp":             cmp,
        "atm_strike":      atm_strike,
        "poc_strike":      poc_strike,
        "ce_wall":         ce_wall,
        "pe_wall":         pe_wall,
        "vah":             vah,
        "val":             val,
        "total_ce_oi":     total_ce,
        "total_pe_oi":     total_pe,
        "pcr":             round(total_pe / total_ce, 2) if total_ce > 0 else 0,
        "profile":         profile,
        "wall_migration":  wall_migration,
        "vacuum_count":    sum(1 for p in profile if p["is_vacuum"]),
    }
