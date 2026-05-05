from utils.db import get_supabase

def get_oi_spikes(threshold: float = 10.0):
    supabase = get_supabase()

    # Get last 2 distinct timestamps
    result = supabase.from_("oi_snapshots").select("timestamp").order("timestamp", desc=True).limit(1000).execute()
    timestamps = sorted(set(r["timestamp"] for r in result.data), reverse=True)

    if len(timestamps) < 2:
        return {"error": "Need at least 2 snapshots", "spikes": []}

    ts_new = timestamps[0]
    ts_old = timestamps[1]

    # Fetch both snapshots
    new_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_new).execute().data
    old_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_old).execute().data

    # Build lookup for old data
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

        if old_oi < 1000:  # ignore tiny OI
            continue

        oi_change = new_oi - old_oi
        oi_pct = (oi_change / old_oi * 100) if old_oi > 0 else 0

        vol_change = (row["volume"] or 0) - (old_row["volume"] or 0)

        if abs(oi_pct) >= threshold:
            spikes.append({
                "symbol": row["symbol"],
                "tradingsymbol": row["tradingsymbol"],
                "strike": row["strike"],
                "option_type": row["option_type"],
                "old_oi": old_oi,
                "new_oi": new_oi,
                "oi_change": oi_change,
                "oi_pct": round(oi_pct, 2),
                "volume": row["volume"],
                "vol_change": vol_change,
                "last_price": row["last_price"],
                "is_index": row.get("is_index", False),
                "direction": "BUILD" if oi_change > 0 else "UNWIND",
            })

    # Sort by absolute % change
    spikes.sort(key=lambda x: abs(x["oi_pct"]), reverse=True)

    return {
        "ts_new": ts_new,
        "ts_old": ts_old,
        "threshold": threshold,
        "total_spikes": len(spikes),
        "spikes": spikes[:100]  # top 100
    }
