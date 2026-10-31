"""Intraday straddle premium chart — NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX only for now.

Different job from Strategy Builder's straddle payoff diagram (which shows
P&L *at expiry* across a price range, computed client-side from a single
live quote). This shows how a straddle's actual combined premium (CE+PE)
moved *through the trading day* at a FIXED strike — the thing you'd actually
watch if you'd already sold/bought a straddle and wanted to see it decay or
expand in real time.

Deliberately fixes the strike for the whole day rather than tracking
"today's ATM" as spot moves: a floating-ATM series jumps every time the ATM
strike rolls to a new one, which looks like a premium spike/crash that has
nothing to do with actual time decay or IV change. Real straddle tracking
is always "the straddle I entered at strike X," so the strike is picked
once (default: ATM at the day's first capture) and held fixed; the caller
can override it via the `strike` param once they see the available list.
"""
from datetime import datetime, timezone, timedelta, date as date_type


def _build_points(strike_rows):
    """Same CE/PE-pairing logic used for the primary strike, factored out
    so the ATM+-1 comparison strikes (Oct 22 2026) can reuse it without a
    second DB round-trip -- they're sliced from the same exp_rows fetch."""
    by_ts: dict = {}
    for r in strike_rows:
        ts = r["timestamp"]
        if ts not in by_ts:
            by_ts[ts] = {}
        by_ts[ts][r["option_type"]] = float(r.get("last_price") or 0)

    points = []
    for ts in sorted(by_ts.keys()):
        row = by_ts[ts]
        ce = row.get("CE")
        pe = row.get("PE")
        if ce is None or pe is None:
            continue
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        points.append({
            "time":     int(dt.timestamp()),
            "ce":       round(ce, 2),
            "pe":       round(pe, 2),
            "combined": round(ce + pe, 2),
        })
    return points


def _compute_fut_vwap(supabase, symbol: str, date: str):
    """Oct 22 2026: intraday NIFTY/BANKNIFTY/etc FUTURES VWAP, built purely
    from the FUT rows the live capture job already writes into oi_snapshots
    every ~15s poll (kite.quote(), same mechanism as every other live
    feature) -- never kite.historical_data(), so this doesn't touch the
    Historical Data permission the production app lacks, and it isn't the
    raw index either (an index carries no real traded volume to weight by;
    futures volume is the standard stand-in).

    Kite's quote() "volume" is the day's CUMULATIVE traded volume as of
    that poll, not a per-interval figure, so each step's volume contribution
    is the delta from the previous poll. The first poll's whole volume is
    used as its own delta (we don't know what traded before our first
    capture) -- a minor first-bucket approximation, not a correctness bug.
    """
    try:
        r = supabase.from_("oi_snapshots")\
            .select("timestamp, last_price, volume")\
            .eq("symbol", symbol)\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{date}T00:00:00+00:00")\
            .lt("timestamp",  f"{date}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .execute()
    except Exception:
        return None

    rows = r.data or []
    if not rows:
        return None

    vwap_points = []
    cum_pv = 0.0
    cum_vol = 0.0
    prev_vol = None
    for row in rows:
        price = float(row.get("last_price") or 0)
        vol = float(row.get("volume") or 0)
        if price <= 0:
            continue
        delta = vol if prev_vol is None else max(0.0, vol - prev_vol)
        prev_vol = vol
        cum_pv += price * delta
        cum_vol += delta
        vwap = (cum_pv / cum_vol) if cum_vol > 0 else price
        dt = datetime.fromisoformat(row["timestamp"].replace('Z', '+00:00'))
        vwap_points.append({"time": int(dt.timestamp()), "price": round(price, 2), "vwap": round(vwap, 2)})

    if not vwap_points:
        return None

    latest = vwap_points[-1]
    return {
        "points":       vwap_points,
        "latest_price": latest["price"],
        "latest_vwap":  latest["vwap"],
        "above_vwap":   latest["price"] >= latest["vwap"],
    }


