from utils.db import get_supabase

def get_volume_spikes(threshold: float = 50.0):
    supabase = get_supabase()

    result = supabase.from_("oi_snapshots").select("timestamp").order("timestamp", desc=True).limit(1000).execute()
    timestamps = sorted(set(r["timestamp"] for r in result.data), reverse=True)

    if len(timestamps) < 2:
        return {"error": "Need at least 2 snapshots", "spikes": []}

    ts_new = timestamps[0]
    ts_old = timestamps[1]

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

        old_vol = old_row["volume"] or 0
        new_vol = row["volume"] or 0
        old_oi = old_row["oi"] or 0
        new_oi = row["oi"] or 0

        if old_vol < 10000:
            continue

        vol_pct = ((new_vol - old_vol) / old_vol * 100) if old_vol > 0 else 0
        oi_pct = ((new_oi - old_oi) / old_oi * 100) if old_oi > 0 else 0

        if vol_pct >= threshold:
            signal = "FRESH_BUILD" if oi_pct > 5 else "UNWINDING" if oi_pct < -5 else "CHURN"
            spikes.append({
                "symbol": row["symbol"],
                "tradingsymbol": row["tradingsymbol"],
                "strike": row["strike"],
                "option_type": row["option_type"],
                "old_volume": old_vol,
                "new_volume": new_vol,
                "vol_pct": round(vol_pct, 2),
                "oi_pct": round(oi_pct, 2),
                "oi_signal": signal,
                "last_price": row["last_price"],
                "is_index": row.get("is_index", False),
            })

    spikes.sort(key=lambda x: x["vol_pct"], reverse=True)
    return {
        "ts_new": ts_new,
        "ts_old": ts_old,
        "threshold": threshold,
        "total_spikes": len(spikes),
        "spikes": spikes[:50]
    }
