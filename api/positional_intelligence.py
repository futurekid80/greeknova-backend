"""
positional_intelligence.py
Unified scanner combining:
  - Active Conviction  : 2+ consecutive FUT signal days (from daily_oi_summary.fut_signal)
  - Stealth Buildup    : Top FUT OI rank + small price candle
  - Volume Breakout    : FUT vol > 1.5x 5-day avg + OI confirmation
  - Series Buildup     : 60%+ consistent signal days over full series

Single source of truth: daily_oi_summary.fut_signal (FUT open→close classification)
"""
import time as time_module
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_cache: dict = {}
_cache_time: float = 0.0
_CACHE_TTL = 30  # 5 min during market, 3600 post-market


def _get_expiry_and_series(today):
    """Get current monthly expiry and series start date."""
    import calendar
    from api.positional_radar import get_monthly_expiry, get_series_start
    expiry = get_monthly_expiry(today.year, today.month)
    series_start = get_series_start(expiry)
    return expiry, series_start


def _classify_stealth_tier(rank, abs_price, net_delta_bullish):
    """Classify stealth buildup tier."""
    if rank <= 3 and abs_price <= 0.5 and net_delta_bullish:
        return "ELITE", "Elite"
    elif rank <= 3 and abs_price <= 1.0:
        return "STRONG", "Strong"
    elif rank <= 5 and abs_price <= 1.5:
        return "WATCH", "Watch"
    return None, None