def get_straddle_chart(symbol: str = "NIFTY", strike: float = None, expiry: str = None, date: str = None):
    from utils.db import get_supabase
    supabase = get_supabase()

    symbol = symbol.upper()
    if symbol not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
        return {"error": "Straddle Chart currently supports NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX only"}

    today = datetime.now(timezone.utc).date()

    if not date:
        date = today.isoformat()

    # Find last available trading date with data (same pattern as oi_profile.py)
    for i in range(7):
        check = (today - timedelta(days=i)).isoformat()
        r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{check}T00:00:00+00:00")\
            .lt("timestamp",  f"{check}T23:59:59+00:00")\
            .limit(1).execute()
        if r.data:
            date = check
            break

    # Fetch the WHOLE day's CE/PE rows for this symbol, paginated (same
    # pagination pattern as oi_profile.py / pcr_trend.py / positional_intelligence.py
    # -- indices alone run ~50 strikes x up to 3 expiries x ~75 snapshots/day,
    # comfortably into five figures of rows).
    all_rows = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp, strike, option_type, oi, last_price, expiry")\
            .eq("symbol", symbol)\
            .in_("option_type", ["CE", "PE"])\
            .gte("timestamp", f"{date}T00:00:00+00:00")\
            .lt("timestamp",  f"{date}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not all_rows:
        return {"error": f"No data for {symbol} on {date}"}

    today_str = date_type.today().isoformat()
    available_expiries = sorted(set(
        r["expiry"] for r in all_rows if r["expiry"] and r["expiry"] >= today_str
    ))
    active_expiry = expiry or (available_expiries[0] if available_expiries else None)
    if not active_expiry:
        return {"error": "No active expiry found"}

    exp_rows = [r for r in all_rows if r["expiry"] == active_expiry]

    available_strikes = sorted(set(float(r["strike"]) for r in exp_rows if r.get("strike")))
    if not available_strikes:
        return {"error": "No strikes found for this expiry"}

    active_strike = strike
    if active_strike is None:
        # ATM at the day's FIRST capture, held fixed for the whole series.
        first_ts = min(r["timestamp"] for r in exp_rows)
        cmp_q = supabase.from_("cmp_prices")\
            .select("cmp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{date}T00:00:00+00:00")\
            .lte("timestamp", first_ts)\
            .order("timestamp", desc=False)\
            .limit(1).execute()
        if cmp_q.data:
            ref_price = float(cmp_q.data[0]["cmp"])
        else:
            # Fallback: midpoint of the day's strike range
            ref_price = available_strikes[len(available_strikes) // 2]
        active_strike = min(available_strikes, key=lambda s: abs(s - ref_price))
    else:
        active_strike = min(available_strikes, key=lambda s: abs(s - float(active_strike)))

    strike_rows = [r for r in exp_rows if float(r["strike"]) == active_strike]
    points = _build_points(strike_rows)

    # Oct 22 2026: ATM +-1 strike comparison ("3 straddle comparison" --
    # Manish's own framing) reuses exp_rows (the whole day's data for this
    # expiry, already fetched above) -- no extra Supabase round-trip, just
    # sliced 3 ways in memory. Step size is derived from the real gap
    # between available strikes rather than hardcoded per symbol (NIFTY=50,
    # BANKNIFTY=100, etc differ).
    compare_strikes = []
    if len(available_strikes) > 1:
        diffs = sorted(set(round(b - a, 2) for a, b in zip(available_strikes, available_strikes[1:]) if b > a))
        step = diffs[0] if diffs else 0
        wanted = sorted(set([active_strike - step, active_strike, active_strike + step])) if step else [active_strike]
        for target in wanted:
            snapped = min(available_strikes, key=lambda s: abs(s - target))
            if any(cs["strike"] == snapped for cs in compare_strikes):
                continue
            s_rows = [r for r in exp_rows if float(r["strike"]) == snapped]
            s_points = _build_points(s_rows)
            compare_strikes.append({
                "strike":            snapped,
                "role":              "atm" if snapped == active_strike else ("minus1" if snapped < active_strike else "plus1"),
                "points":            s_points,
                "day_open_combined": s_points[0]["combined"] if s_points else None,
                "latest_combined":   s_points[-1]["combined"] if s_points else None,
            })
        compare_strikes.sort(key=lambda cs: cs["strike"])
        for cs in compare_strikes:
            if cs["day_open_combined"] and cs["latest_combined"] is not None:
                cs["change_pct"] = round((cs["latest_combined"] - cs["day_open_combined"]) / cs["day_open_combined"] * 100, 1)
            else:
                cs["change_pct"] = None

    # Current CMP for reference (how far spot has moved from entry strike)
    cmp = None
    try:
        cmp_q2 = supabase.from_("cmp_prices")\
            .select("cmp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{date}T00:00:00+00:00")\
            .lt("timestamp",  f"{date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if cmp_q2.data:
            cmp = float(cmp_q2.data[0]["cmp"])
    except Exception:
        pass

    fut_vwap = _compute_fut_vwap(supabase, symbol, date)

    return {
        "symbol":             symbol,
        "date":               date,
        "expiry":             active_expiry,
        "available_expiries": available_expiries,
        "strike":             active_strike,
        "available_strikes":  available_strikes,
        "cmp":                cmp,
        "points":             points,
        "day_open_combined":  points[0]["combined"] if points else None,
        "latest_combined":    points[-1]["combined"] if points else None,
        "compare_strikes":    compare_strikes,
        "fut_vwap":           fut_vwap,
    }
