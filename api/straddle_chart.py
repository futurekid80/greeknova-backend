"""Intraday straddle premium chart — NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY only for now.

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


def get_straddle_chart(symbol: str = "NIFTY", strike: float = None, expiry: str = None, date: str = None):
    from utils.db import get_supabase
    supabase = get_supabase()

    symbol = symbol.upper()
    if symbol not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        return {"error": "Straddle Chart currently supports NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY only"}

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
    }
