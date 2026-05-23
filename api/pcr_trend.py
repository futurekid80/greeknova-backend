from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

def get_pcr_trend(symbol: str = "NIFTY", expiry: str = None):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_str = date_type.today().isoformat()

    # ── Get current CMP for ATM ±10 strike filter ─────────────────────────────
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
    strike_interval = 100 if symbol == "BANKNIFTY" else 50

    # Snap CMP to nearest strike interval → fixed ATM ±10 strikes
    if cmp:
        snapped_atm = round(cmp / strike_interval) * strike_interval
        strike_lower = snapped_atm - (10 * strike_interval)
        strike_upper = snapped_atm + (10 * strike_interval)
    else:
        strike_lower = None
        strike_upper = None

    # ── Fetch OI + Volume data with strike ────────────────────────────────────
    query = supabase.from_("oi_snapshots")\
        .select("timestamp, option_type, oi, volume, expiry, strike")\
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

    # ── Group by timestamp — compute both OI PCR and Vol PCR ─────────────────
    ts_map: dict = {}
    for row in all_data:
        ts = row["timestamp"]
        if ts not in ts_map:
            ts_map[ts] = {"ce_oi": 0, "pe_oi": 0, "ce_vol": 0, "pe_vol": 0}
        if row["option_type"] == "CE":
            ts_map[ts]["ce_oi"]  += row["oi"] or 0
            ts_map[ts]["ce_vol"] += row["volume"] or 0
        else:
            ts_map[ts]["pe_oi"]  += row["oi"] or 0
            ts_map[ts]["pe_vol"] += row["volume"] or 0

    points = []
    for ts, val in sorted(ts_map.items()):
        ce_oi  = val["ce_oi"]
        pe_oi  = val["pe_oi"]
        ce_vol = val["ce_vol"]
        pe_vol = val["pe_vol"]

        oi_pcr  = round(pe_oi  / ce_oi,  3) if ce_oi  > 0 else 0
        vol_pcr = round(pe_vol / ce_vol, 3) if ce_vol > 0 else 0

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
            "pcr":       oi_pcr,
            "vol_pcr":   vol_pcr,
            "ce_oi":     ce_oi,
            "pe_oi":     pe_oi,
            "ce_vol":    ce_vol,
            "pe_vol":    pe_vol,
        })

    return {
        "symbol":          symbol,
        "points":          points,
        "total_snapshots": len(points),
        "expiry":          expiry,
    }
