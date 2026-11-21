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

    # BUG FIX (Aug 29 2026): series_start was only looked up for
    # "monthly" -- but a plain 5-trading-day rolling window for
    # "weekly" can ALSO cross a series rollover whenever expiry falls
    # within the last 5 trading days (same artificial-huge-single-day-%
    # problem the monthly path was already protected against, just
    # rarer). Confirmed live: Aug's series expired inside the
    # 24-28 Aug weekly window, showing ~-50 to -60% "OI collapse" on
    # many stocks simultaneously that was really just the new
    # September contract's naturally-low starting OI being compared
    # against August's built-up OI, not genuine unwinding. Now looked
    # up for both periods; used below to trim (not just anchor) the
    # weekly window so it never straddles two different contracts.
    series_start = None
    try:
        from api.positional_radar import get_current_expiry, get_series_start
        expiry = get_current_expiry(today)
        series_start = get_series_start(expiry)
    except Exception as e:
        print(f"[OIBuildup] Series start lookup failed, falling back to rolling window: {e}")

    # Nov 21 2026: rollover week. In the last days before near-month expiry, near-month OI drains into the
    # next month, so a near-only cumulative OI change reads as huge unwinding / short covering on almost every
    # stock (seen live: 84% short covering, 1 short buildup). The honest measure then is the change in near +
    # next month OI together. Outside rollover week nothing changes.
    rollover = False
    try:
        from services.fut_oi_final import ROLLOVER_DAYS
        from api.positional_radar import get_current_expiry as _gce
        _exp = datetime.strptime(_gce(today), "%Y-%m-%d").date()
        rollover = 0 <= (_exp - today).days <= ROLLOVER_DAYS
    except Exception as e:
        print(f"[OIBuildup] Rollover check failed (non-fatal, near-month basis used): {e}")

    # Nov 22 2026: first days of a new series. Right after expiry the new series has too few trading days for
    # these views, so they would be blank for days. In that window the view bridges across the expiry using
    # near + next month combined (rollover-neutral), so fetch a normal look-back instead of series-start only.
    recent_series = False
    try:
        recent_series = bool(series_start) and 0 <= (today - datetime.strptime(series_start, "%Y-%m-%d").date()).days <= 10
    except Exception:
        recent_series = False
    any_bridge = False

    if period == "monthly" and series_start and not recent_series:
        lookback_start = series_start
    elif period == "monthly" and recent_series:
        lookback_start = (today - timedelta(days=32)).isoformat()  # ~21 trading days, stays under the row limit
    else:
        lookback_start = (today - timedelta(days=int(days * 4.4) + 7)).isoformat()

    try:
        rows_res = supabase.from_("daily_oi_summary") \
            .select("symbol, trade_date, fut_oi_chg_pct, price_chg_pct, fut_price_chg_pct, close_price, fut_vol, fut_oi_close, fut_oi_close_next") \
            .gte("trade_date", lookback_start) \
            .order("trade_date", desc=False) \
            .limit(10000).execute()
    except Exception as e:
        return {"error": str(e), "results": []}

    by_symbol: dict = {}
    for r in (rows_res.data or []):
        by_symbol.setdefault(r["symbol"], []).append(r)

    prev_by_symbol: dict = {}
    if period == "monthly" and series_start:
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
        if series_start:
            same_series_rows = [r for r in rows_sorted if r["trade_date"] >= series_start]
        else:
            same_series_rows = rows_sorted
        if period == "monthly":
            window = same_series_rows
            min_days = 3
        else:
            # BUG FIX (Aug 29 2026): was rows_sorted[-days:] with no
            # series awareness -- trim to same-series rows first, THEN
            # take the last `days` of those. Right after a rollover this
            # naturally yields a shorter-than-5-day window (all from the
            # new series) instead of a 5-day window that silently spans
            # two different contracts.
            window = same_series_rows[-days:]
            min_days = 2
        bridged = False
        if len(window) < min_days:
            if not recent_series:
                continue
            window = rows_sorted[-(PERIOD_DAYS["monthly"] if period == "monthly" else days):]
            if len(window) < min_days:
                continue
            bridged = True
            any_bridge = True

        # BUG FIX (Oct 18 2026): reverting the Oct 15 2026 change above.
        # fut_price_chg_pct is each day's FUT open->close move only, so
        # compounding it across the window silently drops every overnight
        # gap in that window -- worse than the single-day version of this
        # bug fixed the same day in api/positional_intelligence.py (see
        # that fix for the full writeup: a FUT contract that gaps down hard
        # and then sits flat intraday shows ~0% here even though the stock
        # really moved several %). Cash price_chg_pct is close-to-close, so
        # compounding it across consecutive days correctly reconstructs the
        # window's real cumulative move.
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

        if rollover or bridged:
            # Change in TOTAL (near + next) closing OI: end of the window vs the day before it. When the window
            # starts on the first day of the series, the day before belongs to the expired contract, so that
            # first day's own close is the baseline and the price move is counted from the day after it.
            def _tot(row):
                n, x = row.get("fut_oi_close"), row.get("fut_oi_close_next")
                if n is None or x is None:
                    return None
                n, x = float(n), float(x)
                return n + x if n > 0 and x > 0 else None

            def _implied_prev(row):
                # yesterday's total OI as implied by today's own close and % changes
                try:
                    n, x = float(row["fut_oi_close"]), float(row["fut_oi_close_next"])
                    pn, px = float(row["fut_oi_chg_pct"]), float(row["fut_oi_chg_pct_next"])
                    if n <= 0 or x <= 0 or pn <= -100 or px <= -100:
                        return None
                    return n / (1 + pn / 100) + x / (1 + px / 100)
                except Exception:
                    return None

            first_idx = rows_sorted.index(window[0])
            if first_idx > 0 and (bridged or not series_start or rows_sorted[first_idx - 1]["trade_date"] >= series_start):
                base_row, calc_rows = rows_sorted[first_idx - 1], window
            else:
                base_row, calc_rows = window[0], window[1:]
            if not calc_rows:
                continue
            base_tot, end_tot = _tot(base_row), _tot(window[-1])
            if not end_tot:
                continue
            # Guard against a single bad stored close (IDEA 13 Nov held ~1.7B instead of ~6B, which showed a
            # fake +189%): if the baseline disagrees with what the next day's own % change implies by more
            # than 25%, trust the implied figure; if the last close disagrees with the day before it, skip.
            imp = _implied_prev(calc_rows[0])
            if imp and (not base_tot or abs(base_tot / imp - 1) > 0.25):
                base_tot = imp
            if not base_tot:
                continue
            if len(window) >= 2:
                prev_tot, last_imp = _tot(window[-2]), _implied_prev(window[-1])
                if prev_tot and last_imp and abs(prev_tot / last_imp - 1) > 0.25 and abs(end_tot / last_imp - 1) > 0.25:
                    continue
            cumulative_oi_pct = round((end_tot / base_tot - 1) * 100, 2)
            price_factor = 1.0
            for r in calc_rows:
                price_factor *= (1 + float(r.get("price_chg_pct") or 0) / 100)
            cumulative_price_pct = round((price_factor - 1) * 100, 2)

        if abs(cumulative_oi_pct) < 2.0:
            continue

        sig_type, sig_label = classify(cumulative_oi_pct, cumulative_price_pct)
        if sig_type == "NEUTRAL":
            continue

        actual_days = len(window)
        avg_vol = vol_total / actual_days if actual_days else 0

        prev_avg_vol = None
        if period == "monthly" and series_start:
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
        "rollover_week": rollover,
        "series_bridge": any_bridge,
        "results": results[:30],
        "long_bias": sum(1 for r in results if r["signal_type"] == "LONG_BUILDUP"),
        "short_bias": sum(1 for r in results if r["signal_type"] == "SHORT_BUILDUP"),
    }
