from utils.db import get_supabase
from datetime import datetime, timezone

def get_oi_spikes(threshold: float = 10.0, date: str = None):
    supabase = get_supabase()

    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get all timestamps for the given date
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data))

    if len(timestamps) < 2:
        # Fallback: use last 2 global snapshots
        result = supabase.from_("oi_snapshots").select("timestamp").order("timestamp", desc=True).limit(1000).execute()
        timestamps = sorted(set(r["timestamp"] for r in result.data), reverse=True)
        if len(timestamps) < 2:
            return {"error": "Need at least 2 snapshots", "spikes": []}
        ts_old = timestamps[1]
        ts_new = timestamps[0]
    else:
        ts_old = timestamps[0]   # first snapshot of day (open)
        ts_new = timestamps[-1]  # last snapshot of day (close)

    # Fetch both snapshots
    new_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_new).execute().data
    old_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_old).execute().data

    old_map = {}
    for row in old_data:
        key = f"{row['symbol']}_{row['tradingsymbol']}"
        old_map[key] = row

    spikes = []
    for row in new_data:
        key = f"{row['symbol']}_{row['tradingsymbol']}"
        old_row = old_map.get(key)
        if not old_row:
            continue

        old_oi = old_row["oi"] or 0
        new_oi = row["oi"] or 0

        if old_oi < 1000:
            continue

        oi_change = new_oi - old_oi
        oi_pct = round((oi_change / old_oi * 100), 2) if old_oi > 0 else 0
        vol_change = (row["volume"] or 0) - (old_row["volume"] or 0)

        if abs(oi_pct) >= threshold:
            spikes.append({
                "symbol":        row["symbol"],
                "tradingsymbol": row["tradingsymbol"],
                "strike":        row["strike"],
                "option_type":   row["option_type"],
                "old_oi":        old_oi,
                "new_oi":        new_oi,
                "oi_change":     oi_change,
                "oi_pct":        oi_pct,
                "volume":        row["volume"],
                "vol_change":    vol_change,
                "last_price":    row["last_price"],
                "is_index":      row.get("is_index", False),
                "direction":     "BUILD" if oi_change > 0 else "UNWIND",
            })

    spikes.sort(key=lambda x: abs(x["oi_pct"]), reverse=True)

    # Convert timestamps to IST for display
    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                clean = f"{base}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist = dt.hour * 60 + dt.minute + 330
            return f"{(ist//60)%24:02d}:{ist%60:02d}"
        except:
            return ts[11:16]

    return {
        "date":         today,
        "ts_new":       ts_new,
        "ts_old":       ts_old,
        "open_time":    to_ist(ts_old),
        "close_time":   to_ist(ts_new),
        "snapshots":    len(timestamps),
        "threshold":    threshold,
        "total_spikes": len(spikes),
        "spikes":       spikes[:100],
    }
