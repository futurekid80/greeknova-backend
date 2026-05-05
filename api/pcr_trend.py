from utils.db import get_supabase
from datetime import datetime, timezone

def get_pcr_trend(symbol: str = "NIFTY", expiry: str = None):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    query = supabase.from_("oi_snapshots")\
        .select("timestamp, option_type, oi, expiry")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=False)

    if expiry:
        query = query.eq("expiry", expiry)

    result = query.execute()

    if not result.data:
        return {"symbol": symbol, "points": [], "expiry": expiry}

    ts_map: dict = {}
    for row in result.data:
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
            "time": time_label,
            "pcr": pcr,
            "ce_oi": ce,
            "pe_oi": pe,
        })

    return {"symbol": symbol, "points": points, "total_snapshots": len(points), "expiry": expiry}
