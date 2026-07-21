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

    # Current + previous-benchmark expiries, computed dynamically each run
    curr_expiry, next_expiry, prev_curr_expiry, prev_next_expiry, prev_bench_date = \
        _get_rollover_expiries(datetime.now(ist).date())

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

    # ── 2. Previous series benchmark OI ───────────────────────────────────────
    try:
        # Get one snapshot from prev bench date
        prev_ts_res = supabase.from_("oi_snapshots_archive")\
            .select("timestamp")\
            .eq("option_type", "FUT")\
            .eq("symbol", "NIFTY")\
            .in_("expiry", [prev_curr_expiry, prev_next_expiry])\
            .gte("timestamp", f"{prev_bench_date}T03:45:00+00:00")\
            .lt("timestamp",  f"{prev_bench_date}T12:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        prev_ts = prev_ts_res.data[0]["timestamp"] if prev_ts_res.data else None

        if not prev_ts:
            prev_rows = []
        else:
            prev_res = supabase.from_("oi_snapshots_archive")\
                .select("symbol, expiry, oi")\
                .eq("option_type", "FUT")\
                .in_("expiry", [prev_curr_expiry, prev_next_expiry])\
                .eq("timestamp", prev_ts)\
                .limit(1000)\
                .execute()
            prev_rows = prev_res.data or []
            print(f"[ROLLOVER] Prev rows: {len(prev_rows)} at {prev_ts}")
    except Exception as e:
        print(f"[ROLLOVER] Prev fetch failed: {e}")
        prev_rows = []

    prev_curr = {}
    prev_next = {}
    seen_pc = set()
    seen_pn = set()

    for r in prev_rows:
        sym = r["symbol"]
        exp = r.get("expiry")
        oi  = int(r.get("oi") or 0)
        if exp == prev_curr_expiry and sym not in seen_pc:
            prev_curr[sym] = oi
            seen_pc.add(sym)
        elif exp == prev_next_expiry and sym not in seen_pn:
            prev_next[sym] = oi
            seen_pn.add(sym)

    # ── 3. Daily OI summary for price change (signal direction) ───────────────
    try:
        sig_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_signal, price_chg_pct, close_price")\
            .eq("trade_date", last_trading_date)\
            .limit(200)\
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
                roll_label  = "Rolling"
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
    from datetime import date
    expiry_date = date(2026, 6, 30)
    today_date  = datetime.now(ist).date()
    dte = (expiry_date - today_date).days

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
