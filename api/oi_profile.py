from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import calendar


def get_monthly_expiry(year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day)
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')


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

    # Get available expiries
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

    # ── Wall migration — CE/PE wall + CMP per trading day this series ─────────
    wall_migration = []
    for i in range(20):
        d = (today - timedelta(days=i)).isoformat()
        ts_r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if not ts_r.data:
            continue

        day_ts = ts_r.data[0]["timestamp"]
        day_rows = []
        for offset in range(0, 20000, 1000):
            q = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi")\
                .eq("symbol", symbol)\
                .eq("timestamp", day_ts)
            if active_expiry:
                q = q.eq("expiry", active_expiry)
            batch = q.range(offset, offset+999).execute()
            if not batch.data:
                break
            day_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break

        day_ce: dict = {}
        day_pe: dict = {}
        for r in day_rows:
            strike = float(r["strike"])
            oi     = r["oi"] or 0
            if r["option_type"] == "CE":
                day_ce[strike] = day_ce.get(strike, 0) + oi
            else:
                day_pe[strike] = day_pe.get(strike, 0) + oi

        if day_ce and day_pe:
            if cmp and cmp > 0:
                lo, hi = cmp * 0.90, cmp * 1.10
                day_ce = {k: v for k, v in day_ce.items() if lo <= k <= hi}
                day_pe = {k: v for k, v in day_pe.items() if lo <= k <= hi}

            if day_ce and day_pe:
                # ── Fetch closing CMP for this day ────────────────────────────
                day_cmp = None
                try:
                    cmp_day_q = supabase.from_("cmp_prices")\
                        .select("cmp")\
                        .eq("symbol", symbol)\
                        .gte("timestamp", f"{d}T00:00:00+00:00")\
                        .lt("timestamp",  f"{d}T23:59:59+00:00")\
                        .order("timestamp", desc=True)\
                        .limit(1).execute()
                    if cmp_day_q.data:
                        day_cmp = float(cmp_day_q.data[0]["cmp"])
                except:
                    pass

                wall_migration.append({
                    "date":       d,
                    "ce_wall":    max(day_ce, key=day_ce.get),
                    "pe_wall":    max(day_pe, key=day_pe.get),
                    "ce_wall_oi": day_ce[max(day_ce, key=day_ce.get)],
                    "pe_wall_oi": day_pe[max(day_pe, key=day_pe.get)],
                    "cmp":        day_cmp,  # ← NEW: closing price for this day
                })

    wall_migration.reverse()

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
