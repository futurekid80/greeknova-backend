"""
api/watch_today.py
Cross-references yesterday's Positional Radar (Conviction/Ignition stocks)
with today's Intraday Scanner activity.
Shows: stocks that were flagged positionally AND are active intraday today.
"""
from datetime import datetime, timezone, timedelta
from utils.db import get_supabase


def get_watch_today(supabase):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    # Walk back to last trading day
    check = datetime.now(timezone.utc) - timedelta(days=1)
    while check.weekday() >= 5:
        check -= timedelta(days=1)
    last_trading_day = check.strftime('%Y-%m-%d')

    # ── Step 1: Get yesterday's Positional Radar — Conviction+ stocks ────────
    # Get series start dynamically
    from api.positional_radar import get_monthly_expiry, get_series_start
    import datetime as _dt
    _today_date = _dt.date.today()
    _expiry = get_monthly_expiry(_today_date.year, _today_date.month)
    _series_start = get_series_start(_expiry)

    radar_result = supabase.rpc("get_positional_radar_eod_fast", {
        "p_series_start": _series_start,
        "p_series_end": last_trading_day
    }).execute()

    radar_stocks = {}
    for r in (radar_result.data or []):
        level = r.get("conviction_level", "")
        rank = r.get("conviction_rank", 0)
        ignition = r.get("ignition", False)
        # Include BUILDING+ (rank>=2) — lowers bar when market has no Conviction/Ignition
        if rank >= 2 or ignition:
            radar_stocks[r["symbol"]] = {
                "symbol": r["symbol"],
                "conviction_level": level,
                "conviction_label": r.get("conviction_label", ""),
                "conviction_emoji": r.get("conviction_emoji", ""),
                "conviction_rank": rank,
                "ignition": ignition,
                "signal": r.get("signal", ""),
                "bias": r.get("bias", ""),
                "consistency_pct": r.get("consistency_pct", 0),
                "pcr_series": r.get("pcr_series", 0),
            }

    if not radar_stocks:
        return {
            "date": today,
            "watch_today": [],
            "total": 0,
            "message": "No Conviction/Ignition stocks from yesterday's Positional Radar"
        }

    # ── Step 2: Get today's intraday signals ──────────────────────────────────
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

    # ── Step 3: Cross-reference ───────────────────────────────────────────────
    watch_today = []
    for sym, radar in radar_stocks.items():
        intraday = intraday_map.get(sym)

        # Alignment check — positional and intraday biases match?
        aligned = None
        if intraday:
            positional_bias = radar["bias"]
            intraday_bias = intraday.get("bias", "")
            aligned = positional_bias == intraday_bias

        watch_today.append({
            "symbol": sym,
            # Positional Radar context
            "conviction_level": radar["conviction_level"],
            "conviction_label": radar["conviction_label"],
            "conviction_emoji": radar["conviction_emoji"],
            "conviction_rank": radar["conviction_rank"],
            "ignition": radar["ignition"],
            "positional_signal": radar["signal"],
            "positional_bias": radar["bias"],
            "consistency_pct": radar["consistency_pct"],
            # Today's intraday activity
            "has_intraday_signal": intraday is not None,
            "intraday_signal_type": intraday.get("signal_type") if intraday else None,
            "intraday_label": intraday.get("label") if intraday else None,
            "intraday_bias": intraday.get("bias") if intraday else None,
            "intraday_oi_chg_pct": intraday.get("oi_chg_pct") if intraday else None,
            "intraday_price_chg_pct": intraday.get("price_chg_pct") if intraday else None,
            "intraday_persistence_pct": intraday.get("persistence_pct") if intraday else None,
            "intraday_cpr_position": intraday.get("cpr_position") if intraday else None,
            "intraday_options_confirms": intraday.get("options_confirms") if intraday else None,
            "cmp": intraday.get("cmp") if intraday else None,
            # Alignment
            "aligned": aligned,
        })

    # Sort: ignition first, then by conviction rank, then aligned on top
    watch_today.sort(key=lambda x: (
        -int(x["ignition"]),
        -x["conviction_rank"],
        -int(x["aligned"] or False),
        -int(x["has_intraday_signal"])
    ))

    return {
        "date": today,
        "last_radar_date": last_trading_day,
        "watch_today": watch_today,
        "total": len(watch_today),
        "with_intraday": sum(1 for w in watch_today if w["has_intraday_signal"]),
        "aligned": sum(1 for w in watch_today if w["aligned"]),
    }
