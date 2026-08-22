"""
daily_oi_summary.py
Uses server-side RPC to avoid statement timeouts.
Fix: FUT open/close OI now uses nearest expiry only — prevents cross-expiry
contamination that inflated fut_oi_chg_pct (e.g. HINDALCO showing +28% when
actual Jun30 expiry change was only +1.6%).
"""
from datetime import datetime, timedelta
from collections import defaultdict
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
        # ── Fetch official NSE settlement close via Kite historical_data ──
        official_close_map = {}
        try:
            from services.kite_auth import get_kite_client
            kite = get_kite_client()
            instruments = kite.instruments("NSE")
            token_map = {}
            INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "FINNIFTY": 257801}
            token_map.update(INDEX_TOKENS)
            for inst in instruments:
                if inst["tradingsymbol"] in SYMBOLS:
                    token_map[inst["tradingsymbol"]] = inst["instrument_token"]

            import time as _time
            for sym in SYMBOLS:
                token = token_map.get(sym)
                if not token:
                    continue
                try:
                    candles = kite.historical_data(
                        instrument_token=token,
                        from_date=trade_date,
                        to_date=trade_date,
                        interval="day",
                        continuous=False,
                        oi=False,
                    )
                    for c in candles:
                        if str(c["date"])[:10] == trade_date:
                            official_close_map[sym] = float(c["close"])
                            break
                    _time.sleep(0.05)
                except Exception as e:
                    print(f"[DailyOI] Kite close {sym}: {e}")
        except Exception as e:
            print(f"[DailyOI] Kite init failed: {e}")

       # ── Fetch official NSE settlement close via Kite ──────────────────
        official_close_map = {}
        try:
            from services.kite_auth import get_kite_client
            import time as _time
            kite = get_kite_client()
            instruments = kite.instruments("NSE")
            token_map = {**{"NIFTY": 256265, "BANKNIFTY": 260105, "FINNIFTY": 257801}}
            for inst in instruments:
                if inst["tradingsymbol"] in SYMBOLS:
                    token_map[inst["tradingsymbol"]] = inst["instrument_token"]
            for sym in SYMBOLS:
                token = token_map.get(sym)
                if not token:
                    continue
                try:
                    candles = kite.historical_data(
                        instrument_token=token,
                        from_date=trade_date,
                        to_date=trade_date,
                        interval="day",
                        continuous=False,
                        oi=False,
                    )
                    for c in candles:
                        if str(c["date"])[:10] == trade_date:
                            official_close_map[sym] = float(c["close"])
                            break
                    _time.sleep(0.05)
                except Exception as e:
                    print(f"[DailyOI] Kite close {sym}: {e}")
        except Exception as e:
            print(f"[DailyOI] Kite init failed: {e}")

        rpc_res = supabase.rpc("compute_daily_oi_summary", {
            "p_trade_date": trade_date,
        }).execute()

        if not rpc_res.data:
            return {"error": f"No data for {trade_date}", "rows_written": 0}

        # ── Fetch CMP separately (small query) ────────────────────────────
        cmp_res = supabase.from_("cmp_prices") \
            .select("symbol,cmp") \
            .gte("timestamp", f"{trade_date}T00:00:00+00:00") \
            .lte("timestamp", f"{trade_date}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(500).execute()

        # Get previous trading day's close — use last available date from DB (handles holidays)
        try:
            prev_res = supabase.from_("cmp_prices")\
                .select("timestamp")\
                .lt("timestamp", f"{trade_date}T00:00:00+00:00")\
                .order("timestamp", desc=True)\
                .limit(1)\
                .execute()
            if prev_res.data:
                import pytz as _pytz
                _ist = _pytz.timezone('Asia/Kolkata')
                from datetime import datetime as _dt2
                _raw_ts = prev_res.data[0]["timestamp"]
                _dt_obj = _dt2.fromisoformat(_raw_ts.replace("Z", "+00:00")).astimezone(_ist)
                prev_date = _dt_obj.strftime('%Y-%m-%d')
            else:
                raise Exception("no prev data")
        except:
            from datetime import datetime as _dt2
            trade_dt = _dt2.strptime(trade_date, '%Y-%m-%d')
            prev_date = (trade_dt - timedelta(days=3)).strftime('%Y-%m-%d')

        prev_cmp_res = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{prev_date}T00:00:00+00:00")\
            .lte("timestamp", f"{prev_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(500).execute()

        prev_cmp_map = {}
        seen_prev = set()
        for row in (prev_cmp_res.data or []):
            if row["symbol"] not in seen_prev:
                prev_cmp_map[row["symbol"]] = float(row["cmp"])
                seen_prev.add(row["symbol"])

        cmp_map = {}
        seen = set()
        for row in (cmp_res.data or []):
            sym = row["symbol"]
            if sym not in seen:
                curr = float(row.get("cmp") or 0)
                prev = prev_cmp_map.get(sym, 0)
                price_chg = round((curr - prev) / prev * 100, 2) if prev > 0 else None
                cmp_map[sym] = {
                    "cmp": row.get("cmp"),
                    "price_chg_pct": price_chg
                }
                seen.add(sym)

        # ── Build upsert rows ─────────────────────────────────────────────
        rows = []
        def cap_pct(val, limit=9999.99):
            """Cap percentage values to avoid numeric overflow on new series day."""
            if val is None: return None
            try: return max(-limit, min(limit, float(val)))
            except: return None

        for r in rpc_res.data:
            sym = r["r_symbol"]
            cmp_data = cmp_map.get(sym, {})
            rows.append({
                "trade_date":    trade_date,
                "symbol":        sym,
                "total_oi":      r["r_total_oi"],
                "oi_chg_abs":    r["r_oi_chg_abs"],
                "oi_chg_pct":    cap_pct(r["r_oi_chg_pct"]),
                "total_volume":  r["r_total_volume"],
                "vol_chg_abs":   r["r_vol_chg_abs"],
                "vol_chg_pct":   cap_pct(r["r_vol_chg_pct"]),
                "close_price":   official_close_map.get(sym) or cmp_data.get("cmp"),
                "price_chg_pct": cap_pct(cmp_data.get("price_chg_pct")),
            })

        if not rows:
            return {"error": "No rows to write", "rows_written": 0}

        # ── Fetch FUT open snapshot (9:15-9:20 AM IST = 03:45-03:50 UTC) ─
        # Include expiry so we can filter to nearest expiry only
        fut_open_res = supabase.from_("oi_snapshots")\
            .select("symbol, oi, volume, expiry")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{trade_date}T03:44:00+00:00")\
            .lte("timestamp", f"{trade_date}T03:52:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(1000)\
            .execute()

        # ── Fetch FUT close snapshot (3:25-3:30 PM IST = 09:55-10:00 UTC) ─
        # Include expiry so we can filter to nearest expiry only
        fut_close_res = supabase.from_("oi_snapshots")\
            .select("symbol, oi, volume, expiry")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{trade_date}T09:50:00+00:00")\
            .lte("timestamp", f"{trade_date}T10:05:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1000)\
            .execute()

        # ── Build nearest AND next-nearest expiry maps from open snapshot ──
        # (Aug 22 2026): also track each symbol's SECOND-nearest expiry, so
        # we can compute a next-month OI change % alongside the existing
        # near-month one. Purpose: expiry-week OI spikes in the near-month
        # contract are often just rollover (positions shifting from near to
        # next), not genuine new conviction. Showing both months side by
        # side lets Stealth Buildup distinguish the two -- a real buildup
        # tends to show up in next-month too, or in the combined total;
        # pure rollover mostly cancels out when the two are summed.
        fut_all_expiries: dict = defaultdict(set)
        for r in (fut_open_res.data or []):
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if exp and exp >= trade_date:
                fut_all_expiries[sym].add(exp)

        fut_nearest_expiry = {}
        fut_next_expiry = {}
        for sym, exps in fut_all_expiries.items():
            sorted_exps = sorted(exps)
            fut_nearest_expiry[sym] = sorted_exps[0]
            if len(sorted_exps) > 1:
                fut_next_expiry[sym] = sorted_exps[1]

        # ── Build open OI maps — nearest and next-nearest expiry ─────────
        fut_open_map = {}
        fut_open_map_next = {}
        for r in (fut_open_res.data or []):
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if exp == fut_nearest_expiry.get(sym) and sym not in fut_open_map:
                fut_open_map[sym] = int(r.get("oi") or 0)
            elif exp == fut_next_expiry.get(sym) and sym not in fut_open_map_next:
                fut_open_map_next[sym] = int(r.get("oi") or 0)

        # ── Build close OI + volume maps — nearest and next-nearest ──────
        # Use same expiry maps as open for consistency (apples-to-apples)
        fut_close_oi_map = {}
        fut_close_oi_map_next = {}
        fut_vol_map = {}
        seen_close = set()
        seen_close_next = set()
        for r in (fut_close_res.data or []):
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if exp == fut_nearest_expiry.get(sym) and sym not in seen_close:
                fut_close_oi_map[sym] = int(r.get("oi") or 0)
                fut_vol_map[sym] = int(r.get("volume") or 0)
                seen_close.add(sym)
            elif exp == fut_next_expiry.get(sym) and sym not in seen_close_next:
                fut_close_oi_map_next[sym] = int(r.get("oi") or 0)
                seen_close_next.add(sym)

        # ── Compute FUT OI change % — near-month and next-month ───────────
        fut_oi_chg_map = {}
        for sym in fut_close_oi_map:
            open_oi  = fut_open_map.get(sym, 0)
            close_oi = fut_close_oi_map[sym]
            if open_oi > 0:
                fut_oi_chg_map[sym] = round((close_oi - open_oi) / open_oi * 100, 2)

        fut_oi_chg_map_next = {}
        for sym in fut_close_oi_map_next:
            open_oi  = fut_open_map_next.get(sym, 0)
            close_oi = fut_close_oi_map_next[sym]
            if open_oi > 0:
                fut_oi_chg_map_next[sym] = round((close_oi - open_oi) / open_oi * 100, 2)

        # ── Add fut_vol, fut_oi_chg_pct (+next) and fut_signal to rows ───
        for row in rows:
            sym = row["symbol"]
            fut_oi = fut_oi_chg_map.get(sym, 0)
            price  = row.get("price_chg_pct") or 0
            row["fut_vol"]        = fut_vol_map.get(sym, 0)
            row["fut_oi_chg_pct"] = fut_oi
            row["fut_oi_chg_pct_next"] = fut_oi_chg_map_next.get(sym, None)
            # Classify FUT signal — same logic as OI Buildup chart
            if fut_oi >= 2.0 and price >= 0.3:
                row["fut_signal"] = "LONG_BUILDUP"
            elif fut_oi >= 2.0 and price <= -0.3:
                row["fut_signal"] = "SHORT_BUILDUP"
            elif fut_oi <= -2.0 and price >= 0.3:
                row["fut_signal"] = "SHORT_COVERING"
            elif fut_oi <= -2.0 and price <= -0.3:
                row["fut_signal"] = "LONG_UNWINDING"
            else:
                row["fut_signal"] = "NEUTRAL"

        supabase.from_("daily_oi_summary") \
            .upsert(rows, on_conflict="trade_date,symbol").execute()

        print(f"[DAILY_OI_SUMMARY] Wrote {len(rows)} rows for {trade_date}")
        return {"success": True, "trade_date": trade_date, "rows_written": len(rows)}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "rows_written": 0}
