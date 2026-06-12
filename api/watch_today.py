"""
api/watch_today.py
Cross-references Positional Radar stocks with today's Intraday Scanner.
Three rules to qualify:
1. Stock is in Positional Radar (2+ days same directional OI buildup)
2. Stock is active in today's Intraday Scanner (FUT OI moving today)
3. Positional bias matches intraday bias (aligned direction)
"""
from datetime import datetime, timezone, timedelta
import datetime as _dt


def get_watch_today(supabase):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Walk back to last trading day
    check = _dt.date.today() - _dt.timedelta(days=1)
    while check.weekday() >= 5:
        check -= _dt.timedelta(days=1)
    last_trading_day = check.isoformat()

    # ── Step 1: Get Positional Radar — ALL stocks (already filtered 2+ days) ──
    try:
        from api.positional_radar import get_monthly_expiry, get_series_start
        _today_date = _dt.date.today()
        _expiry = get_monthly_expiry(_today_date.year, _today_date.month)
        _series_start = get_series_start(_expiry)
    except:
        _series_start = "2026-05-27"

    radar_result = supabase.rpc("get_positional_radar_eod_fast", {
        "p_series_start": _series_start,
        "p_series_end": last_trading_day
    }).execute()

    radar_stocks = {}
    for r in (radar_result.data or []):
        radar_stocks[r["symbol"]] = {
            "symbol":            r["symbol"],
            "conviction_level":  r.get("conviction_level", ""),
            "conviction_label":  r.get("conviction_label", ""),
            "conviction_emoji":  r.get("conviction_emoji", ""),
            "conviction_rank":   r.get("conviction_rank", 0),
            "ignition":          r.get("ignition", False),
            "positional_signal": r.get("signal", ""),
            "positional_bias":   r.get("bias", ""),
            "consistency_pct":   r.get("consistency_pct", 0),
            "pcr_series":        r.get("pcr_series", 0),
        }

    if not radar_stocks:
        return {
            "date": today,
            "watch_today": [],
            "total": 0,
            "message": "Positional Radar has no data yet"
        }

    # ── Step 2: Get today's intraday signals from cache ───────────────────────
    intraday_result = supabase.from_("intraday_signal_cache")\
        .select("*")\
        .eq("id", 1)\
        .limit(1)\
        .execute()

    intraday_map = {}
    if intraday_result.data:
        row = intraday_result.data[0]
        signals = row.get("signals", [])
        if isinstance(signals, str):
            import json
            signals = json.loads(signals)
        for sig in signals:
            intraday_map[sig["symbol"]] = sig

    # ── Step 3: Cross-reference with alignment filter ─────────────────────────
    watch_today = []
    for sym, radar in radar_stocks.items():
        intraday = intraday_map.get(sym)

        # Rule 2: Must have intraday signal today
        if not intraday:
            continue

        # Rule 3: Positional and intraday bias must align
        positional_bias = radar["positional_bias"]
        intraday_bias = intraday.get("bias", "")
        if positional_bias != intraday_bias:
            continue

        watch_today.append({
            "symbol":                    sym,
            # Positional context
            "conviction_level":          radar["conviction_level"],
            "conviction_label":          radar["conviction_label"],
            "conviction_emoji":          radar["conviction_emoji"],
            "conviction_rank":           radar["conviction_rank"],
            "ignition":                  radar["ignition"],
            "positional_signal":         radar["positional_signal"],
            "positional_bias":           positional_bias,
            "consistency_pct":           radar["consistency_pct"],
            "pcr_series":                radar["pcr_series"],
            # Intraday confirmation
            "intraday_signal_type":      intraday.get("signal_type"),
            "intraday_label":            intraday.get("label"),
            "intraday_bias":             intraday_bias,
            "intraday_oi_chg_pct":       intraday.get("oi_chg_pct"),
            "intraday_price_chg_pct":    intraday.get("price_chg_pct"),
            "intraday_persistence_pct":  intraday.get("persistence_pct"),
            "intraday_cpr_position":     intraday.get("cpr_position"),
            "intraday_options_confirms": intraday.get("options_confirms"),
            "intraday_vol_ratio":        intraday.get("vol_ratio", 0),
            "intraday_vol_rank_label":   intraday.get("vol_rank_label", ""),
            "cmp":                       intraday.get("cmp"),
            "ce_wall":                   intraday.get("ce_wall"),
            "pe_wall":                   intraday.get("pe_wall"),
        })

    # Sort: ignition first → conviction rank → consistency
    watch_today.sort(key=lambda x: (
        -int(x["ignition"]),
        -x["conviction_rank"],
        -x["consistency_pct"],
    ))

    return {
        "date":             today,
        "last_radar_date":  last_trading_day,
        "watch_today":      watch_today,
        "total":            len(watch_today),
        "radar_total":      len(radar_stocks),
        "intraday_total":   len(intraday_map),
        "message":          None if watch_today else "No stocks with aligned positional + intraday signals today"
    }
