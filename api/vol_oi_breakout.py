"""
vol_oi_breakout.py - Volume + OI Breakout scanner
Persists EOD snapshot to Supabase so it survives Railway restarts.
Fixes:
- Price change now uses prev close → current (not FUT open → now)
- ±0.3% threshold applied before classifying signal direction
"""
import time as time_module
from datetime import datetime, timezone, timedelta

_breakout_cache = {}
_breakout_cache_time = 0.0

def is_market_hours():
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930

def is_weekday():
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).weekday() < 5

def get_price_context(cmp, day_high, day_low, signal_type=None):
    if day_high <= day_low or day_high == 0:
        return {"label": "Mid Range", "color": "GRAY"}
    day_range = day_high - day_low
    pct_from_high = round((day_high - cmp) / day_high * 100, 2)
    pct_from_low  = round((cmp - day_low) / day_low * 100, 2)
    range_pos     = (cmp - day_low) / day_range * 100

    if pct_from_high <= 0.3:
        return {"label": "At Day High", "color": "EMERALD"}
    elif pct_from_low <= 0.3:
        return {"label": "At Day Low", "color": "RED"}

    # For Short Buildup — highlight recovery from lows as a caution signal
    # Any position above 30% of day range = not at lows = caution for bears
    if signal_type == "SHORT_BUILDUP" and range_pos >= 30:
        return {"label": f"Recovered +{pct_from_low}% from Low ⚠️", "color": "AMBER"}

    elif pct_from_high > 1.5:
        return {"label": f"Off High -{pct_from_high}%", "color": "AMBER"}
    elif pct_from_low > 1.5 and range_pos <= 40:
        return {"label": f"Off Low +{pct_from_low}%", "color": "CYAN"}
    elif range_pos >= 60:
        return {"label": f"Off High -{pct_from_high}%", "color": "AMBER"}
    elif range_pos <= 40:
        return {"label": f"Off Low +{pct_from_low}%", "color": "CYAN"}
    else:
        return {"label": "Mid Range", "color": "GRAY"}

def classify_signal(oi_chg_pct, price_chg):
    """Classify signal with ±0.3% price threshold to avoid flat-price misfires."""
    if oi_chg_pct > 0 and price_chg >= 0.3:
        return "LONG_BUILDUP", "Long Buildup"
    elif oi_chg_pct > 0 and price_chg <= -0.3:
        return "SHORT_BUILDUP", "Short Buildup"
    elif oi_chg_pct < 0 and price_chg >= 0.3:
        return "SHORT_COVERING", "Short Covering"
    elif oi_chg_pct < 0 and price_chg <= -0.3:
        return "LONG_UNWINDING", "Long Unwinding"
    else:
        return None, None  # flat price — skip

def _load_from_supabase(supabase):
    """Load last saved EOD snapshot from Supabase."""
    try:
        result = supabase.from_("vol_oi_breakout_cache")\
            .select("*")\
            .eq("id", 1)\
            .limit(1)\
            .execute()
        print(f"[VOL_OI_BREAKOUT] Supabase load: {len(result.data) if result.data else 0} rows")
        if result.data:
            row = result.data[0]
            signals = row.get("signals", [])
            if isinstance(signals, str):
                import json
                signals = json.loads(signals)
            total = row.get("total", len(signals))
            print(f"[VOL_OI_BREAKOUT] Loaded {len(signals)} signals for {row.get('trade_date')}")
            return {
                "signals":         signals,
                "total":           total,
                "date":            str(row.get("trade_date", "")),
                "is_eod_snapshot": True,
            }
    except Exception as e:
        import traceback
        print(f"[VOL_OI_BREAKOUT] Supabase load failed: {e}")
        traceback.print_exc()
    return None

def _save_to_supabase(supabase, signals, total, trade_date):
    """Persist EOD snapshot to Supabase — survives Railway restarts."""
    try:
        supabase.from_("vol_oi_breakout_cache")\
            .upsert({
                "id":         1,
                "signals":    signals,
                "total":      total,
                "trade_date": trade_date,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            })\
            .execute()
        print(f"[VOL_OI_BREAKOUT] EOD snapshot saved to Supabase — {total} signals")
    except Exception as e:
        print(f"[VOL_OI_BREAKOUT] Supabase save failed: {e}")

