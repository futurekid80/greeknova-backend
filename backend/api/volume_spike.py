from utils.db import get_supabase
from datetime import datetime, timezone

def get_volume_spikes(threshold: float = 50.0, date: str = None):
    supabase = get_supabase()

    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Filter by NIFTY to avoid row limit on timestamp query
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data))

    if len(timestamps) < 2:
        result = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .order("timestamp", desc=True)\
            .limit(100)\
            .execute()
        timestamps = sorted(set(r["timestamp"] for r in result.data), reverse=True)
        if len(timestamps) < 2:
            return {"error": "Need at least 2 snapshots", "spikes": []}
        ts_new = timestamps[0]
        ts_old = timestamps[1]
    else:
        ts_old = timestamps[-2]
        ts_new = timestamps[-1]

    # Paginated snapshot fetch
    def fetch_snapshot(ts):
        all_data = []
        for offset in range(0, 50000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .eq("timestamp", ts)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            all_data.extend(batch.data)
            if len(batch.data) < 1000:
                break
        return all_data

    new_data = fetch_snapshot(ts_new)
    old_data = fetch_snapshot(ts_old)

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

        old_vol = old_row["volume"] or 0
        new_vol = row["volume"] or 0
        old_oi  = old_row["oi"] or 0
        new_oi  = row["oi"] or 0

        if old_vol < 10000:
            continue

        vol_pct = round(((new_vol - old_vol) / old_vol * 100), 2) if old_vol > 0 else 0
        oi_pct  = round(((new_oi  - old_oi)  / old_oi  * 100), 2) if old_oi  > 0 else 0

        if vol_pct >= threshold:
            signal = "FRESH_BUILD" if oi_pct > 5 else "UNWINDING" if oi_pct < -5 else "CHURN"
            spikes.append({
                "symbol":        row["symbol"],
                "tradingsymbol": row["tradingsymbol"],
                "strike":        row["strike"],
                "option_type":   row["option_type"],
                "old_volume":    old_vol,
                "new_volume":    new_vol,
                "vol_pct":       vol_pct,
                "oi_pct":        oi_pct,
                "oi_signal":     signal,
                "last_price":    row["last_price"],
                "is_index":      row.get("is_index", False),
            })

    spikes.sort(key=lambda x: x["vol_pct"], reverse=True)

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
        "spikes":       spikes[:50],
    }