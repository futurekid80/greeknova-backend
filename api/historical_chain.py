"""
historical_chain.py - Historical Option Chain
Lets a user browse a past option chain: pick a date, pick a snapshot time
from that day, see the full CE/PE grid as it looked at that moment.

Data sources:
  - oi_snapshots          : recent data (rolling window, archived weekly)
  - oi_snapshots_archive  : older data moved out of oi_snapshots
Earliest available data: 2026-05-26 (nothing survives before that).

Both tables are queried and merged wherever a date could plausibly live in
either one, since the exact live/archive cutover date shifts week to week.
"""
from datetime import datetime, date as date_type, timedelta
from utils.db import get_supabase
from api.option_chain import calculate_iv, calculate_greeks

EARLIEST_AVAILABLE_DATE = "2026-05-26"


def _query_both_tables(supabase, build_query):
    """Run the same filtered query against oi_snapshots and
    oi_snapshots_archive, and merge the results. build_query takes a table
    query builder and returns it with filters applied."""
    rows = []
    for table in ("oi_snapshots", "oi_snapshots_archive"):
        try:
            q = build_query(supabase.from_(table))
            res = q.execute()
            if res.data:
                rows.extend(res.data)
        except Exception as e:
            print(f"[HIST_CHAIN] Query against {table} failed (non-fatal): {e}")
    return rows


def get_available_dates(symbol: str):
    """Which trade dates have data for this symbol. Uses daily_oi_summary
    as a lightweight index (one row per symbol per day) instead of scanning
    the much larger oi_snapshots/archive tables directly."""
    supabase = get_supabase()
    symbol = symbol.upper()

    res = supabase.from_("daily_oi_summary")\
        .select("trade_date")\
        .eq("symbol", symbol)\
        .gte("trade_date", EARLIEST_AVAILABLE_DATE)\
        .order("trade_date", desc=True)\
        .limit(500)\
        .execute()

    dates = sorted(set(str(r["trade_date"]) for r in (res.data or [])), reverse=True)
    return {"symbol": symbol, "dates": dates, "earliest_available": EARLIEST_AVAILABLE_DATE}


def get_available_snapshots(symbol: str, date_str: str):
    """Which snapshot timestamps exist for this symbol on this date."""
    supabase = get_supabase()
    symbol = symbol.upper()

    day_start = f"{date_str}T00:00:00+00:00"
    day_end   = f"{date_str}T23:59:59+00:00"

    def build(q):
        return q.select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", day_start)\
            .lte("timestamp", day_end)\
            .limit(20000)

    rows = _query_both_tables(supabase, build)
    timestamps = sorted(set(r["timestamp"] for r in rows))

    return {"symbol": symbol, "date": date_str, "snapshots": timestamps, "count": len(timestamps)}