def _get_eod_from_summary(supabase, now_ist):
    """Fallback: compute Vol+OI breakout from daily_oi_summary when cache is stale."""
    from collections import defaultdict
    check = now_ist.date()
    # Post-market: use today if after 3:30 PM, else use previous trading day
    if not is_market_hours():
        market_closed_today = now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 30)
        if not market_closed_today:
            check -= timedelta(days=1)
            while check.weekday() >= 5:
                check -= timedelta(days=1)
    trade_date = check.isoformat()

    rows = supabase.from_("daily_oi_summary")\
        .select("symbol, fut_vol, oi_chg_pct, fut_oi_chg_pct, price_chg_pct, close_price")\
        .eq("trade_date", trade_date)\
        .gt("fut_vol", 0)\
        .limit(200)\
        .execute()

    hist_start = (now_ist.date() - timedelta(days=35)).isoformat()
    hist = supabase.from_("daily_oi_summary")\
        .select("symbol, trade_date, fut_vol")\
        .gte("trade_date", hist_start)\
        .lt("trade_date", trade_date)\
        .gt("fut_vol", 0)\
        .limit(5000)\
        .execute()

    sym_hist = defaultdict(list)
    for r in (hist.data or []):
        sym_hist[r["symbol"]].append(int(r.get("fut_vol") or 0))

    signals = []
    for r in (rows.data or []):
        sym = r["symbol"]
        vol_today = int(r.get("fut_vol") or 0)
        hist_vols = sorted(sym_hist[sym], reverse=True)[:5]
        avg_5d = sum(hist_vols) / len(hist_vols) if hist_vols else 0
        vol_ratio = round(vol_today / avg_5d, 2) if avg_5d > 0 else 0
        if vol_ratio < 1.5:
            continue
        fut_oi_chg = float(r.get("fut_oi_chg_pct") or 0)
        oi_chg = round(fut_oi_chg if fut_oi_chg != 0 else float(r.get("oi_chg_pct") or 0), 2)
        if abs(oi_chg) < 2.0:
            continue
        # EOD uses prev_close → close price change (already correct in daily_oi_summary)
        price_chg = round(float(r.get("price_chg_pct") or 0), 2)
        cmp = float(r.get("close_price") or 0)

        sig_type, sig_label = classify_signal(oi_chg, price_chg)
        if sig_type is None:
            continue  # flat price — skip

        # Use saved day_high/day_low from daily_oi_summary if available
        # Get intraday high/low from cmp_prices for today
        day_high = cmp
        day_low  = cmp
        try:
            hl_res = supabase.from_("cmp_prices")\
                .select("cmp")\
                .eq("symbol", sym)\
                .gte("timestamp", f"{trade_date}T03:45:00+00:00")\
                .lte("timestamp", f"{trade_date}T10:00:00+00:00")\
                .execute()
            if hl_res.data:
                prices = [float(r["cmp"]) for r in hl_res.data]
                day_high = max(prices)
                day_low  = min(prices)
        except:
            pass
        # Use actual H/L from cmp_prices for proper context
        price_ctx = get_price_context(cmp, day_high, day_low, sig_type)

        signals.append({
            "symbol":          sym,
            "cmp":             cmp,
            "day_high":        day_high,
            "day_low":         day_low,
            "oi_chg_pct":      oi_chg,
            "price_chg_pct":   price_chg,
            "vol_latest":      vol_today,
            "vol_avg_5d":      round(avg_5d),
            "vol_ratio":       vol_ratio,
            "signal_type":     sig_type,
            "signal_label":    sig_label,
            "price_context":   price_ctx["label"],
            "price_ctx_color": price_ctx["color"],
            "cpr_position":    None,
            "cpr_width_label": None,
            "cpr_width_emoji": None,
        })

    signals.sort(key=lambda x: (x["vol_ratio"], abs(x["oi_chg_pct"])), reverse=True)
    return {
        "signals":         signals[:10],
        "total":           len(signals),
        "date":            trade_date,
        "is_eod_snapshot": True,
    }

