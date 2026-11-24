"""
rollover.py - Series Rollover Tracker
Shows current month vs next month FUT OI rollover status.
Compares vs previous series benchmark for context.
"""
from datetime import datetime, timezone, timedelta
import calendar


def _last_tuesday(year: int, month: int):
    """NSE monthly F&O expiry: last Tuesday of the month (post FAOP68747 circular)."""
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day).date()
    while d.weekday() != 1:  # Tuesday
        d -= timedelta(days=1)
    return d


def _shift_month(year: int, month: int, n: int):
    m = month - 1 + n
    y = year + m // 12
    m = m % 12 + 1
    return y, m


def _get_rollover_expiries(today_date):
    """BUG FIX (Jul 20 2026): expiry dates used to be hardcoded literals
    (e.g. '2026-06-30' / '2026-07-28') that went stale the moment the
    'current' expiry passed -- comparing OI against a dead, already-settled
    contract with zero live snapshots, which silently emptied the whole
    tracker every month once that date rolled by. Now computed fresh from
    today's date every time the function runs, so it self-corrects across
    every future expiry cycle without needing a manual code edit."""
    y, m = today_date.year, today_date.month
    curr = _last_tuesday(y, m)
    if today_date > curr:
        y, m = _shift_month(y, m, 1)
        curr = _last_tuesday(y, m)
    ny, nm = _shift_month(y, m, 1)
    nxt = _last_tuesday(ny, nm)
    py, pm = _shift_month(y, m, -1)
    prev_curr = _last_tuesday(py, pm)
    prev_next = curr
    prev_bench = prev_curr
    return (curr.strftime('%Y-%m-%d'), nxt.strftime('%Y-%m-%d'),
            prev_curr.strftime('%Y-%m-%d'), prev_next.strftime('%Y-%m-%d'),
            prev_bench.strftime('%Y-%m-%d'))


def _trading_days_to(ref, expiry):
    """Trading days after `ref` up to and including `expiry` (0 when ref is the expiry day or later)."""
    from datetime import timedelta as _td
    try:
        from utils.market_calendar import is_trading_day
    except Exception:
        is_trading_day = lambda d: d.weekday() < 5
    n, d = 0, ref
    while d < expiry:
        d += _td(days=1)
        if is_trading_day(d):
            n += 1
    return n


def _get_real_rollover_expiries(supabase, today_date):
    """Oct 23 2026: _get_rollover_expiries() assumes every NSE monthly F&O
    expiry falls on the last Tuesday of the month -- true since the
    FAOP68747 circular, EXCEPT when that Tuesday is itself a trading
    holiday, in which case NSE shifts it a day. Nov 2026 is exactly that
    case: the computed "last Tuesday" is Nov 24, but the exchange's real
    monthly expiry (confirmed straight from Kite's own instrument dump,
    the same data every snapshot is captured against) is Nov 23 -- a
    Monday. That single day of drift meant next_expiry never matched any
    row actually in oi_snapshots, so sym_next stayed permanently empty,
    all_syms = sym_curr & sym_next came out empty, and the whole tracker
    went blank -- not a pre-market timing issue, broken all day regardless
    of market hours, and it'll recur every time a future month's last
    Tuesday happens to be a holiday.

    Oct 26 2026 FIX: the original version of this fix queried BOTH
    oi_snapshots and oi_snapshots_archive to build the expiry list. The
    archive table is ~120M rows / 23GB with no index covering `expiry`,
    so that query blew the DB connection's statement_timeout on every
    single call since this landed on Oct 23 -- confirmed in Railway logs,
    every request falling back to the (wrong, for Nov 2026) calendar
    formula. The tracker has been silently blank continuously since then,
    not just today.

    Real fix: curr/next are BY DEFINITION not-yet-expired, so they are
    always present in the live oi_snapshots table (captured every 5 min
    all day) -- the archive table is never actually needed to find them.
    Only drop to the archive-free calendar formula for the prev/benchmark
    fields (the historical "vs last series" comparison columns, which
    don't block the tracker showing current data if they're approximate).
    """
    try:
        today_str = today_date.isoformat()
        r = supabase.from_("oi_snapshots")\
            .select("expiry")\
            .eq("option_type", "FUT")\
            .eq("symbol", "NIFTY")\
            .gte("expiry", today_str)\
            .execute()
        exps = sorted({str(row["expiry"])[:10] for row in (r.data or []) if row.get("expiry")})
        if len(exps) >= 2:
            curr_expiry, next_expiry = exps[0], exps[1]
            _, _, prev_curr_expiry, prev_next_expiry, prev_bench_date = _get_rollover_expiries(today_date)
            return (curr_expiry, next_expiry, prev_curr_expiry, prev_next_expiry, prev_bench_date)
    except Exception as e:
        print(f"[ROLLOVER] Real-expiry lookup failed, falling back to calendar formula: {e}")
    curr, nxt, prev_curr, prev_next, prev_bench = _get_rollover_expiries(today_date)
    return (curr, nxt, prev_curr, prev_next, prev_bench)


