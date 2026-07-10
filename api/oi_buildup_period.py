"""
oi_buildup_period.py
Rolling weekly (5 trading days) and monthly (20 trading days) FUT OI buildup
ranking — reuses daily_oi_summary.fut_oi_chg_pct, compounded across the
window to get a true cumulative change (not a simple sum, since daily %
changes compound rather than add).
"""
from datetime import datetime, timedelta

PERIOD_DAYS = {"weekly": 5, "monthly": 20}


def classify(oi_pct, price_pct):
    MIN = 0.3
    if oi_pct > 0 and price_pct >= MIN:
        return "LONG_BUILDUP", "Long Buildup"
    if oi_pct > 0 and price_pct <= -MIN:
        return "SHORT_BUILDUP", "Short Buildup"
    if oi_pct < 0 and price_pct >= MIN:
        return "SHORT_COVERING", "Short Covering"
    if oi_pct < 0 and price_pct <= -MIN:
        return "LONG_UNWINDING", "Long Unwinding"
    return "NEUTRAL", "Neutral"


def get_oi_buildup_period(supabase, period: str = "weekly") -> dict:
    days = PERIOD_DAYS.get(period, 5)

    today = datetime.now().date()

    # "Monthly" means "since the current F&O series started", NOT a blind
    # rolling 20-day window — a rolling window can cross a monthly rollover,
    # where FUT OI resets to a new contract. That produces one artificially
    # huge daily % change (comparing fresh low OI against the old expiring
    # contract) which then wrecks the whole cumulative figure once compounded.
    # Weekly stays a simple rolling window since a week rarely crosses rollover.
    series_start = None
    if period == "monthly":
        try:
            from api.positional_radar import get_monthly_expiry, get_series_start
            expiry = get_monthly_expiry(today.year, today.month)
            series_start = get_series_start(expiry)
        except Exception as e:
            print(f"[OIBuildup] Series start lookup failed, falling back to rolling window: {e}")

    lookback_start = series_start if series_start else (today - timedelta(days=int(days * 2.2) + 5)).isoformat()

    try:
        rows_res = supabase.from_("daily_oi_summary") \
            .select("symbol, trade_date, fut_oi_chg_pct, price_chg_pct, close_price, fut_vol") \
            .gte("trade_date", lookback_start) \
            .order("trade_date", desc=False) \
            .limit(10000).execute()
    except Exception as e:
        return {"error": str(e), "results": []}

    by_symbol: dict = {}
    for r in (rows_res.data or []):
        by_symbol.setdefault(r["symbol"], []).append(r)

    results = []
    for sym, rows in by_symbol.items():
        rows_sorted = sorted(rows, key=lambda r: r["trade_date"])
        window = rows_sorted if series_start else rows_sorted[-days:]
        min_days = 3 if series_start else days
        if len(window) < min_days:
            continue

        oi_factor = 1.0
        price_factor = 1.0
        vol_total = 0
        for r in window:
            oi_pct = float(r.get("fut_oi_chg_pct") or 0)
            price_pct = float(r.get("price_chg_pct") or 0)
            oi_factor *= (1 + oi_pct / 100)
            price_factor *= (1 + price_pct / 100)
            vol_total += int(r.get("fut_vol") or 0)

        cumulative_oi_pct = round((oi_factor - 1) * 100, 2)
        cumulative_price_pct = round((price_factor - 1) * 100, 2)

        if abs(cumulative_oi_pct) < 2.0:
            continue

        sig_type, sig_label = classify(cumulative_oi_pct, cumulative_price_pct)
        if sig_type == "NEUTRAL":
            continue

        actual_days = len(window)
        results.append({
            "symbol": sym,
            "period": period,
            "trading_days": actual_days,
            "start_date": window[0]["trade_date"],
            "end_date": window[-1]["trade_date"],
            "cumulative_oi_pct": cumulative_oi_pct,
            "cumulative_price_pct": cumulative_price_pct,
            "close_price": float(window[-1].get("close_price") or 0),
            "avg_daily_fut_vol": round(vol_total / actual_days) if actual_days else 0,
            "signal_type": sig_type,
            "signal_label": sig_label,
        })

    results.sort(key=lambda x: -abs(x["cumulative_oi_pct"]))

    return {
        "period": period,
        "trading_days": days,
        "total": len(results),
        "results": results[:30],
        "long_bias": sum(1 for r in results if r["signal_type"] == "LONG_BUILDUP"),
        "short_bias": sum(1 for r in results if r["signal_type"] == "SHORT_BUILDUP"),
    }