def get_positional_intelligence(min_consec: int = 0):
    global _cache, _cache_time

    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    is_market = now_ist.weekday() < 5 and (9 * 60 + 15) <= (now_ist.hour * 60 + now_ist.minute) <= (15 * 60 + 30)
    ttl = 300 if is_market else 3600

    cache_key = str(min_consec)
    if _cache.get(cache_key) and (time_module.time() - _cache_time) < ttl:
        return _cache[cache_key]

    from utils.db import get_supabase
    supabase = get_supabase()

    today = now_ist.date()
    today_str = today.isoformat()

    # Get expiry and series dates
    try:
        expiry, series_start = _get_expiry_and_series(today)
    except Exception as e:
        print(f"[PI] Expiry calc failed: {e}")
        return {"error": str(e), "results": []}

    # ── 1. Get positional intelligence from RPC ───────────────────────────
    try:
        rpc = supabase.rpc("get_positional_intelligence", {
            "p_series_start": series_start,
            "p_series_end": today_str
        }).execute()
        pi_data = {r["symbol"]: r for r in (rpc.data or [])}
    except Exception as e:
        print(f"[PI] RPC failed: {e}")
        pi_data = {}

    # ── 2. Get stealth buildup data (last 15 days FUT OI history) ─────────
    hist_start = (today - timedelta(days=25)).isoformat()
    try:
        hist_res = supabase.from_("daily_oi_summary")\
            .select("symbol, trade_date, fut_oi_chg_pct, price_chg_pct, close_price, fut_signal")\
            .gte("trade_date", hist_start)\
            .lte("trade_date", today_str)\
            .order("trade_date", desc=False)\
            .limit(5000)\
            .execute()
        sym_history = defaultdict(list)
        for r in (hist_res.data or []):
            sym_history[r["symbol"]].append(r)
    except Exception as e:
        print(f"[PI] History fetch failed: {e}")
        sym_history = defaultdict(list)

    # ── 3. Get CMP ────────────────────────────────────────────────────────
    try:
        cmp_res = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{today_str}T00:00:00+00:00")\
            .lte("timestamp", f"{today_str}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(500)\
            .execute()
        cmp_map = {}
        seen = set()
        for r in (cmp_res.data or []):
            if r["symbol"] not in seen:
                cmp_map[r["symbol"]] = float(r["cmp"])
                seen.add(r["symbol"])
    except Exception as e:
        print(f"[PI] CMP fetch failed: {e}")
        cmp_map = {}

    # ── 4. Get volume breakout data ───────────────────────────────────────
    vol_start = (today - timedelta(days=35)).isoformat()
    try:
        vol_hist = supabase.from_("daily_oi_summary")\
            .select("symbol, trade_date, fut_vol, fut_oi_chg_pct, price_chg_pct, close_price")\
            .gte("trade_date", vol_start)\
            .lte("trade_date", today_str)\
            .gt("fut_vol", 0)\
            .order("trade_date", desc=False)\
            .limit(5000)\
            .execute()
        vol_sym_data = defaultdict(list)
        for r in (vol_hist.data or []):
            vol_sym_data[r["symbol"]].append(r)
    except Exception as e:
        print(f"[PI] Vol hist failed: {e}")
        vol_sym_data = defaultdict(list)

    # ── 5. Get OI walls (CPR position) ────────────────────────────────────
    try:
        # CPR is stored for next trading day — find nearest available
        cpr_res = supabase.from_("cpr_levels")\
            .select("symbol, tc, bc, width_label, width_emoji, cpr_trend, trade_date")\
            .gte("trade_date", today_str)\
            .order("trade_date", desc=False)\
            .limit(200)\
            .execute()
        cpr_map = {r["symbol"]: r for r in (cpr_res.data or [])}
    except:
        cpr_map = {}

  # ── 5b. Get live intraday FUT OI change (market hours only) ──────────
    live_oi_map = {}
    if is_market:
        try:
            snap_start = f"{today_str}T03:45:00+00:00"  # 9:15 IST
            snap_res = supabase.from_("oi_snapshots")\
                .select("symbol, oi, last_price, timestamp")\
                .eq("option_type", "FUT")\
                .gte("timestamp", snap_start)\
                .order("timestamp", desc=False)\
                .limit(10000)\
                .execute()
            first_oi = {}
            latest_oi = {}
            latest_price = {}
            open_price = {}
            snap_by_ts: dict = {}
            for r in (snap_res.data or []):
                s = r["symbol"]
                oi = int(r.get("oi") or 0)
                ts = r.get("timestamp", "")
                key = f"{s}_{ts}"
                if oi > snap_by_ts.get(key, {}).get("oi", 0):
                    snap_by_ts[key] = {"symbol": s, "oi": oi, "lp": float(r.get("last_price") or 0)}
            for row in snap_by_ts.values():
                s = row["symbol"]
                oi = row["oi"]
                lp = row["lp"]
                if s not in first_oi and oi > 0:
                    first_oi[s] = oi
                    open_price[s] = lp
                if oi > 0:
                    latest_oi[s] = oi
                    latest_price[s] = lp
            snap_by_ts: dict = {}
            for r in (snap_res.data or []):
                s = r["symbol"]
                oi = int(r.get("oi") or 0)
                ts = r.get("timestamp", "")
                key = f"{s}_{ts}"
                if oi > snap_by_ts.get(key, {}).get("oi", 0):
                    snap_by_ts[key] = {"symbol": s, "oi": oi, "lp": float(r.get("last_price") or 0), "ts": ts}

            for row in snap_by_ts.values():
                s = row["symbol"]
                oi = row["oi"]
                lp = row["lp"]
                if s not in first_oi and oi > 0:
                    first_oi[s] = oi
                    open_price[s] = lp
                if oi > 0:
                    latest_oi[s] = oi
                    latest_price[s] = lp
                if oi > 0:
                    latest_oi[s] = oi
                    latest_price[s] = lp
            for s in first_oi:
                if first_oi[s] > 0:
                    oi_chg = ((latest_oi[s] - first_oi[s]) / first_oi[s]) * 100
                    price_chg = ((latest_price[s] - open_price[s]) / open_price[s]) * 100 if open_price[s] > 0 else 0
                    live_oi_map[s] = {
                        "fut_oi_chg_pct": round(oi_chg, 2),
                        "price_chg_pct": round(price_chg, 2),
                    }
        except Exception as e:
            print(f"[PI] Live OI fetch failed: {e}")

    # ── Build results ─────────────────────────────────────────────────────
    active_conviction = []
    stealth_buildup = []
    vol_breakout = []
    series_buildup = []

    SYMBOLS = list(pi_data.keys()) if pi_data else list(sym_history.keys())

    for sym in SYMBOLS:
        pi = pi_data.get(sym, {})
        history = sym_history.get(sym, [])
        cmp = cmp_map.get(sym, float(pi.get("latest_price") or 0))
        cpr = cpr_map.get(sym, {})

        cpr_position = None
        if cpr and cmp > 0:
            tc = float(cpr.get("tc") or 0)
            bc = float(cpr.get("bc") or 0)
            if tc and bc:
                cpr_position = "Above CPR" if cmp > tc else "Below CPR" if cmp < bc else "Inside CPR"

        consec_lb = int(pi.get("consec_lb") or 0)
        consec_sb = int(pi.get("consec_sb") or 0)
        lb_consistency = int(pi.get("lb_consistency_pct") or 0)
        sb_consistency = int(pi.get("sb_consistency_pct") or 0)
        latest_signal = pi.get("latest_fut_signal", "NEUTRAL")
        series_lb_oi = float(pi.get("series_lb_oi") or 0)
        series_sb_oi = float(pi.get("series_sb_oi") or 0)
        total_days = int(pi.get("total_days") or 0)

        base = {
            "symbol": sym,
            "cmp": round(cmp, 2),
            "latest_signal": latest_signal,
            "latest_fut_oi_chg": round(float(pi.get("latest_fut_oi_chg") or 0), 2),
            "latest_price_chg": round(float(pi.get("latest_price_chg") or 0), 2),
            "cpr_position": cpr_position,
            "cpr_width_label": cpr.get("width_label"),
            "cpr_width_emoji": cpr.get("width_emoji"),
            "cpr_trend": cpr.get("cpr_trend"),
            "total_days": total_days,
        }

        # ── Active Conviction ─────────────────────────────────────────────
        consec = consec_lb if consec_lb >= 2 else (consec_sb if consec_sb >= 2 else 0)
        if consec >= 2:
            signal = "LONG_BUILDUP" if consec_lb >= consec_sb else "SHORT_BUILDUP"
            consistency = lb_consistency if signal == "LONG_BUILDUP" else sb_consistency
            series_oi = series_lb_oi if signal == "LONG_BUILDUP" else series_sb_oi

            if min_consec == 0 or consec >= min_consec:
                active_conviction.append({
                    **base,
                    "signal": signal,
                    "consec_days": consec,
                    "consistency_pct": consistency,
                    "series_oi_pct": round(series_oi, 1),
                    "lb_days": int(pi.get("lb_days") or 0),
                    "sb_days": int(pi.get("sb_days") or 0),
                })

        # ── Stealth Buildup ───────────────────────────────────────────────
        if len(history) >= 8:
            last_15 = history[-15:]
            # During market hours use live snapshot data, else use daily_oi_summary
            if is_market and sym in live_oi_map:
                today_oi = live_oi_map[sym].get("fut_oi_chg_pct", 0)
                today_price = live_oi_map[sym].get("price_chg_pct", 0)
            else:
                today_data = next((h for h in reversed(last_15) if h["trade_date"] == today_str), None)
                if not today_data:
                    today_data = history[-1] if history else None
                today_oi = float((today_data or {}).get("fut_oi_chg_pct") or 0)
                today_price = float((today_data or {}).get("price_chg_pct") or 0)
            if today_oi > 0 and today_price > -0.3:
                if True:
                    if is_market:
                        # Intraday: use absolute OI threshold (EOD rank comparison doesn't apply)
                        if today_oi >= 2.0 and abs(today_price) <= 0.5:
                            tier, tier_label = "ELITE", "Elite"
                        elif today_oi >= 1.5 and abs(today_price) <= 1.0:
                            tier, tier_label = "STRONG", "Strong"
                        elif today_oi >= 1.0 and abs(today_price) <= 1.5:
                            tier, tier_label = "WATCH", "Watch"
                        else:
                            tier, tier_label = None, None
                    else:
                        all_oi = sorted([float(h.get("fut_oi_chg_pct") or 0) for h in last_15 if float(h.get("fut_oi_chg_pct") or 0) > 0], reverse=True)
                        rank = next((i + 1 for i, v in enumerate(all_oi) if today_oi >= v), len(all_oi) + 1)
                        tier, tier_label = _classify_stealth_tier(rank, abs(today_price), True)
                    if tier:
                        stealth_buildup.append({
                            **base,
                            "signal": "STEALTH",
                            "tier": tier,
                            "tier_label": tier_label,
                            "rank": rank,
                            "today_oi_chg": round(today_oi, 2),
                            "price_chg": round(today_price, 2),
                            "net_delta": 0,
                            "oi_history": [round(float(h.get("fut_oi_chg_pct") or 0), 2) for h in last_15],
                        })

        # ── Volume Breakout ───────────────────────────────────────────────
        vol_data = vol_sym_data.get(sym, [])
        if len(vol_data) >= 5:
            today_vol_row = next((r for r in reversed(vol_data) if r["trade_date"] == today_str), None)
            if today_vol_row:
                today_vol = int(today_vol_row.get("fut_vol") or 0)
                hist_vols = sorted([int(r.get("fut_vol") or 0) for r in vol_data[:-1] if int(r.get("fut_vol") or 0) > 0], reverse=True)[:5]
                avg_5d = sum(hist_vols) / len(hist_vols) if hist_vols else 0
                vol_ratio = round(today_vol / avg_5d, 2) if avg_5d > 0 else 0
                if vol_ratio >= 1.5:
                    fut_oi = float(today_vol_row.get("fut_oi_chg_pct") or 0)
                    price_chg = float(today_vol_row.get("price_chg_pct") or 0)
                    if abs(fut_oi) >= 2.0:
                        if fut_oi > 0 and price_chg >= 0.3:
                            vol_signal = "LONG_BUILDUP"
                        elif fut_oi > 0 and price_chg <= -0.3:
                            vol_signal = "SHORT_BUILDUP"
                        elif fut_oi < 0 and price_chg >= 0.3:
                            vol_signal = "SHORT_COVERING"
                        elif fut_oi < 0 and price_chg <= -0.3:
                            vol_signal = "LONG_UNWINDING"
                        else:
                            vol_signal = None
                        if vol_signal:
                            vol_breakout.append({
                                **base,
                                "signal": vol_signal,
                                "vol_ratio": vol_ratio,
                                "vol_today": today_vol,
                                "vol_avg_5d": round(avg_5d),
                                "fut_oi_chg_pct": round(fut_oi, 2),
                                "price_chg_pct": round(price_chg, 2),
                            })

        # ── Series Buildup ────────────────────────────────────────────────
        dominant_signal = None
        dominant_consistency = 0
        dominant_series_oi = 0

        lb_days_count = int(pi.get("lb_days") or 0)
        sb_days_count = int(pi.get("sb_days") or 0)

        if lb_consistency >= 60 and consec_lb < 2 and lb_days_count >= 3:
            dominant_signal = "LONG_BUILDUP"
            dominant_consistency = lb_consistency
            dominant_series_oi = series_lb_oi
        elif sb_consistency >= 60 and consec_sb < 2 and sb_days_count >= 3:
            dominant_signal = "SHORT_BUILDUP"
            dominant_consistency = sb_consistency
            dominant_series_oi = series_sb_oi

        if dominant_signal and total_days >= 8:
            series_buildup.append({
                **base,
                "signal": dominant_signal,
                "consistency_pct": dominant_consistency,
                "series_oi_pct": round(dominant_series_oi, 1),
                "lb_days": int(pi.get("lb_days") or 0),
                "sb_days": int(pi.get("sb_days") or 0),
            })

    # Sort each section
    active_conviction.sort(key=lambda x: (-x["consec_days"], -x["consistency_pct"]))
    stealth_buildup.sort(key=lambda x: ({"ELITE": 0, "STRONG": 1, "WATCH": 2}.get(x["tier"], 3), x["rank"]))
    vol_breakout.sort(key=lambda x: -x["vol_ratio"])
    series_buildup.sort(key=lambda x: -x["consistency_pct"])

    result = {
        "expiry": expiry,
        "series_start": series_start,
        "total_trading_days": max((p.get("total_days", 0) for p in pi_data.values()), default=0),
        "date": today_str,
        "active_conviction": active_conviction,
        "stealth_buildup": stealth_buildup[:10],
        "vol_breakout": vol_breakout[:10],
        "series_buildup": series_buildup[:15],
        "summary": {
            "active_conviction": len(active_conviction),
            "stealth_buildup": len(stealth_buildup),
            "vol_breakout": len(vol_breakout),
            "series_buildup": len(series_buildup),
            "long_bias": sum(1 for r in active_conviction if r["signal"] == "LONG_BUILDUP"),
            "short_bias": sum(1 for r in active_conviction if r["signal"] == "SHORT_BUILDUP"),
        }
    }

    _cache[cache_key] = result
    _cache_time = time_module.time()
    print(f"[PI] Done — conviction:{len(active_conviction)} stealth:{len(stealth_buildup)} vol:{len(vol_breakout)} series:{len(series_buildup)}")
    return result
