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
from datetime import datetime, date as date_type
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
            .eq("timestamp", timestamp)
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
