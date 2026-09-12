"""
52-week high, computed from spot_daily_bars -- the daily OHLCV table
already backfilled via kite.historical_data() and kept current by a daily
EOD job (see api/spot_volume_scanner.py, built for the spot volume
scanner). Reused here rather than a new data source.
"""
import time as _time
from datetime import datetime, timedelta, timezone

_cache = None
_cache_time = 0
CACHE_TTL = 300  # 5 min -- daily bars don't change intraday, no need to
                 # recompute the full aggregation on every page load


def _fetch_all_bars(supabase, cutoff: str):
    rows = []
    offset = 0
    PAGE = 1000
    while True:
        batch = supabase.from_("spot_daily_bars").select("symbol,high,trade_date") \
            .gte("trade_date", cutoff).range(offset, offset + PAGE - 1).execute()
        if not batch.data:
            break
        rows.extend(batch.data)
        if len(batch.data) < PAGE:
            break
        offset += PAGE
    return rows


def _fallback_cmp_map(supabase):
    """cmp_map is normally the in-memory _last_cmp dict from main.py, which
    only fills in during live market-hours capture -- it's empty right
    after every deploy/restart, and always empty on weekends/holidays.
    Falls back to the latest cmp_prices row per symbol so this endpoint
    doesn't silently show every CMP as null until the next capture tick."""
    try:
        rows = supabase.from_("cmp_prices").select("symbol,cmp,timestamp") \
            .order("timestamp", desc=True).limit(500).execute().data or []
        seen = {}
        for r in rows:
            sym = r.get("symbol")
            if sym and sym not in seen and r.get("cmp"):
                seen[sym] = float(r["cmp"])
        return seen
    except Exception as e:
        print(f"[week52_high] cmp_prices fallback failed: {e}")
        return {}


def compute_52_week_high(supabase, cmp_map: dict):
    """Returns one row per symbol: week52_high, the date it was hit, current
    CMP, % distance from that high, and whether CMP is at/near it (within
    0.5%, since intraday CMP can tick a hair below the exact daily-bar high
    even on the day it's set)."""
    global _cache, _cache_time
    if not any(cmp_map.values()):
        cmp_map = _fallback_cmp_map(supabase)
    now = _time.time()
    if _cache is not None and (now - _cache_time) < CACHE_TTL:
        highs, high_dates = _cache
    else:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=365)).isoformat()
        rows = _fetch_all_bars(supabase, cutoff)
        highs: dict = {}
        high_dates: dict = {}
        for r in rows:
            sym = r["symbol"]
            h = r.get("high")
            if h is None:
                continue
            if sym not in highs or h > highs[sym]:
                highs[sym] = h
                high_dates[sym] = r["trade_date"]
        _cache = (highs, high_dates)
        _cache_time = now

    result = []
    for sym, high in highs.items():
        cmp_ = cmp_map.get(sym)
        pct_from_high = round((cmp_ - high) / high * 100, 2) if cmp_ and high else None
        at_high = pct_from_high is not None and pct_from_high >= -0.5
        result.append({
            "symbol": sym,
            "week52_high": round(high, 2),
            "week52_high_date": high_dates[sym],
            "cmp": cmp_,
            "pct_from_52w_high": pct_from_high,
            "at_52w_high": at_high,
        })
    return result
