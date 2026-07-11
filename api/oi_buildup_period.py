"""
oi_buildup_period.py
Rolling weekly (5 trading days) and monthly (current series) FUT OI buildup
ranking — reuses daily_oi_summary.fut_oi_chg_pct, compounded across the
window to get a true cumulative change (not a simple sum, since daily %
changes compound rather than add).

"Monthly" means "since the current F&O series started", NOT a blind rolling
20-day window — a rolling window can cross a monthly rollover, where FUT OI
resets to a new contract. That produces one artificially huge daily % change
(comparing fresh low OI against the old expiring contract) which then wrecks
the whole cumulative figure once compounded. Weekly stays a simple rolling
window since a week rarely crosses rollover.

Also computes avg_daily_fut_vol as a RATIO vs the equivalent previous period
(prior 5 trading days for weekly, prior series for monthly), not a raw
absolute number — e.g. "1.2x" meaning 20% more daily volume than last period.
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


def _prev_month_series_start(today):
    """Get the series_start of the PREVIOUS monthly F&O series."""
    from api.positional_radar import get_monthly_expiry, get_series_start
    prev_month = today.month - 1
    prev_year = today.year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    prev_expiry = get_monthly_expiry(prev_year, prev_month)
    return get_series_start(prev_expiry)


def _get_net_delta_map(supabase):
    """
    PE OI minus CE OI at ATM±5 strikes, from the latest available options
    snapshot. Same pattern as positional_intelligence.py's stealth buildup
    net delta — tells you whether today's buildup is put-writing-heavy
    (support building below) or call-writing-heavy (resistance above),
    NOT averaged/summed across the week/month — a point-in-time reading,
    same as everywhere else this metric is shown.
    """
    net_delta_map: dict = {}
    try:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)
        today_str = now_ist.date().isoformat()

        from utils.market_calendar import is_trading_day
        check = now_ist.date()
        if not is_trading_day(check):
            check -= timedelta(days=1)
            while not is_trading_day(check):
                check -= timedelta(days=1)
        last_trading_day = check.isoformat()

        cmp_res = supabase.from_("cmp_prices") \
            .select("symbol, cmp") \
            .gte("timestamp", f"{last_trading_day}T00:00:00+00:00") \
            .lte("timestamp", f"{last_trading_day}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(500).execute()
        cmp_map: dict = {}
        seen = set()
        for r in (cmp_res.data or []):
            if r["symbol"] not in seen:
                cmp_map[r["symbol"]] = float(r["cmp"])
                seen.add(r["symbol"])

        latest_snap = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .gte("timestamp", f"{last_trading_day}T03:45:00+00:00") \
            .lt("timestamp", f"{last_trading_day}T11:00:00+00:00") \
            .order("timestamp", desc=True) \
            .limit(1).execute()
        if not latest_snap.data:
            return net_delta_map
        latest_ts = latest_snap.data[0]["timestamp"]

        options_res = supabase.from_("oi_snapshots") \
            .select("symbol, option_type, strike, oi") \
            .eq("timestamp", latest_ts) \
            .in_("option_type", ["CE", "PE"]) \
            .limit(10000).execute()

        sym_options: dict = {}
        for r in (options_res.data or []):
            sym_options.setdefault(r["symbol"], []).append(r)

        for sym, opts in sym_options.items():
            cmp_price = cmp_map.get(sym, 0)
            if cmp_price <= 0:
                continue
            strikes = sorted(set(float(r["strike"]) for r in opts if r.get("strike")))
            if not strikes:
                continue
            atm = min(strikes, key=lambda x: abs(x - cmp_price))
            atm_idx = strikes.index(atm)
            atm_range = strikes[max(0, atm_idx - 5): atm_idx + 6]
            pe_oi = sum(int(r.get("oi") or 0) for r in opts if float(r.get("strike") or 0) in atm_range and r["option_type"] == "PE")
            ce_oi = sum(int(r.get("oi") or 0) for r in opts if float(r.get("strike") or 0) in atm_range and r["option_type"] == "CE")
            net_delta_map[sym] = pe_oi - ce_oi
    except Exception as e:
        print(f"[OIBuildup] Net delta fetch failed (non-fatal): {e}")

    return net_delta_map


def get_oi_buildup_period(supabase, period: str = "weekly") -> dict:
    days = PERIOD_DAYS.get(period, 5)
    today = datetime.now().date()

    series_start = None
    if period == "monthly":
        try:
            from api.positional_radar import get_monthly_expiry, get_series_start
            expiry = get_monthly_expiry(today.year, today.month)
            series_start = get_series_start(expiry)
        except Exception as e:
            print(f"[OIBuildup] Series start lookup failed, falling back to rolling window: {e}")

    if series_start:
        lookback_start = series_start
    else:
        lookback_start = (today - timedelta(days=int(days * 4.4) + 7)).isoformat()

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

    prev_by_symbol: dict = {}
    if series_start:
        try:
            prev_start = _prev_month_series_start(today)
            prev_end = (datetime.strptime(series_start, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
            prev_res = supabase.from_("daily_oi_summary") \
                .select("symbol, trade_date, fut_vol") \
                .gte("trade_date", prev_start) \
                .lte("trade_date", prev_end) \
                .limit(10000).execute()
            for r in (prev_res.data or []):
                prev_by_symbol.setdefault(r["symbol"], []).append(r)
        except Exception as e:
            print(f"[OIBuildup] Previous series fetch failed (non-fatal, ratio will be omitted): {e}")

    net_delta_map = _get_net_delta_map(supabase)

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
        avg_vol = vol_total / actual_days if actual_days else 0

        prev_avg_vol = None
        if series_start:
            prev_rows = prev_by_symbol.get(sym, [])
            if prev_rows:
                prev_total = sum(int(r.get("fut_vol") or 0) for r in prev_rows)
                prev_avg_vol = prev_total / len(prev_rows) if prev_rows else None
        else:
            prev_window = rows_sorted[-(2 * days):-days] if len(rows_sorted) >= 2 * days else []
            if prev_window:
                prev_total = sum(int(r.get("fut_vol") or 0) for r in prev_window)
                prev_avg_vol = prev_total / len(prev_window)

        vol_ratio = round(avg_vol / prev_avg_vol, 2) if prev_avg_vol and prev_avg_vol > 0 else None

        results.append({
            "symbol": sym,
            "period": period,
            "trading_days": actual_days,
            "start_date": window[0]["trade_date"],
            "end_date": window[-1]["trade_date"],
            "cumulative_oi_pct": cumulative_oi_pct,
            "cumulative_price_pct": cumulative_price_pct,
            "close_price": float(window[-1].get("close_price") or 0),
            "avg_daily_fut_vol": round(avg_vol),
            "vol_ratio": vol_ratio,
            "net_delta": net_delta_map.get(sym),
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