def get_vol_oi_breakout(supabase):
    global _breakout_cache, _breakout_cache_time

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today = now_ist.strftime('%Y-%m-%d')

    # Post-market or weekend: serve EOD snapshot
    if not is_market_hours():
        # Invalidate cache if stale date
        if _breakout_cache.get("date") != today:
            _breakout_cache = {}
            _breakout_cache_time = 0.0
        # Check if today's snapshot exists in cache
        if _breakout_cache.get("signals") and _breakout_cache.get("date") == today:
            return dict(_breakout_cache, is_eod_snapshot=True)
        saved = _load_from_supabase(supabase)
        # Only serve saved data if it's from today
        if saved and saved.get("date") == today:
            _breakout_cache = saved
            return saved
        # Today's data not yet saved — compute fresh from daily_oi_summary
        return _get_eod_from_summary(supabase, now_ist)

    # ── MARKET HOURS ONLY BELOW THIS LINE ────────────────────────────────
    # During market hours NEVER serve EOD snapshot from Supabase
    # Always compute live from oi_snapshots

    # During market hours: use in-memory cache (5 min TTL)
    # Invalidate cache if it contains stale date (yesterday's data)
    if _breakout_cache and _breakout_cache.get("date") != today:
        print(f"[VOL_OI_BREAKOUT] Cache date mismatch — clearing stale cache")
        _breakout_cache = {}
        _breakout_cache_time = 0.0
    if _breakout_cache and (time_module.time() - _breakout_cache_time) < 300:
        return _breakout_cache
    # Never load from Supabase during market hours — always compute live
    print(f"[VOL_OI_BREAKOUT] Market hours — computing live data for {today}")

    try:
        # ── Step 1: Today's FUT snapshots ─────────────────────────────────
        today_rows = supabase.from_("oi_snapshots")\
            .select("symbol, timestamp, oi, volume, last_price, expiry")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(10000)\
            .execute()

        if not today_rows.data:
            return {"signals": [], "total": 0}

        # ── Step 2: Build per-symbol data (nearest expiry only) ───────────
        sym_data = {}
        for r in today_rows.data:
            sym    = r["symbol"]
            expiry = str(r.get("expiry") or "")
            oi     = int(r.get("oi") or 0)
            vol    = int(r.get("volume") or 0)
            price  = float(r.get("last_price") or 0)

            if sym not in sym_data:
                sym_data[sym] = {"expiry": expiry, "oi": [], "volume": [], "prices": []}

            cur_expiry = sym_data[sym]["expiry"]
            if cur_expiry and expiry and expiry > cur_expiry:
                continue
            if expiry and expiry != cur_expiry:
                sym_data[sym]["expiry"] = expiry
                sym_data[sym]["oi"] = []
                sym_data[sym]["volume"] = []
                sym_data[sym]["prices"] = []

            sym_data[sym]["oi"].append(oi)
            sym_data[sym]["volume"].append(vol)
            sym_data[sym]["prices"].append(price)

       # ── Step 3: 5-day historical volume from daily_oi_summary ─────────
        hist_start = (datetime.now(timezone.utc) - timedelta(days=10)).strftime('%Y-%m-%d')
        hist_rows = supabase.from_("daily_oi_summary")\
            .select("symbol, trade_date, fut_vol")\
            .gte("trade_date", hist_start)\
            .lt("trade_date", today)\
            .gt("fut_vol", 0)\
            .limit(1000)\
            .execute()

        from collections import defaultdict
        sym_date_vol = defaultdict(list)
        for r in (hist_rows.data or []):
            sym = r["symbol"]
            vol = int(r.get("fut_vol") or 0)
            if vol > 0:
                sym_date_vol[sym].append(vol)

        hist_avg_vol = {}
        for sym, vols in sym_date_vol.items():
            sorted_vols = sorted(vols, reverse=True)[:5]
            if sorted_vols:
                hist_avg_vol[sym] = sum(sorted_vols) / len(sorted_vols)

        # ── Step 4: CPR levels ────────────────────────────────────────────
        cpr_rows = supabase.from_("cpr_levels")\
            .select("symbol, tc, bc, width_label, width_emoji")\
            .eq("trade_date", today)\
            .execute()
        cpr_map = {r["symbol"]: r for r in (cpr_rows.data or [])}

        # ── Step 5: Prev close prices (for accurate price change) ─────────
        # Use prev trading day's latest CMP — same as Market Pulse / OI Pulse
        from datetime import datetime as _dt
        prev_day = _dt.strptime(today, '%Y-%m-%d')
        for _ in range(5):
            prev_day = prev_day - timedelta(days=1)
            if prev_day.weekday() < 5:
                break
        prev_date = prev_day.strftime('%Y-%m-%d')

        prev_cmp_res = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{prev_date}T00:00:00+00:00")\
            .lte("timestamp", f"{prev_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(500)\
            .execute()

        prev_close_map = {}
        seen_prev = set()
        for row in (prev_cmp_res.data or []):
            sym = row["symbol"]
            if sym not in seen_prev:
                prev_close_map[sym] = float(row["cmp"])
                seen_prev.add(sym)

        # ── Step 6: Compute signals ────────────────────────────────────────
        signals = []
        for sym, d in sym_data.items():
            oi_list    = d["oi"]
            vol_list   = d["volume"]
            price_list = d["prices"]

            if len(oi_list) < 3:
                continue

            oi_open  = oi_list[1]
            vol_open = vol_list[1]
            oi_now   = oi_list[-1]
            vol_now  = vol_list[-1]
            cmp      = price_list[-1]

            if not oi_open or not vol_open or not cmp:
                continue

            oi_chg_pct = round((oi_now - oi_open) / oi_open * 100, 2)
            avg_vol_5d = hist_avg_vol.get(sym, 0)
            vol_ratio  = round(vol_now / avg_vol_5d, 2) if avg_vol_5d > 0 else 0

            if vol_ratio < 1.5:
                continue
            if abs(oi_chg_pct) < 2.0:
                continue

            # Use prev close → current CMP for price change (consistent with Market Pulse)
            prev_close = prev_close_map.get(sym, 0)
            if prev_close > 0:
                price_chg = round((cmp - prev_close) / prev_close * 100, 2)
            else:
                # Fallback to intraday if no prev close available
                price_open = price_list[1] if len(price_list) >= 2 else price_list[0]
                price_chg = round((cmp - price_open) / price_open * 100, 2) if price_open else 0

            sig_type, sig_label = classify_signal(oi_chg_pct, price_chg)
            if sig_type is None:
                continue  # flat price — skip

            valid_prices = [p for p in price_list if p > 0]
            day_high = max(valid_prices) if valid_prices else cmp
            day_low  = min(valid_prices) if valid_prices else cmp
            price_ctx = get_price_context(cmp, day_high, day_low, sig_type)

            cpr = cpr_map.get(sym, {})
            cpr_position = None
            if cpr:
                tc = float(cpr.get("tc") or 0)
                bc = float(cpr.get("bc") or 0)
                if tc and bc:
                    cpr_position = "Above CPR" if cmp > tc else "Below CPR" if cmp < bc else "Inside CPR"

            signals.append({
                "symbol":          sym,
                "cmp":             cmp,
                "day_high":        day_high,
                "day_low":         day_low,
                "oi_chg_pct":      oi_chg_pct,
                "price_chg_pct":   price_chg,
                "vol_latest":      vol_now,
                "vol_avg_5d":      round(avg_vol_5d),
                "vol_ratio":       vol_ratio,
                "signal_type":     sig_type,
                "signal_label":    sig_label,
                "price_context":   price_ctx["label"],
                "price_ctx_color": price_ctx["color"],
                "cpr_position":    cpr_position,
                "cpr_width_label": cpr.get("width_label"),
                "cpr_width_emoji": cpr.get("width_emoji"),
            })

        signals.sort(key=lambda x: (x["vol_ratio"], abs(x["oi_chg_pct"])), reverse=True)
        top_signals = signals[:10]

        result = {
            "signals":         top_signals,
            "total":           len(signals),
            "date":            today,
            "is_eod_snapshot": False,
        }

        # Save to Supabase after market close (3:30 PM IST = 10:00 UTC)
        ist_now = datetime.now(ist)
        if ist_now.hour >= 15 and ist_now.minute >= 30:
            _save_to_supabase(supabase, top_signals, len(signals), today)

        _breakout_cache.update(result)
        _breakout_cache_time = time_module.time()
        print(f"[VOL_OI_BREAKOUT] {len(signals)} signals computed")
        return result

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[VOL_OI_BREAKOUT] Error: {e}")
        saved = _load_from_supabase(supabase)
        if saved:
            return saved
        return {"signals": [], "total": 0, "error": str(e)}