def get_rollover(supabase):
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).strftime('%Y-%m-%d')

    # Find last available trading date (handles holidays/weekends)
    try:
        last_date_res = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("option_type", "FUT")\
            .eq("symbol", "NIFTY")\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        if last_date_res.data:
            last_ts = last_date_res.data[0]["timestamp"]
            last_trading_date = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).astimezone(ist).strftime('%Y-%m-%d')
        else:
            last_trading_date = today
    except Exception as _e:
        print(f"[ROLLOVER] last_trading_date fallback: {_e}")
        last_trading_date = today

    print(f"[ROLLOVER] Using trading date: {last_trading_date} (today={today})")

    # Current + previous-benchmark expiries, snapped to the real captured
    # FUT expiry calendar rather than trusting the last-Tuesday formula
    # alone (see _get_real_rollover_expiries).
    # Nov 24 2026: use the last date that actually has data as the reference,
    # not the calendar date. On the Tue 24 Nov holiday the calendar had moved
    # on to the Dec/Jan series while the data was still the 23 Nov close, so
    # the page was labelled Dec/Jan over Nov/Dec numbers and benchmarked
    # against a day from the wrong end of the previous series.
    from datetime import date as _d0
    try:
        _ref_date = min(_d0.fromisoformat(last_trading_date), datetime.now(ist).date())
    except Exception:
        _ref_date = datetime.now(ist).date()
    curr_expiry, next_expiry, prev_curr_expiry, prev_next_expiry, prev_bench_date = \
        _get_real_rollover_expiries(supabase, _ref_date)

    # ── 1. Today's OI for both expiries ───────────────────────────────────────
    try:
        # Get latest timestamp first
        ts_res = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("option_type", "FUT")\
            .eq("symbol", "NIFTY")\
            .in_("expiry", [curr_expiry, next_expiry])\
            .gte("timestamp", f"{last_trading_date}T03:45:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        latest_ts = ts_res.data[0]["timestamp"] if ts_res.data else None

        if not latest_ts:
            today_rows = []
        else:
            today_res = supabase.from_("oi_snapshots")\
                .select("symbol, expiry, oi, last_price")\
                .eq("option_type", "FUT")\
                .in_("expiry", [curr_expiry, next_expiry])\
                .eq("timestamp", latest_ts)\
                .limit(1000)\
                .execute()
            today_rows = today_res.data or []
            print(f"[ROLLOVER] Today rows: {len(today_rows)} at {latest_ts}")
    except Exception as e:
        print(f"[ROLLOVER] Today fetch failed: {e}")
        today_rows = []

    # Build per-symbol latest OI for each expiry
    sym_curr = {}  # symbol -> {oi, price}
    sym_next = {}
    seen_curr = set()
    seen_next = set()

    for r in today_rows:
        sym = r["symbol"]
        exp = r.get("expiry")
        oi  = int(r.get("oi") or 0)
        price = float(r.get("last_price") or 0)

        if exp == curr_expiry and sym not in seen_curr:
            sym_curr[sym] = {"oi": oi, "price": price}
            seen_curr.add(sym)
        elif exp == next_expiry and sym not in seen_next:
            sym_next[sym] = {"oi": oi, "price": price}
            seen_next.add(sym)

    # ── 1b. Prefer FINAL end-of-day OI once the day is done ────────────────────
    # Nov 23 2026 audit: the last intraday snapshot understated rollover
    # (84.6% live vs 94.0% final NSE close) because expiry-day OI keeps
    # moving until the close. daily_oi_summary holds the final per-stock
    # near/next OI, so use it when the session is over (or the date is past)
    # and it has next-month data for most symbols.
    eod_used = False
    try:
        now_ist = datetime.now(ist)
        session_over = (last_trading_date < today) or (now_ist.hour * 60 + now_ist.minute >= 16 * 60)
        if session_over:
            eod = supabase.from_("daily_oi_summary")\
                .select("symbol, fut_oi_close, fut_oi_close_next, close_price")\
                .eq("trade_date", last_trading_date)\
                .limit(1000)\
                .execute().data or []
            usable = [r for r in eod if (r.get("fut_oi_close") or 0) > 0 and (r.get("fut_oi_close_next") or 0) > 0]
            if len(usable) >= 100:
                sym_curr, sym_next = {}, {}
                for r in usable:
                    px = float(r.get("close_price") or 0)
                    sym_curr[r["symbol"]] = {"oi": int(r["fut_oi_close"]), "price": px}
                    sym_next[r["symbol"]] = {"oi": int(r["fut_oi_close_next"]), "price": px}
                eod_used = True
                print(f"[ROLLOVER] Using final EOD OI from daily_oi_summary: {len(usable)} symbols")
    except Exception as e:
        print(f"[ROLLOVER] EOD override failed, keeping snapshot data: {e}")

    # ── 2. Previous series benchmark OI (same days-to-expiry, final EOD) ──────
    # Nov 23 2026 audit: the old archive lookup used the calendar "last
    # Tuesday" (Nov 24) as the prev series' next expiry -- the real one is Nov
    # 23 -- and the archive table is too big to query reliably, so every
    # symbol showed a 0% benchmark ("Faster than last series" for all).
    # daily_oi_summary has final near/next OI per day: pick the date with the
    # same number of trading days before the previous expiry.
    prev_curr = {}
    prev_next = {}
    try:
        from datetime import date as _date
        _dte = _trading_days_to(_ref_date, _date.fromisoformat(curr_expiry))
        d_res = supabase.from_("daily_oi_summary")\
            .select("trade_date")\
            .eq("symbol", "NIFTY")\
            .lte("trade_date", prev_curr_expiry)\
            .order("trade_date", desc=True)\
            .limit(40)\
            .execute()
        dates = [str(r["trade_date"])[:10] for r in (d_res.data or [])]
        if dates:
            # same number of trading days before the previous expiry
            bench_idx = min(_dte, len(dates) - 1)
            prev_bench_date = dates[bench_idx]
            pr = supabase.from_("daily_oi_summary")\
                .select("symbol, fut_oi_close, fut_oi_close_next")\
                .eq("trade_date", prev_bench_date)\
                .limit(1000)\
                .execute().data or []
            for r in pr:
                if (r.get("fut_oi_close") or 0) > 0 and (r.get("fut_oi_close_next") or 0) > 0:
                    prev_curr[r["symbol"]] = int(r["fut_oi_close"])
                    prev_next[r["symbol"]] = int(r["fut_oi_close_next"])
            print(f"[ROLLOVER] Benchmark {prev_bench_date}: {len(prev_curr)} symbols")
    except Exception as e:
        print(f"[ROLLOVER] Benchmark fetch failed: {e}")

    # ── 3. Daily OI summary for price change (signal direction) ───────────────
    try:
        sig_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_signal, price_chg_pct, close_price")\
            .eq("trade_date", last_trading_date)\
            .limit(500)\
            .execute()
        sig_map = {r["symbol"]: r for r in (sig_res.data or [])}
    except:
        sig_map = {}

    # ── 4. Compute rollover metrics ───────────────────────────────────────────
    all_syms = set(sym_curr.keys()) & set(sym_next.keys())

    results = []
    total_curr_oi = 0
    total_next_oi = 0
    total_prev_curr = 0
    total_prev_next = 0

    for sym in all_syms:
        curr_oi = sym_curr[sym]["oi"]
        next_oi = sym_next[sym]["oi"]
        price   = sym_curr[sym]["price"]

        if curr_oi == 0 and next_oi == 0:
            continue

        total = curr_oi + next_oi
        rollover_pct = round(next_oi / total * 100, 1) if total > 0 else 0

        # Historical benchmark
        pc = prev_curr.get(sym, 0)
        pn = prev_next.get(sym, 0)
        prev_total = pc + pn
        prev_rollover_pct = round(pn / prev_total * 100, 1) if prev_total > 0 else None
        vs_prev = round(rollover_pct - prev_rollover_pct, 1) if prev_rollover_pct is not None else None

        # Signal direction
        sig_data = sig_map.get(sym, {})
        fut_signal = sig_data.get("fut_signal") or ""
        price_chg  = float(sig_data.get("price_chg_pct") or 0)

        # Rollover signal logic
        if rollover_pct >= 15:
            if fut_signal in ("LONG_BUILDUP", "SHORT_COVERING") or price_chg >= 0.3:
                roll_signal = "BULLISH_ROLL"
                roll_label  = "Bullish Roll"
                roll_color  = "emerald"
            elif fut_signal in ("SHORT_BUILDUP", "LONG_UNWINDING") or price_chg <= -0.3:
                roll_signal = "BEARISH_ROLL"
                roll_label  = "Bearish Roll"
                roll_color  = "red"
            else:
                roll_signal = "ROLLING"
                roll_label  = "Flat Roll"
                roll_color  = "amber"
        elif curr_oi > 0 and next_oi < curr_oi * 0.1:
            roll_signal = "SQUARING"
            roll_label  = "Squaring Off"
            roll_color  = "gray"
        else:
            roll_signal = "EARLY"
            roll_label  = "Early Stage"
            roll_color  = "blue"

        # Rollover speed vs last series
        speed_label = None
        if vs_prev is not None:
            if vs_prev >= 5:
                speed_label = "Faster than last series"
            elif vs_prev <= -5:
                speed_label = "Slower than last series"
            else:
                speed_label = "Similar to last series"

        total_curr_oi += curr_oi
        total_next_oi += next_oi
        if pc: total_prev_curr += pc
        if pn: total_prev_next += pn

        results.append({
            "symbol":            sym,
            "curr_oi":           curr_oi,
            "next_oi":           next_oi,
            "rollover_pct":      rollover_pct,
            "prev_rollover_pct": prev_rollover_pct,
            "vs_prev":           vs_prev,
            "roll_signal":       roll_signal,
            "roll_label":        roll_label,
            "roll_color":        roll_color,
            "speed_label":       speed_label,
            "price":             price,
            "price_chg_pct":     round(price_chg, 2),
            "fut_signal":        fut_signal,
        })

    # Sort by rollover % descending
    results.sort(key=lambda x: x["rollover_pct"], reverse=True)

    # ── 5. Overall market rollover ────────────────────────────────────────────
    mkt_total = total_curr_oi + total_next_oi
    mkt_rollover_pct = round(total_next_oi / mkt_total * 100, 1) if mkt_total > 0 else 0

    prev_mkt_total = total_prev_curr + total_prev_next
    prev_mkt_rollover_pct = round(total_prev_next / prev_mkt_total * 100, 1) if prev_mkt_total > 0 else None

    # Days to expiry
    # BUG FIX (Oct 26 2026): expiry_date was a hardcoded literal
    # (date(2026, 6, 30)) left stale from whenever this was first written --
    # showed "-118 days to expiry" live on Oct 26 instead of the real ~1 day
    # to the Oct 27 monthly expiry. curr_expiry is already the real,
    # correctly-derived current expiry (see _get_real_rollover_expiries
    # above) -- just use that instead of a separate guess.
    from datetime import date
    expiry_date = date.fromisoformat(curr_expiry)
    today_date  = datetime.now(ist).date()
    dte = max((expiry_date - today_date).days, 0)  # 0 on the expiry day and while its close is still the latest data

    # Top rollers by rollover %
    top_rollers = sorted(results, key=lambda x: x["rollover_pct"], reverse=True)[:15]

    # Signal summary counts
    signal_counts = {}
    for r in results:
        signal_counts[r["roll_signal"]] = signal_counts.get(r["roll_signal"], 0) + 1

    return {
        "curr_expiry":          curr_expiry,
        "next_expiry":          next_expiry,
        "prev_bench_date":      prev_bench_date,
        "dte":                  dte,
        "market_rollover_pct":  mkt_rollover_pct,
        "prev_market_rollover": prev_mkt_rollover_pct,
        "vs_prev_market":       round(mkt_rollover_pct - prev_mkt_rollover_pct, 1) if prev_mkt_rollover_pct else None,
        "total_curr_oi":        total_curr_oi,
        "total_next_oi":        total_next_oi,
        "signal_counts":        signal_counts,
        "top_rollers":          top_rollers,
        "symbols":              results,
        "total_symbols":        len(results),
    }
