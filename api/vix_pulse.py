from utils.db import get_supabase
from datetime import datetime, timezone, timedelta

VIX_BASELINE_MINUTES = 25
VIX_SPIKE_THRESHOLD_PCT = 8.0

# Simple in-memory cache, same pattern as api/oi_pulse.py — avoids hammering
# Kite/Supabase on every poll (frontend polls every 30s)
_vix_cache: dict = {}
_vix_cache_time: float = 0
VIX_CACHE_TTL = 20  # seconds


def classify_zone(vix: float) -> dict:
    if vix < 12:
        return {"zone": "Complacent", "color": "green"}
    elif vix < 15:
        return {"zone": "Low", "color": "green"}
    elif vix < 20:
        return {"zone": "Normal", "color": "yellow"}
    elif vix < 25:
        return {"zone": "Elevated", "color": "orange"}
    elif vix < 30:
        return {"zone": "High Fear", "color": "red"}
    else:
        return {"zone": "Panic", "color": "red"}


def get_vix_pulse():
    import time as time_module
    global _vix_cache, _vix_cache_time

    now_epoch = time_module.time()
    if _vix_cache and (now_epoch - _vix_cache_time) < VIX_CACHE_TTL:
        return _vix_cache

    from services.kite_auth import get_kite_client
    kite = get_kite_client()
    supabase = get_supabase()

    quote = kite.quote(["NSE:INDIA VIX"])["NSE:INDIA VIX"]
    vix_value = quote["last_price"]
    prev_close = quote["ohlc"]["close"]
    change_pct = round((vix_value - prev_close) / prev_close * 100, 2) if prev_close else None
    now = datetime.now(timezone.utc)

    # throttle DB inserts to ~once/minute regardless of poll frequency
    last = (
        supabase.table("vix_snapshots")
        .select("timestamp")
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )
    should_insert = True
    if last.data:
        last_ts = datetime.fromisoformat(last.data[0]["timestamp"].replace("Z", "+00:00"))
        if (now - last_ts) < timedelta(seconds=55):
            should_insert = False

    if should_insert:
        supabase.table("vix_snapshots").insert({
            "timestamp": now.isoformat(),
            "vix_value": vix_value,
            "prev_close": prev_close,
            "change_pct": change_pct,
        }).execute()

    # rolling-baseline spike check
    baseline_cutoff = (now - timedelta(minutes=VIX_BASELINE_MINUTES)).isoformat()
    baseline_rows = (
        supabase.table("vix_snapshots")
        .select("vix_value, timestamp")
        .lte("timestamp", baseline_cutoff)
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )

    alert = None
    if baseline_rows.data:
        baseline_val = baseline_rows.data[0]["vix_value"]
        if baseline_val:
            move_pct = round((vix_value - baseline_val) / baseline_val * 100, 2)
            if abs(move_pct) >= VIX_SPIKE_THRESHOLD_PCT:
                alert = {
                    "type": "VIX_SPIKE" if move_pct > 0 else "VIX_COOLING",
                    "move_pct": move_pct,
                    "baseline_minutes_ago": VIX_BASELINE_MINUTES,
                }

    # last 3 hours for the intraday chart
    history_cutoff = (now - timedelta(hours=3)).isoformat()
    history = (
        supabase.table("vix_snapshots")
        .select("timestamp, vix_value")
        .gte("timestamp", history_cutoff)
        .order("timestamp")
        .execute()
    )

    zone = classify_zone(vix_value)
    result = {
        "vix_value": vix_value,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "day_high": quote["ohlc"]["high"],
        "day_low": quote["ohlc"]["low"],
        "zone": zone["zone"],
        "color": zone["color"],
        "alert": alert,
        "history": history.data,
        "timestamp": now.isoformat(),
    }
    _vix_cache = result
    _vix_cache_time = now_epoch
    return result


RANGE_DAYS = {"1m": 30, "3m": 90, "6m": 182, "1y": 365}


def get_vix_daily_history(range: str = "6m"):
    supabase = get_supabase()
    days = RANGE_DAYS.get(range, 182)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = (
        supabase.table("vix_daily_history")
        .select("date, open, high, low, close")
        .gte("date", cutoff)
        .order("date")
        .execute()
    )
    closes = [r["close"] for r in rows.data] if rows.data else []
    return {
        "range": range,
        "history": rows.data,
        "range_low": min(closes) if closes else None,
        "range_high": max(closes) if closes else None,
    }
