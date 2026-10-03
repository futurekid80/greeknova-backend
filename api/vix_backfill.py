"""One-off / rerunnable VIX history backfill.

Daily closes: pulled in ~5-year chunks (Kite's day-interval cap), covering
as far back as Kite has data for INDIA VIX, upserted into vix_daily_history.

Intraday: pulled in 60-day chunks (Kite's minute-interval cap) and upserted
into vix_snapshots, seeding the intraday chart instead of starting from
whenever the live /vix-pulse endpoint was first deployed.

Safe to rerun — both writes are upserts keyed on date / timestamp, so this
can also be used later to catch up if snapshot capture has a gap.
"""
from utils.db import get_supabase
from datetime import datetime, timedelta, timezone

INDIA_VIX_TOKEN = 264969


def backfill_vix_history(daily_years: int = 5, intraday_days: int = 60):
    from services.kite_auth import get_kite_client
    kite = get_kite_client()
    supabase = get_supabase()

    result = {"daily_rows": 0, "intraday_rows": 0, "errors": []}

    # ---- Daily closes, chunked (Kite caps day-interval requests around 2000 days) ----
    end = datetime.now()
    start_overall = end - timedelta(days=daily_years * 365)
    chunk_start = start_overall
    daily_rows = []
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=1900), end)
        try:
            candles = kite.historical_data(
                INDIA_VIX_TOKEN,
                chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                "day",
            )
            for c in candles:
                daily_rows.append({
                    "date": c["date"].strftime("%Y-%m-%d") if hasattr(c["date"], "strftime") else str(c["date"])[:10],
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                })
        except Exception as e:
            result["errors"].append(f"daily {chunk_start.date()}-{chunk_end.date()}: {e}")
        chunk_start = chunk_end

    if daily_rows:
        for i in range(0, len(daily_rows), 500):
            supabase.table("vix_daily_history").upsert(daily_rows[i:i + 500], on_conflict="date").execute()
        result["daily_rows"] = len(daily_rows)

    # ---- Intraday minute candles, chunked (Kite caps minute-interval at 60 days) ----
    intraday_rows = []
    end2 = datetime.now()
    chunk_start = end2 - timedelta(days=intraday_days)
    while chunk_start < end2:
        chunk_end = min(chunk_start + timedelta(days=58), end2)
        try:
            candles = kite.historical_data(
                INDIA_VIX_TOKEN,
                chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                "minute",
            )
            prev_close = None
            for c in candles:
                ts = c["date"]
                ts_iso = ts.astimezone(timezone.utc).isoformat() if hasattr(ts, "astimezone") else str(ts)
                change_pct = round((c["close"] - prev_close) / prev_close * 100, 2) if prev_close else None
                intraday_rows.append({
                    "timestamp": ts_iso,
                    "vix_value": c["close"],
                    "prev_close": prev_close,
                    "change_pct": change_pct,
                })
                prev_close = c["close"]
        except Exception as e:
            result["errors"].append(f"intraday {chunk_start.date()}-{chunk_end.date()}: {e}")
        chunk_start = chunk_end

    if intraday_rows:
        for i in range(0, len(intraday_rows), 500):
            supabase.table("vix_snapshots").upsert(intraday_rows[i:i + 500], on_conflict="timestamp").execute()
        result["intraday_rows"] = len(intraday_rows)

    return result
