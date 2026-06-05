"""
vol_oi_breakout.py — Volume + OI Breakout scanner
Finds stocks where:
  1. Today's volume > 1.5x of 5-day average volume
  2. FUT OI change > 2% from open
Returns price context (at high/low/off high/off low) for conviction assessment.
"""

import time as time_module
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_breakout_cache: dict = {}
_breakout_cache_time: float = 0

def is_market_hours() -> bool:
    from datetime import datetime
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    h, m = now.hour, now.minute
    mins = h * 60 + m
    return 555 <= mins <= 930  # 9:15 AM to 3:30 PM

def get_price_context(cmp: float, day_high: float, day_low: float) -> dict:
    """Determine where price is relative to day range."""
    if day_high <= day_low:
        return {"label": "➡️ Mid Range", "color": "GRAY", "pct": 0}
    
    day_range = day_high - day_low
    pct_from_high = round((day_high - cmp) / day_high * 100, 2)
    pct_from_low  = round((cmp - day_low) / day_low * 100, 2)
    range_position = (cmp - day_low) / day_range * 100  # 0=low, 100=high

    if pct_from_high <= 0.3:
        return {"label": "🔝 At Day High", "color": "EMERALD", "pct": pct_from_high}
    elif pct_from_low <= 0.3:
        return {"label": "🔻 At Day Low", "color": "RED", "pct": pct_from_low}
    elif range_position >= 60:
        return {"label": f"📉 Off High -{pct_from_high}%", "color": "AMBER", "pct": pct_from_high}
    elif range_position <= 40:
        return {"label": f"📈 Off Low +{pct_from_low}%", "color": "CYAN", "pct": pct_from_low}
    else:
        return {"label": "➡️ Mid Range", "color": "GRAY", "pct": 0}


