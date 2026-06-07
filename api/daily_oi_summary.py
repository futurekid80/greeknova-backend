"""
daily_oi_summary.py
Computes and stores EOD OI + Volume summary for all symbols.
Runs daily at 4:45 PM IST via scheduler in main.py.
Also used for weekly/monthly Vol+OI breakout comparisons.
"""

from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

def get_last_trading_date(supabase) -> str | None:
    """Walk back up to 7 days to find last date with OI data."""
    for i in range(7):
        d = (datetime.now(IST) - timedelta(days=i)).strftime("%Y-%m-%d")
        res = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .gte("timestamp", f"{d}T00:00:00+00:00") \
            .lte("timestamp", f"{d}T23:59:59+00:00") \
            .limit(1).execute()
        if res.data:
            return d
    return None

def compute_daily_summary(supabase, trade_date: str = None) -> dict:
    """
    For a given trade_date, compute EOD OI + Volume for all symbols
    and upsert into daily_oi_summary table.
    """
    try:
        if not trade_date:
            trade_date = get_last_trading_date(supabase)
        if not trade_date:
            return {"error": "No trading data found", "rows_written": 0}

        print(f"[DAILY_OI_SUMMARY] Computing for {trade_date}")

        # ── Fetch all EOD snapshots for this date ─────────────────────────
        # Get the latest timestamp of the day (EOD snapshot)
        ts_res = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .gte("timestamp", f"{trade_date}T09:00:00+00:00") \
            .lte("timestamp", f"{trade_date}T11:00:00+00:00") \
            .order("timestamp", desc=True) \
            .limit(1).execute()

        if not ts_res.data:
            return {"error": f"No EOD snapshot found for {trade_date}", "rows_written": 0}

        eod_ts = ts_res.data[0]["timestamp"]

        # Get first snapshot of the day (open)
        open_ts_res = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .gte("timestamp", f"{trade_date}T09:00:00+00:00") \
            .lte("timestamp", f"{trade_date}T11:00:00+00:00") \
            .order("timestamp", desc=False) \
            .limit(1).execute()

        open_ts = open_ts_res.data[0]["timestamp"] if open_ts_res.data else eod_ts

        print(f"[DAILY_OI_SUMMARY] EOD ts={eod_ts}, Open ts={open_ts}")

        # ── Fetch EOD OI data (all symbols, options only) ─────────────────
        eod_res = supabase.from_("oi_snapshots") \
            .select("symbol,option_type,oi,volume") \
            .eq("timestamp", eod_ts) \
            .in_("option_type", ["CE", "PE"]) \
            .limit(10000).execute()

        # ── Fetch Open OI data ────────────────────────────────────────────
        open_res = supabase.from_("oi_snapshots") \
            .select("symbol,option_type,oi,volume") \
            .eq("timestamp", open_ts) \
            .in_("option_type", ["CE", "PE"]) \
            .limit(10000).execute()

        # ── Fetch CMP data ────────────────────────────────────────────────
        cmp_res = supabase.from_("cmp_prices") \
            .select("symbol,cmp,price_chg_pct") \
            .gte("timestamp", f"{trade_date}T00:00:00+00:00") \
            .lte("timestamp", f"{trade_date}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(500).execute()

        # ── Aggregate EOD OI by symbol ────────────────────────────────────
        eod_by_symbol: dict[str, dict] = {}
        for row in (eod_res.data or []):
            sym = row["symbol"]
            if sym not in eod_by_symbol:
                eod_by_symbol[sym] = {"oi": 0, "volume": 0}
            eod_by_symbol[sym]["oi"]     += row["oi"] or 0
            eod_by_symbol[sym]["volume"] += row["volume"] or 0

        # ── Aggregate Open OI by symbol ───────────────────────────────────
        open_by_symbol: dict[str, dict] = {}
        for row in (open_res.data or []):
            sym = row["symbol"]
            if sym not in open_by_symbol:
                open_by_symbol[sym] = {"oi": 0, "volume": 0}
            open_by_symbol[sym]["oi"]     += row["oi"] or 0
            open_by_symbol[sym]["volume"] += row["volume"] or 0

        # ── CMP map (latest per symbol) ───────────────────────────────────
        cmp_map: dict[str, dict] = {}
        seen = set()
        for row in (cmp_res.data or []):
            sym = row["symbol"]
            if sym not in seen:
                cmp_map[sym] = {
                    "cmp": row.get("cmp"),
                    "price_chg_pct": row.get("price_chg_pct")
                }
                seen.add(sym)

        # ── Build upsert rows ─────────────────────────────────────────────
        rows = []
        for sym, eod in eod_by_symbol.items():
            open_data = open_by_symbol.get(sym, {"oi": 0, "volume": 0})
            eod_oi    = eod["oi"]
            open_oi   = open_data["oi"]
            eod_vol   = eod["volume"]
            open_vol  = open_data["volume"]

            oi_chg_abs  = eod_oi  - open_oi
            vol_chg_abs = eod_vol - open_vol
            oi_chg_pct  = round((oi_chg_abs / open_oi * 100), 2) if open_oi > 0 else 0
            vol_chg_pct = round((vol_chg_abs / open_vol * 100), 2) if open_vol > 0 else 0

            cmp_data = cmp_map.get(sym, {})

            rows.append({
                "trade_date":    trade_date,
                "symbol":        sym,
                "total_oi":      eod_oi,
                "oi_chg_abs":    oi_chg_abs,
                "oi_chg_pct":    oi_chg_pct,
                "total_volume":  eod_vol,
                "vol_chg_abs":   vol_chg_abs,
                "vol_chg_pct":   vol_chg_pct,
                "close_price":   cmp_data.get("cmp"),
                "price_chg_pct": cmp_data.get("price_chg_pct"),
            })

        if not rows:
            return {"error": "No rows to write", "rows_written": 0}

        # ── Upsert into Supabase ──────────────────────────────────────────
        supabase.from_("daily_oi_summary") \
            .upsert(rows, on_conflict="trade_date,symbol").execute()

        print(f"[DAILY_OI_SUMMARY] Wrote {len(rows)} rows for {trade_date}")
        return {"success": True, "trade_date": trade_date, "rows_written": len(rows)}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "rows_written": 0}
