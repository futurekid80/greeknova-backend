"""
vol_oi_breakout.py - Volume + OI Breakout scanner
Persists EOD snapshot to Supabase so it survives Railway restarts.
"""
import time as time_module
from datetime import datetime, timezone, timedelta

_breakout_cache = {}
_breakout_cache_time = 0.0

def is_market_hours():
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930

def is_weekday():
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).weekday() < 5  # Mon-Fri

def get_price_context(cmp, day_high, day_low):
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
            # Handle string JSON (not auto-parsed)
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

def get_vol_oi_breakout(supabase):
    global _breakout_cache, _breakout_cache_time

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Post-market or weekend: serve EOD snapshot
    if not is_market_hours():
        # Try in-memory cache first
        if _breakout_cache.get("signals") and _breakout_cache.get("date") == today:
            return dict(_breakout_cache, is_eod_snapshot=True)
        # Try Supabase persistent cache
        saved = _load_from_supabase(supabase)
        if saved:
            _breakout_cache = saved  # warm in-memory cache
            return saved
        # No data at all
        return {"signals": [], "total": 0, "is_eod_snapshot": True, "date": today}

    # During market hours: use in-memory cache (5 min TTL)
    if _breakout_cache and (time_module.time() - _breakout_cache_time) < 300:
        return _breakout_cache

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

        # ── Step 2: Build per-symbol data ─────────────────────────────────
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

        # ── Step 3: 5-day historical volume ───────────────────────────────
        hist_start = (datetime.now(timezone.utc) - timedelta(days=8)).strftime('%Y-%m-%d')
        hist_rows = supabase.from_("oi_snapshots")\
            .select("symbol, volume, timestamp")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{hist_start}T00:00:00+00:00")\
            .lt("timestamp",  f"{today}T00:00:00+00:00")\
            .gte("volume", 1000)\
            .limit(5000)\
            .execute()

        sym_date_vol = {}
        for r in (hist_rows.data or []):
            sym      = r["symbol"]
            date_str = str(r["timestamp"])[:10]
            vol      = int(r.get("volume") or 0)
            if sym not in sym_date_vol:
                sym_date_vol[sym] = {}
            if vol > sym_date_vol[sym].get(date_str, 0):
                sym_date_vol[sym][date_str] = vol

        hist_avg_vol = {}
        for sym, date_vols in sym_date_vol.items():
            sorted_vols = sorted(date_vols.values(), reverse=True)[:5]
            if sorted_vols:
                hist_avg_vol[sym] = sum(sorted_vols) / len(sorted_vols)

        # ── Step 4: CPR levels ────────────────────────────────────────────
        cpr_rows = supabase.from_("cpr_levels")\
            .select("symbol, tc, bc, width_label, width_emoji")\
            .eq("trade_date", today)\
            .execute()
        cpr_map = {r["symbol"]: r for r in (cpr_rows.data or [])}

        # ── Step 5: Compute signals ────────────────────────────────────────
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

            price_open = price_list[1] if len(price_list) >= 2 else price_list[0]
            price_chg  = round((cmp - price_open) / price_open * 100, 2) if price_open else 0

            MIN_PRICE_CHG = 0.3
            if oi_chg_pct > 0 and price_chg >= MIN_PRICE_CHG:
                sig_type, sig_label = "LONG_BUILDUP", "Long Buildup"
            elif oi_chg_pct > 0 and price_chg <= -MIN_PRICE_CHG:
                sig_type, sig_label = "SHORT_BUILDUP", "Short Buildup"
            elif oi_chg_pct < 0 and price_chg >= MIN_PRICE_CHG:
                sig_type, sig_label = "SHORT_COVERING", "Short Covering"
            elif oi_chg_pct < 0 and price_chg <= -MIN_PRICE_CHG:
                sig_type, sig_label = "LONG_UNWINDING", "Long Unwinding"
            else:
                continue  # skip flat price stocks entirely

            valid_prices = [p for p in price_list if p > 0]
            day_high = max(valid_prices) if valid_prices else cmp
            day_low  = min(valid_prices) if valid_prices else cmp
            price_ctx = get_price_context(cmp, day_high, day_low)

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
        import pytz
        ist = pytz.timezone('Asia/Kolkata')
        ist_now = datetime.now(ist)
        if ist_now.hour >= 15 and ist_now.minute >= 25:
            _save_to_supabase(supabase, top_signals, len(signals), today)

        _breakout_cache.update(result)
        _breakout_cache_time = time_module.time()
        print(f"[VOL_OI_BREAKOUT] {len(signals)} signals computed")
        return result

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[VOL_OI_BREAKOUT] Error: {e}")
        # Try Supabase fallback on error
        saved = _load_from_supabase(supabase)
        if saved:
            return saved
        return {"signals": [], "total": 0, "error": str(e)}