def get_vol_oi_breakout(supabase) -> dict:
    global _breakout_cache, _breakout_cache_time

    # Cache: 5 min during market, hold EOD snapshot after close
    if not is_market_hours() and _breakout_cache.get("signals"):
        return {**_breakout_cache, "is_eod_snapshot": True}

    cache_ttl = 300  # 5 minutes
    if _breakout_cache and (time_module.time() - _breakout_cache_time) < cache_ttl:
        return _breakout_cache

    try:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # ── Step 1: Get today's FUT snapshots ────────────────────────────────
        today_rows = supabase.from_("oi_snapshots")\
            .select("symbol, timestamp, oi, volume, last_price, expiry")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(10000)\
            .execute()

        if not today_rows.data:
            return {"signals": [], "total": 0, "error": "No data"}

        # ── Step 2: Build per-symbol data ─────────────────────────────────────
        sym_data: dict = defaultdict(lambda: {
            "timestamps": [], "oi": [], "volume": [],
            "prices": [], "expiry": None
        })

        for r in today_rows.data:
            sym    = r["symbol"]
            expiry = str(r.get("expiry", ""))
            # Use nearest expiry only
            if sym_data[sym]["expiry"] and expiry > sym_data[sym]["expiry"]:
                continue
            sym_data[sym]["expiry"] = expiry
            sym_data[sym]["timestamps"].append(r["timestamp"])
            sym_data[sym]["oi"].append(int(r["oi"] or 0))
            sym_data[sym]["volume"].append(int(r["volume"] or 0))
            sym_data[sym]["prices"].append(float(r["last_price"] or 0))

        # ── Step 3: Get 5-day historical volume ───────────────────────────────
        hist_start = (datetime.now(timezone.utc) - timedelta(days=8)).strftime('%Y-%m-%d')
        hist_rows = supabase.from_("oi_snapshots")\
            .select("symbol, volume, timestamp")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{hist_start}T00:00:00+00:00")\
            .lt("timestamp",  f"{today}T00:00:00+00:00")\
            .gte("volume", 1000)\
            .limit(5000)\
            .execute()

        # Build 5-day avg volume per symbol
        sym_date_vol: dict = defaultdict(lambda: defaultdict(int))
        for r in (hist_rows.data or []):
            sym      = r["symbol"]
            date_str = str(r["timestamp"])[:10]
            vol      = int(r["volume"] or 0)
            if vol > sym_date_vol[sym][date_str]:
                sym_date_vol[sym][date_str] = vol

        hist_avg_vol: dict = {}
        for sym, date_vols in sym_date_vol.items():
            sorted_vols = sorted(date_vols.values(), reverse=True)[:5]
            if sorted_vols:
                hist_avg_vol[sym] = sum(sorted_vols) / len(sorted_vols)

        # ── Step 4: Get CPR positions ──────────────────────────────────────────
        cpr_rows = supabase.from_("cpr_levels")\
            .select("symbol, tc, bc, width_label, width_emoji")\
            .eq("trade_date", today)\
            .execute()
        cpr_map = {r["symbol"]: r for r in (cpr_rows.data or [])}

        # ── Step 5: Compute signals ────────────────────────────────────────────
        signals = []
        for sym, d in sym_data.items():
            if len(d["timestamps"]) < 3:
                continue

            oi_list  = d["oi"]
            vol_list = d["volume"]
            price_list = d["prices"]

            # Use second snapshot as open baseline
            oi_open   = oi_list[1] if len(oi_list) >= 2 else oi_list[0]
            vol_open  = vol_list[1] if len(vol_list) >= 2 else vol_list[0]
            oi_latest = oi_list[-1]
            vol_latest = vol_list[-1]
            cmp       = price_list[-1]

            # Day high/low from all prices today
            day_high = max(p for p in price_list if p > 0)
            day_low  = min(p for p in price_list if p > 0)

            if not oi_open or not vol_open or not cmp:
                continue

            oi_chg_pct  = round((oi_latest - oi_open) / oi_open * 100, 2)
            vol_chg_pct = round((vol_latest - vol_open) / vol_open * 100, 2) if vol_open else 0

            # 5-day avg volume check
            avg_vol_5d = hist_avg_vol.get(sym, 0)
            vol_ratio  = round(vol_latest / avg_vol_5d, 2) if avg_vol_5d > 0 else 0

            # ── Qualification filters ──────────────────────────────────────────
            # 1. Volume must be > 1.5x 5-day average
            if vol_ratio < 1.5:
                continue
            # 2. OI must be changing meaningfully
            if abs(oi_chg_pct) < 2.0:
                continue

            # Signal type
            price_open = price_list[1] if len(price_list) >= 2 else price_list[0]
            price_chg  = round((cmp - price_open) / price_open * 100, 2) if price_open else 0

            if oi_chg_pct > 0 and price_chg >= 0:
                signal_type = "LONG_BUILDUP"
                signal_label = "Long Buildup"
                bias = "BULLISH"
            elif oi_chg_pct > 0 and price_chg < 0:
                signal_type = "SHORT_BUILDUP"
                signal_label = "Short Buildup"
                bias = "BEARISH"
            elif oi_chg_pct < 0 and price_chg >= 0:
                signal_type = "SHORT_COVERING"
                signal_label = "Short Covering"
                bias = "BULLISH"
            else:
                signal_type = "LONG_UNWINDING"
                signal_label = "Long Unwinding"
                bias = "BEARISH"

            # Price context
            price_ctx = get_price_context(cmp, day_high, day_low)

            # CPR
            cpr = cpr_map.get(sym, {})
            if cpr:
                tc = float(cpr.get("tc", 0) or 0)
                bc = float(cpr.get("bc", 0) or 0)
                if cmp > tc:
                    cpr_position = "Above CPR"
                elif cmp < bc:
                    cpr_position = "Below CPR"
                else:
                    cpr_position = "Inside CPR"
            else:
                cpr_position = None

            signals.append({
                "symbol":         sym,
                "cmp":            cmp,
                "day_high":       day_high,
                "day_low":        day_low,
                "oi_chg_pct":     oi_chg_pct,
                "price_chg_pct":  price_chg,
                "vol_latest":     vol_latest,
                "vol_avg_5d":     round(avg_vol_5d),
                "vol_ratio":      vol_ratio,
                "vol_ratio_label": f"{vol_ratio}x avg",
                "signal_type":    signal_type,
                "signal_label":   signal_label,
                "bias":           bias,
                "price_context":  price_ctx["label"],
                "price_ctx_color": price_ctx["color"],
                "cpr_position":   cpr_position,
                "cpr_width_label": cpr.get("width_label"),
                "cpr_width_emoji": cpr.get("width_emoji"),
            })

        # Sort: vol_ratio desc, then oi_chg_pct desc
        signals.sort(key=lambda x: (x["vol_ratio"], abs(x["oi_chg_pct"])), reverse=True)

        result = {
            "signals":   signals[:10],  # top 10, frontend shows 5
            "total":     len(signals),
            "date":      today,
            "is_eod_snapshot": False,
        }

        _breakout_cache.update(result)
        _breakout_cache_time = time_module.time()
        print(f"[VOL_OI_BREAKOUT] {len(signals)} signals computed")
        return result

    except Exception as e:
        print(f"[VOL_OI_BREAKOUT] Error: {e}")
        return {"signals": [], "total": 0, "error": str(e)}