def get_historical_chain(symbol: str, date_str: str, timestamp: str = None, expiry: str = None):
    """Reconstruct the full option chain as it looked at a given past
    timestamp. If timestamp isn't given, uses the last snapshot of that day
    (end-of-day view)."""
    supabase = get_supabase()
    symbol = symbol.upper()

    if not timestamp:
        snaps = get_available_snapshots(symbol, date_str)["snapshots"]
        if not snaps:
            return {"symbol": symbol, "date": date_str, "chain": [], "error": "No data for this date"}
        timestamp = snaps[-1]

    def build(q):
        qq = q.select("strike, option_type, oi, volume, last_price, expiry")\
            .eq("symbol", symbol)\
            .eq("timestamp", timestamp)\
            .limit(2000)
        if expiry:
            qq = qq.eq("expiry", expiry)
        return qq

    rows = _query_both_tables(supabase, build)
    if not rows:
        return {"symbol": symbol, "date": date_str, "timestamp": timestamp, "chain": [],
                "error": "No data at this timestamp"}

    # BUG FIX (Jul 22 2026): oi_snapshots stores a single FUT placeholder
    # row (strike=0.00) for far-month contracts alongside the real CE/PE
    # option chain rows for near-month expiries. These FUT rows were
    # getting swept into available_expiries, so a far-month expiry with
    # zero real strikes could get auto-selected as "active", producing a
    # single all-zero row instead of a real chain. Only expiries that
    # actually have CE/PE strike data are offered as choices.
    option_rows = [r for r in rows if r["option_type"] in ("CE", "PE")]
    available_expiries = sorted(set(r["expiry"] for r in option_rows))
    if not available_expiries:
        return {"symbol": symbol, "date": date_str, "timestamp": timestamp, "chain": [],
                "error": "No option chain data at this timestamp (futures only)"}

    active_expiry = expiry if expiry in available_expiries else available_expiries[0]
    rows = [r for r in option_rows if r["expiry"] == active_expiry]

    # T is relative to the snapshot's own date, not today
    exp_date = datetime.strptime(active_expiry, "%Y-%m-%d").date()
    snap_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    days_left = (exp_date - snap_date).days
    T = max(days_left, 0.5) / 365
    r_f = 0.065

    # No live Kite quote for a historical moment — estimate spot from the
    # strike where CE and PE premiums were closest (put-call parity proxy),
    # same fallback the live option chain uses when Kite is unavailable.
    strikes_dict = {}
    for row in rows:
        s = row["strike"]
        strikes_dict.setdefault(s, {})[row["option_type"]] = row["last_price"]
    best, best_diff = None, float("inf")
    for s, v in strikes_dict.items():
        if "CE" in v and "PE" in v and v["CE"] and v["PE"]:
            diff = abs(v["CE"] - v["PE"])
            if diff < best_diff:
                best_diff = diff
                best = s
    spot = best or rows[len(rows) // 2]["strike"]

    strikes = sorted(set(r["strike"] for r in rows if r["strike"] and r["strike"] > 0))
    ce_map = {r["strike"]: r for r in rows if r["option_type"] == "CE"}
    pe_map = {r["strike"]: r for r in rows if r["option_type"] == "PE"}
    atm = min(strikes, key=lambda s: abs(s - spot))

    chain = []
    for strike in strikes:
        ce = ce_map.get(strike, {})
        pe = pe_map.get(strike, {})
        ce_ltp = ce.get("last_price", 0) or 0
        pe_ltp = pe.get("last_price", 0) or 0

        ce_iv = calculate_iv(ce_ltp, spot, strike, T, r_f, True)
        pe_iv = calculate_iv(pe_ltp, spot, strike, T, r_f, False)
        ce_sig = (ce_iv / 100) if ce_iv else 0.25
        pe_sig = (pe_iv / 100) if pe_iv else 0.25

        chain.append({
            "strike": strike,
            "is_atm": strike == atm,
            "ce": {
                "ltp": ce_ltp, "iv": ce_iv,
                "oi": ce.get("oi", 0), "volume": ce.get("volume", 0),
                **calculate_greeks(spot, strike, T, r_f, ce_sig, True),
            },
            "pe": {
                "ltp": pe_ltp, "iv": pe_iv,
                "oi": pe.get("oi", 0), "volume": pe.get("volume", 0),
                **calculate_greeks(spot, strike, T, r_f, pe_sig, False),
            },
        })

    return {
        "symbol": symbol,
        "date": date_str,
        "timestamp": timestamp,
        "spot": spot,
        "expiry": active_expiry,
        "days_left": days_left,
        "expiries": available_expiries,
        "chain": chain,
    }


def get_strike_history(symbol: str, date_str: str, strike: float, option_type: str, expiry: str = None):
    """Oct 25 2026 (Manish): one strike's full-day OI + premium time series,
    so a user can see the real intraday move behind an Alert/UOA signal
    instead of taking the badge's word for it -- same oi_snapshots data
    this whole file already reads, just sliced by one strike across the
    whole day instead of one timestamp across the whole chain.

    If expiry isn't given, picks whichever expiry has the most rows for
    this strike (a stock/index can have this same strike listed under more
    than one expiry -- see the FUT-placeholder-row comment above for why
    not every expiry row is a real option)."""
    supabase = get_supabase()
    symbol = symbol.upper()
    option_type = option_type.upper()

    day_start = f"{date_str}T00:00:00+00:00"
    day_end   = f"{date_str}T23:59:59+00:00"

    def build(q):
        qq = q.select("timestamp, oi, last_price, volume, expiry")\
            .eq("symbol", symbol)\
            .eq("strike", strike)\
            .eq("option_type", option_type)\
            .gte("timestamp", day_start)\
            .lte("timestamp", day_end)\
            .order("timestamp")\
            .limit(5000)
        if expiry:
            qq = qq.eq("expiry", expiry)
        return qq

    rows = _query_both_tables(supabase, build)
    if not rows:
        return {
            "symbol": symbol, "date": date_str, "strike": strike,
            "option_type": option_type, "expiry": expiry, "points": [],
            "error": "No data for this strike on this date",
        }

    # Pick the active expiry (most rows) if the caller didn't pin one.
    if not expiry:
        counts: dict = {}
        for r in rows:
            counts[r["expiry"]] = counts.get(r["expiry"], 0) + 1
        expiry = max(counts, key=counts.get)
        rows = [r for r in rows if r["expiry"] == expiry]

    # One point per minute (dedupe the ~1min double-write some crons do),
    # keeping the LAST reading in each minute.
    by_minute: dict = {}
    for r in rows:
        minute_key = r["timestamp"][:16]  # "...T09:45" -- minute precision
        by_minute[minute_key] = r
    points = [
        {"timestamp": r["timestamp"], "oi": r["oi"], "last_price": r["last_price"], "volume": r["volume"]}
        for r in sorted(by_minute.values(), key=lambda r: r["timestamp"])
    ]

    open_ltp = points[0]["last_price"] if points else None
    last = points[-1] if points else None
    # OI change over the last ~5 min (same window the Alert cards use),
    # not the full day -- that's the number that actually matches what an
    # Alert/UOA badge is claiming. (Oct 25 2026: corrected from an earlier
    # 30-min assumption -- the real Alert window is ~5 min.)
    OI_WINDOW_MINUTES = 5
    window_start = None
    if last:
        last_t = datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00"))
        target = last_t - timedelta(minutes=OI_WINDOW_MINUTES)
        for p in points:
            pt = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
            if pt <= target:
                window_start = p
            else:
                break

    def pct(a, b):
        if not a:
            return None
        return round((b - a) / a * 100, 1)

    return {
        "symbol": symbol,
        "date": date_str,
        "strike": strike,
        "option_type": option_type,
        "expiry": expiry,
        "points": points,
        "summary": {
            "open_ltp": open_ltp,
            "last_ltp": last["last_price"] if last else None,
            "last_oi": last["oi"] if last else None,
            "ltp_from_open_pct": pct(open_ltp, last["last_price"]) if last else None,
            "oi_window_start": window_start["oi"] if window_start else (points[0]["oi"] if points else None),
            "oi_window_pct": pct(window_start["oi"], last["oi"]) if window_start and last else (
                pct(points[0]["oi"], last["oi"]) if points and last else None
            ),
            "oi_window_minutes": OI_WINDOW_MINUTES,
        },
    }
