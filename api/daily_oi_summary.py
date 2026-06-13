"""
daily_oi_summary.py
Uses server-side RPC to avoid statement timeouts.
"""
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

def compute_daily_summary(supabase, trade_date: str = None) -> dict:
    try:
        if not trade_date:
            # Find last trading weekday
            d = datetime.now(IST).date()
            for _ in range(7):
                if d.weekday() < 5:
                    break
                d -= timedelta(days=1)
            trade_date = d.isoformat()

        print(f"[DAILY_OI_SUMMARY] Computing for {trade_date}")

        # ── Server-side aggregation via RPC ───────────────────────────────
        rpc_res = supabase.rpc(
            "compute_daily_oi_summary",
            {"p_trade_date": trade_date}
        ).execute()

        if not rpc_res.data:
            return {"error": f"No data for {trade_date}", "rows_written": 0}

        # ── Fetch CMP separately (small query) ────────────────────────────
        cmp_res = supabase.from_("cmp_prices") \
            .select("symbol,cmp") \
            .gte("timestamp", f"{trade_date}T00:00:00+00:00") \
            .lte("timestamp", f"{trade_date}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(500).execute()

        cmp_map = {}
        seen = set()
        for row in (cmp_res.data or []):
            sym = row["symbol"]
            if sym not in seen:
                cmp_map[sym] = {
                    "cmp": row.get("cmp"),
                    "price_chg_pct": None
                }
                seen.add(sym)

        # ── Build upsert rows ─────────────────────────────────────────────
        rows = []
        for r in rpc_res.data:
            sym = r["r_symbol"]
            cmp_data = cmp_map.get(sym, {})
            rows.append({
                "trade_date":    trade_date,
                "symbol":        sym,
                "total_oi":      r["r_total_oi"],
                "oi_chg_abs":    r["r_oi_chg_abs"],
                "oi_chg_pct":    r["r_oi_chg_pct"],
                "total_volume":  r["r_total_volume"],
                "vol_chg_abs":   r["r_vol_chg_abs"],
                "vol_chg_pct":   r["r_vol_chg_pct"],
                "close_price":   cmp_data.get("cmp"),
                "price_chg_pct": cmp_data.get("price_chg_pct"),
            })

        if not rows:
            return {"error": "No rows to write", "rows_written": 0}

        # ── Fetch FUT-only volume separately ──────────────────────────────
        fut_res = supabase.from_("oi_snapshots")\
            .select("symbol, volume")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{trade_date}T00:00:00+00:00")\
            .lte("timestamp", f"{trade_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(5000)\
            .execute()

        # Get max FUT volume per symbol (EOD snapshot)
        fut_vol_map = {}
        for r in (fut_res.data or []):
            sym = r["symbol"]
            vol = int(r.get("volume") or 0)
            if sym not in fut_vol_map or vol > fut_vol_map[sym]:
                fut_vol_map[sym] = vol

        # Add fut_vol and fut_oi_chg_pct to rows
        for row in rows:
            sym = row["symbol"]
            row["fut_vol"] = fut_vol_map.get(sym, 0)
            row["fut_oi_chg_pct"] = fut_oi_chg_map.get(sym, 0)

        supabase.from_("daily_oi_summary") \
            .upsert(rows, on_conflict="trade_date,symbol").execute()

        print(f"[DAILY_OI_SUMMARY] Wrote {len(rows)} rows for {trade_date}")
        return {"success": True, "trade_date": trade_date, "rows_written": len(rows)}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "rows_written": 0}
