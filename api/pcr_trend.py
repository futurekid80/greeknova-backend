from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

def get_pcr_trend(symbol: str = "NIFTY", expiry: str = None):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_str = date_type.today().isoformat()

    # ── Get current CMP for ATM-centered strike filter ────────────────────────
    cmp = None
    try:
        cmp_q = supabase.from_("cmp_prices")\
            .select("cmp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if cmp_q.data:
            cmp = float(cmp_q.data[0]["cmp"])
    except:
        pass

    # Strike interval per symbol
    if symbol == "NIFTY":
        strike_interval = 50
    elif symbol == "BANKNIFTY":
        strike_interval = 100
    elif symbol == "FINNIFTY":
        strike_interval = 50
    else:
        strike_interval = 50

    # Snap CMP to nearest strike interval for consistent fixed set
if cmp:
    snapped_atm = round(cmp / strike_interval) * strike_interval
    strike_lower = snapped_atm - (10 * strike_interval)
    strike_upper = snapped_atm + (10 * strike_interval)
else:
    strike_lower = None
    strike_upper = None

    # ── Fetch OI data with strike ─────────────────────────────────────────────
    query = supabase.from_("oi_snapshots")\
        .select("timestamp, option_type, oi, expiry, strike")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=False)

    if expiry:
        query = query.eq("expiry", expiry)

    all_data = []
    for offset in range(0, 50000, 1000):
        batch = query.range(offset, offset + 999).execute()
        if not batch.data:
            break
        all_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not all_data:
        return {"symbol": symbol, "points": [], "expiry": expiry}

    # ── Filter to nearest active expiry ───────────────────────────────────────
    if not expiry:
        active_expiries = sorted(set(
            r["expiry"] for r in all_data
            if r["expiry"] and r["expiry"] >= today_str
        ))
        nearest_expiry = active_expiries[0] if active_expiries else None
        if nearest_expiry:
            all_data = [r for r in all_data if r["expiry"] == nearest_expiry]
            expiry = nearest_expiry

    # ── Filter to ATM ±10 strikes for stable PCR ──────────────────────────────
    if strike_lower and strike_upper:
        all_data = [
            r for r in all_data
            if r.get("strike") and strike_lower <= float(r["strike"]) <= strike_upper
        ]

    # ── Group by timestamp ────────────────────────────────────────────────────
    ts_map: dict = {}
    for row in all_data:
        ts = row["timestamp"]
        if ts not in ts_map:
            ts_map[ts] = {"ce": 0, "pe": 0}
        if row["option_type"] == "CE":
            ts_map[ts]["ce"] += row["oi"] or 0
        else:
            ts_map[ts]["pe"] += row["oi"] or 0

    points = []
    for ts, val in sorted(ts_map.items()):
        ce = val["ce"]
        pe = val["pe"]
        pcr = round(pe / ce, 3) if ce > 0 else 0
        try:
            clean_ts = ts.split('+')[0].split('Z')[0]
            if '.' in clean_ts:
                base, frac = clean_ts.split('.')
                frac = frac[:6].ljust(6, '0')
                clean_ts = f"{base}.{frac}"
            dt = datetime.fromisoformat(clean_ts).replace(tzinfo=timezone.utc)
            ist_total_min = dt.hour * 60 + dt.minute + 330
            ist_hour = (ist_total_min // 60) % 24
            ist_min = ist_total_min % 60
            time_label = f"{ist_hour:02d}:{ist_min:02d}"
        except Exception:
            time_label = ts[11:16]

        points.append({
            "timestamp": ts,
            "time":      time_label,
            "pcr":       pcr,
            "ce_oi":     ce,
            "pe_oi":     pe,
        })

    return {
        "symbol":          symbol,
        "points":          points,
        "total_snapshots": len(points),
        "expiry":          expiry,
    }
