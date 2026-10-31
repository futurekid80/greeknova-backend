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

# BUG FIX (Sep 16 2026): this file used SYMBOLS below without ever importing
# it -- a real NameError waiting to happen every time this code path ran
# (compute_daily_summary is called live from main.py). Found while auditing
# every SYMBOLS usage in the repo as part of the live-F&O-universe fix.
from api.iv_analysis import SYMBOLS

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
        # ── Official close, trade_date's own session ──────────────────────
        # FIX (Oct 23 2026): kite.historical_data() has no real permission
        # on this API key (same root cause as the CPR/Wolves fixes earlier
        # today -- see api/cpr.py and services/wolves_scanner.py). This used
        # to call it TWICE (one block's result was immediately discarded by
        # the other -- dead code, ~20s of wasted kite.instruments()+per-symbol
        # historical_data() calls for a map that was overwritten) on top of
        # being broken. Replaced with a single cmp_prices-based close --
        # last tick of trade_date's own session, same pattern as cpr.py's
        # prev-day close.
        official_close_map = {}
        try:
            day_start = f"{trade_date}T00:00:00+00:00"
            from datetime import datetime as _dt, timedelta as _td
            day_end = (_dt.strptime(trade_date, "%Y-%m-%d") + _td(days=1)).isoformat() + "T00:00:00+00:00"
            for sym in SYMBOLS:
                try:
                    rows = supabase.from_("cmp_prices")\
                        .select("cmp, timestamp")\
                        .eq("symbol", sym)\
                        .gte("timestamp", day_start)\
                        .lt("timestamp", day_end)\
                        .order("timestamp")\
                        .limit(2000)\
                        .execute()
                    cmps = [float(r["cmp"]) for r in (rows.data or []) if r.get("cmp")]
                    if cmps:
                        official_close_map[sym] = cmps[-1]
                except Exception as e:
                    print(f"[DailyOI] cmp_prices close {sym}: {e}")
        except Exception as e:
            print(f"[DailyOI] Official close computation failed: {e}")

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

        # BUG FIX (Oct 5 2026): price_chg_pct used to be computed purely from
        # cmp_prices -- two intraday/EOD LTP *snapshots* (today's last poll
        # vs. yesterday's last poll), not NSE's official close-to-close
        # move. Same bug class already fixed in api/cpr.py, api/oi_pulse.py,
        # api/vol_oi_breakout.py and api/positional_intelligence.py: NSE's
        # official close is set by a post-15:30 closing auction and can
        # differ meaningfully from the last continuous-session LTP.
        # Confirmed live: this exact bug was still inflating ANGELONE/BSE/
        # KALYANKJIL's scanner % change to +4%+ even after those four other
        # fixes, because this is where the post-market scanner (_get_eod_pulse
        # in oi_pulse.py) actually reads price_chg_pct from -- a precomputed,
        # stored column, written once daily by this very function and never
        # recomputed live. Use the official close (official_close_map,
        # already fetched above via Kite's historical_data) against
        # cpr_levels.prev_close (also Kite-official, computed by the EOD CPR
        # job) instead of cmp_prices snapshots on either side.
        cpr_prev_res = supabase.from_("cpr_levels")\
            .select("symbol, prev_close")\
            .eq("trade_date", trade_date)\
            .execute()
        cpr_prev_close_map = {
            r["symbol"]: float(r["prev_close"])
            for r in (cpr_prev_res.data or [])
            if r.get("prev_close") is not None
        }

        # Fallback prev-close source (cmp_prices LTP) only for a symbol
        # cpr_levels doesn't have yet (e.g. not covered by the EOD CPR job).
        # Still uses the last available date in the DB so it naturally
        # skips weekends/holidays with no data.
        prev_cmp_map = {}
        missing_for_fallback = [s for s in SYMBOLS if s not in cpr_prev_close_map]
        if missing_for_fallback:
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

            seen_prev = set()
            for row in (prev_cmp_res.data or []):
                sym = row["symbol"]
                if sym in missing_for_fallback and sym not in seen_prev:
                    prev_cmp_map[sym] = float(row["cmp"])
                    seen_prev.add(sym)

        # BUG FIX (Oct 5 2026, follow-up): official_close_map was meant to be
        # TODAY's (trade_date's) official close via Kite's historical_data,
        # but a manual recompute run shortly after market close showed it
        # returning the PREVIOUS trading day's close instead (ANGELONE came
        # back as exactly 283.75, Oct 1's close, with price_chg_pct=0.00 for
        # every symbol tested) -- Kite's EOD daily candle for "today" is
        # evidently not published via the historical API this soon after
        # close (a data-vendor settlement lag), so the from_date=to_date=
        # trade_date query was silently resolving to the last candle it did
        # have. Rather than depend on same-day Kite EOD data that may not
        # exist yet, use the latest intraday LTP snapshot from cmp_prices as
        # "today's close" (close enough to the real closing price, as
        # established earlier today: at most a ~1% gap) -- the same source
        # that already worked correctly for this before today's change.

        # BUG FIX (Oct 18 2026): the PREVIOUS close side was NOT unaffected
        # like the comment above claims -- cpr_levels.prev_close comes from
        # a different job (the EOD CPR computation) and was found to drift
        # from this table's own close_price history by 1-2.5 rupees on
        # ordinary back-to-back trading days (360ONE: cpr_levels.prev_close
        # for Oct 16 2026 was 1071.0, but this table's own close_price for
        # Oct 15 2026 -- the real previous trading day -- was 1073.00,
        # which matches what Kite itself was showing as the prior close).
        # That ~0.19% drift was compounding into a visible mismatch between
        # GreekNova's displayed day-change% and Kite's for the same stock
        # (e.g. 360ONE showed 0.37% here vs Kite's 0.16% on Oct 16 2026).
        # Use THIS table's own close_price for the previous trading day as
        # the primary prev-close source instead -- it's the one GreekNova
        # itself will compare today's close against tomorrow, so using it
        # both days keeps the chain self-consistent, and it already lines
        # up with Kite in practice. cpr_levels.prev_close and the cmp_prices
        # fallback remain as fallbacks for a symbol with no stored prior row
        # (e.g. a new listing).
        own_prev_close_map = {}
        try:
            from utils.market_calendar import get_prev_trading_day
            _prev_trade_date = get_prev_trading_day(
                datetime.strptime(trade_date, '%Y-%m-%d').date()
            ).isoformat()
            _own_prev_res = supabase.from_("daily_oi_summary")                .select("symbol, close_price")                .eq("trade_date", _prev_trade_date)                .execute()
            own_prev_close_map = {
                r["symbol"]: float(r["close_price"])
                for r in (_own_prev_res.data or [])
                if r.get("close_price") is not None
            }
        except Exception as e:
            print(f"[DAILY_OI_SUMMARY] own-prev-close fetch failed: {e}")

        cmp_map = {}
        seen = set()
        for row in (cmp_res.data or []):
            sym = row["symbol"]
            if sym not in seen:
                curr = float(row.get("cmp") or 0)
                prev = own_prev_close_map.get(sym) or cpr_prev_close_map.get(sym) or prev_cmp_map.get(sym, 0)
                price_chg = round((curr - prev) / prev * 100, 2) if prev > 0 and curr > 0 else None
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
                "close_price":   cmp_data.get("cmp") or official_close_map.get(sym),
                "price_chg_pct": cap_pct(cmp_data.get("price_chg_pct")),
            })

        if not rows:
            return {"error": "No rows to write", "rows_written": 0}

        # ── Fetch FUT open snapshot (9:15-9:20 AM IST = 03:45-03:50 UTC) ─
        # Include expiry so we can filter to nearest expiry only
        # Include last_price so we can derive a FUT-based price change (see
        # fut_price_chg_map below) instead of mixing cash price with FUT OI.
        fut_open_res = supabase.from_("oi_snapshots")\
            .select("symbol, oi, volume, expiry, last_price")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{trade_date}T03:44:00+00:00")\
            .lte("timestamp", f"{trade_date}T03:52:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(1000)\
            .execute()

        # ── Fetch FUT close snapshot (3:25-3:30 PM IST = 09:55-10:00 UTC) ─
        # Include expiry so we can filter to nearest expiry only
        fut_close_res = supabase.from_("oi_snapshots")\
            .select("symbol, oi, volume, expiry, last_price")\
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

        # ── Build open OI + price maps — nearest and next-nearest expiry ──
        fut_open_map = {}
        fut_open_map_next = {}
        fut_open_price_map = {}
        for r in (fut_open_res.data or []):
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if exp == fut_nearest_expiry.get(sym) and sym not in fut_open_map:
                fut_open_map[sym] = int(r.get("oi") or 0)
                fut_open_price_map[sym] = float(r.get("last_price") or 0)
            elif exp == fut_next_expiry.get(sym) and sym not in fut_open_map_next:
                fut_open_map_next[sym] = int(r.get("oi") or 0)

        # ── Build close OI + volume + price maps — nearest and next-nearest ─
        # Use same expiry maps as open for consistency (apples-to-apples)
        fut_close_oi_map = {}
        fut_close_oi_map_next = {}
        fut_vol_map = {}
        fut_close_price_map = {}
        seen_close = set()
        seen_close_next = set()
        for r in (fut_close_res.data or []):
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if exp == fut_nearest_expiry.get(sym) and sym not in seen_close:
                fut_close_oi_map[sym] = int(r.get("oi") or 0)
                fut_vol_map[sym] = int(r.get("volume") or 0)
                fut_close_price_map[sym] = float(r.get("last_price") or 0)
                seen_close.add(sym)
            elif exp == fut_next_expiry.get(sym) and sym not in seen_close_next:
                fut_close_oi_map_next[sym] = int(r.get("oi") or 0)
                seen_close_next.add(sym)

        # ── FUT-based price change % (open→close), nearest expiry ────────
        # BUG FIX (Oct 2026): price_chg_pct below was sourced purely from
        # cmp_prices (CASH price) but then classified together with
        # fut_oi_chg_pct (FUTURES OI) in fut_signal -- a real mismatch since
        # FUT and cash can diverge (basis, especially in the last days before
        # expiry). Prefer the FUT contract's own open->close price change;
        # fall back to the cash price_chg_pct already in `rows` only when a
        # FUT price isn't available for that symbol/day.
        fut_price_chg_map = {}
        for sym, close_price in fut_close_price_map.items():
            open_price = fut_open_price_map.get(sym, 0)
            if open_price > 0 and close_price > 0:
                fut_price_chg_map[sym] = round((close_price - open_price) / open_price * 100, 2)

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

        # ── Add fut_vol, fut_oi_chg_pct (+next), fut_oi_close (+next), fut_signal ───
        # BUG FIX (Oct 9 2026): this loop used to run unconditionally and
        # always set row["fut_price_chg_pct"] etc, even when fut_open_res/
        # fut_close_res came back with zero rows (e.g. this job firing mid
        # deploy-cutover, right as the old process is being replaced, or any
        # other transient query hiccup). Since the write below is an upsert
        # that replaces the whole row, a single bad run would silently null
        # out a PREVIOUSLY GOOD fut_price_chg_pct for every symbol that day --
        # and because Stealth Buildup's post-market branch in
        # positional_intelligence.py treats a None fut_price_chg_pct as
        # "skip this stock", the entire Stealth Buildup panel would quietly
        # go empty for the rest of the day with no error anywhere. On a
        # normal trading day both snapshot queries return hundreds of rows,
        # so an empty result here is itself the signal something's wrong --
        # skip the FUT-derived fields (leave existing values untouched)
        # rather than writing nulls over good data.
        skip_fut_fields = not (fut_open_res.data and fut_close_res.data)
        if skip_fut_fields:
            print(f"[DAILY_OI_SUMMARY] WARNING: FUT open/close snapshot query came back "
                  f"empty (open={len(fut_open_res.data or [])} rows, "
                  f"close={len(fut_close_res.data or [])} rows) for {trade_date} -- "
                  f"skipping fut_price_chg_pct/fut_oi_chg_pct/fut_signal this run so "
                  f"existing values aren't overwritten with nulls.")
        for row in rows:
            if skip_fut_fields:
                continue
            sym = row["symbol"]
            fut_oi = fut_oi_chg_map.get(sym, 0)
            # BUG FIX (Oct 5 2026): this used to overwrite row["price_chg_pct"]
            # -- the field the scanner displays as the stock's day change --
            # with the FUT contract's own 9:15->15:30 open/close move. That's
            # a futures-basis intraday swing, not the cash market's
            # close-to-close % change, and it was the single biggest source
            # of the inflated ANGELONE/BSE/KALYANKJIL scanner numbers (a FUT
            # contract can easily swing several % intraday on basis/rollover
            # even when the underlying barely moved day-to-day). Keep using
            # the FUT open->close move ONLY for fut_signal classification
            # (that's the "Oct 2026" fix's actual intent -- keeping the
            # signal consistent with the FUT OI it's paired with), via a
            # local variable, and leave row["price_chg_pct"] as the correct
            # cash close-to-close value computed above.
            #
            # BUG FIX (Oct 18 2026): reverting the Oct 6 2026 change above.
            # fut_price_chg_map is the FUT contract's own TODAY-ONLY
            # open->close move -- it hides overnight gaps entirely (LTF on
            # Oct 16 2026: cash close-to-close -3.42%, FUT open->close only
            # -0.04%), which was misclassifying fut_signal for any stock
            # that gapped hard and then traded flat. The Oct 6 premise (that
            # this should match a FUT-vs-prior-close basis the live/intraday
            # branch uses) was also wrong: that branch is cash LTP vs cash
            # prev_close, not FUT. fut_price_chg_pct is still computed and
            # persisted above for reference, just no longer used to classify
            # fut_signal -- see api/positional_intelligence.py's Oct 18 2026
            # fix for the full writeup; this is the same fix at the source.
            price = row.get("price_chg_pct") or 0
            row["fut_price_chg_pct"] = fut_price_chg_map.get(sym)
            row["fut_vol"]        = fut_vol_map.get(sym, 0)
            row["fut_oi_chg_pct"] = fut_oi
            row["fut_oi_chg_pct_next"] = fut_oi_chg_map_next.get(sym, None)
            # (Aug 27 2026): absolute OI was always computed above but
            # never persisted -- only the derived % change was stored.
            row["fut_oi_close"] = fut_close_oi_map.get(sym, None)
            row["fut_oi_close_next"] = fut_close_oi_map_next.get(sym, None)
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
