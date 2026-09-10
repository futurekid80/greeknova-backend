"""Gamma Exposure (GEX) scanner — replaces the old single-strike OI-unwind
"Gamma Squeeze" heuristic with the actual dealer-gamma concept traders mean
by that name.

For every symbol (nearest active expiry), we back out implied volatility
from each strike's traded premium, compute that strike's Black-Scholes
gamma, and weight it by open interest to build a per-strike gamma exposure
profile across the whole option chain:

  - Call Wall  = the CE strike carrying the most gamma*OI  -> key resistance
  - Put Wall   = the PE strike carrying the most gamma*OI  -> key support
  - Net GEX    = signed sum of (call gamma*OI - put gamma*OI) across all
                 strikes -> is the market-maker book, in aggregate, long or
                 short gamma right now?
  - Flip point = the strike level where cumulative GEX (walking strikes low
                 to high) crosses from negative to positive -> the "zero
                 gamma" level separating a stabilizing (dealers long gamma,
                 hedging dampens moves) regime from an amplifying (dealers
                 short gamma, hedging accelerates moves) one.

NOTE ON UNITS: this has no lot-size table wired in (NSE lot sizes differ per
symbol and are revised quarterly — not tracked anywhere in this codebase
yet), so Net GEX / wall gamma values are in raw "gamma x OI" units, not
rupees. That's still exactly right for everything this page does — which
wall is bigger, where the flip point sits, whether a symbol is short or
long gamma — because lot size is just a constant per symbol and doesn't
change any of those comparisons *within* a symbol. It would only matter for
ranking absolute squeeze size *across* symbols in rupee terms, which this
page doesn't attempt yet.
"""
from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type
import time as time_module
import math

from api.uoa import is_market_hours, is_post_market
from services.black_scholes import implied_vol, bs_gamma

_gex_cache: dict = {}
_gex_cache_time: float = 0
GEX_CACHE_TTL = 240  # 4 minutes, same cadence as the old scanner

RISK_FREE_RATE = 0.07  # rough India short-rate assumption; gamma is not
                        # very sensitive to this, so a fixed estimate is fine
MIN_OPEN_OI = 5000      # ignore illiquid strikes — same floor as before
MAX_STRIKE_MONEYNESS = 0.35  # ignore strikes more than 35% away from spot —
                              # deep OTM/ITM legs have near-zero gamma and
                              # unreliable IV solves (thin/stale quotes)
PROXIMITY_PCT = 1.5     # "near a wall / the flip point" = within this % of it


def _eligible_expiry_map(new_data_raw, today_date):
    nearest_expiry_map: dict = {}
    for r in new_data_raw:
        sym = r["symbol"]
        exp = r.get("expiry")
        opt = r.get("option_type", "")
        if opt not in ("CE", "PE"):
            continue
        if not exp or exp < today_date.isoformat():
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp
    return nearest_expiry_map


def get_gamma_exposure(date: str = None):
    global _gex_cache, _gex_cache_time

    cache_ttl = GEX_CACHE_TTL if is_market_hours() else 600
    if _gex_cache and (time_module.time() - _gex_cache_time) < cache_ttl:
        return _gex_cache

    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    post_market = is_post_market()

    # ── Latest capture timestamp today (direct MAX query — see gamma_squeeze
    # .py for why .range() pagination on a tied-timestamp column is unsafe) ──
    new_row = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    if not new_row.data:
        return {"symbols": [], "signals": [], "total": 0, "date": today, "as_of": None, "is_post_market": post_market}

    ts_new = new_row.data[0]["timestamp"]
    ts_new_dt = datetime.fromisoformat(ts_new.replace('+00:00', '')).replace(tzinfo=timezone.utc)
    window_start = ts_new_dt.isoformat()
    from datetime import timedelta
    window_start = (ts_new_dt - timedelta(minutes=7)).isoformat()

    rows = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("*")\
            .gte("timestamp", window_start)\
            .lte("timestamp", ts_new)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    latest_by_key: dict = {}
    for r in rows:
        key = (r.get("symbol"), r.get("option_type"), r.get("strike"), r.get("expiry"))
        if key not in latest_by_key or r["timestamp"] > latest_by_key[key]["timestamp"]:
            latest_by_key[key] = r
    all_rows = list(latest_by_key.values())

    today_date = date_type.fromisoformat(today) if date else datetime.now(timezone.utc).date()
    nearest_expiry_map = _eligible_expiry_map(all_rows, today_date)
    eligible_symbols = set(nearest_expiry_map.keys())

    chain_rows = [
        r for r in all_rows
        if r["symbol"] in eligible_symbols
        and nearest_expiry_map.get(r["symbol"]) == r.get("expiry")
        and r.get("option_type") in ("CE", "PE")
        and (r.get("oi") or 0) >= MIN_OPEN_OI
    ]

    # ── Spot price per symbol ───────────────────────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        cmp_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break
    cmp_map, seen_cmp = {}, set()
    for c in cmp_raw:
        if c["symbol"] not in seen_cmp:
            cmp_map[c["symbol"]] = c["cmp"]
            seen_cmp.add(c["symbol"])

    # ── Group option-chain rows by symbol ───────────────────────────────────
    by_symbol: dict = {}
    for r in chain_rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    symbols_out = []
    signals = []

    for sym, chain in by_symbol.items():
        spot = cmp_map.get(sym)
        if not spot or spot <= 0:
            continue
        expiry = nearest_expiry_map.get(sym)
        try:
            dte = (date_type.fromisoformat(expiry) - today_date).days
        except Exception:
            continue
        T = max(dte, 0) / 365.0
        # Same-day expiry: give it a sliver of time value rather than 0 so
        # gamma doesn't blow up to infinity at the strike.
        if T <= 0:
            T = 0.25 / 365.0

        per_strike: dict = {}  # strike -> {"CE": gex, "PE": gex}
        for r in chain:
            strike = float(r["strike"])
            if abs(strike - spot) / spot > MAX_STRIKE_MONEYNESS:
                continue
            opt = r["option_type"]
            oi = r.get("oi") or 0
            premium = r.get("last_price") or 0
            if oi <= 0 or premium <= 0:
                continue
            iv = implied_vol(premium, spot, strike, T, RISK_FREE_RATE, opt)
            if iv is None:
                continue
            gamma = bs_gamma(spot, strike, T, RISK_FREE_RATE, iv)
            gex = gamma * oi  # unscaled (no lot size) — see module docstring
            per_strike.setdefault(strike, {"CE": 0.0, "PE": 0.0})
            per_strike[strike][opt] += gex

        if not per_strike:
            continue

        strikes_sorted = sorted(per_strike.keys())
        net_per_strike = {k: (v["CE"] - v["PE"]) for k, v in per_strike.items()}

        call_wall = max(per_strike.items(), key=lambda kv: kv[1]["CE"]) if any(v["CE"] > 0 for v in per_strike.values()) else None
        put_wall = max(per_strike.items(), key=lambda kv: kv[1]["PE"]) if any(v["PE"] > 0 for v in per_strike.values()) else None

        net_gex = sum(net_per_strike.values())

        # ── Flip point: walk strikes low -> high, find where the cumulative
        # net GEX changes sign. That crossing strike is the "zero gamma"
        # level. If the whole chain is one-signed, there's no flip in range.
        flip_point = None
        cum = 0.0
        prev_strike, prev_cum = None, None
        for k in strikes_sorted:
            cum += net_per_strike[k]
            if prev_cum is not None and ((prev_cum < 0 <= cum) or (prev_cum > 0 >= cum)):
                # linear interpolation between the two straddling strikes
                span = k - prev_strike
                if span > 0 and (cum - prev_cum) != 0:
                    frac = (0 - prev_cum) / (cum - prev_cum)
                    flip_point = round(prev_strike + frac * span, 2)
                else:
                    flip_point = k
                break
            prev_strike, prev_cum = k, cum

        regime = None
        if flip_point is not None:
            regime = "SHORT_GAMMA" if spot < flip_point else "LONG_GAMMA"
        else:
            regime = "SHORT_GAMMA" if net_gex < 0 else "LONG_GAMMA"

        call_wall_strike = call_wall[0] if call_wall else None
        call_wall_gamma_oi = round(call_wall[1]["CE"], 2) if call_wall else None
        put_wall_strike = put_wall[0] if put_wall else None
        put_wall_gamma_oi = round(put_wall[1]["PE"], 2) if put_wall else None

        pct_to_call_wall = round((call_wall_strike - spot) / spot * 100, 2) if call_wall_strike else None
        pct_to_put_wall = round((spot - put_wall_strike) / spot * 100, 2) if put_wall_strike else None
        pct_to_flip = round((spot - flip_point) / flip_point * 100, 2) if flip_point else None

        squeeze = False
        bias = None
        label = ""
        desc = ""

        if regime == "SHORT_GAMMA":
            near_call_wall = pct_to_call_wall is not None and -PROXIMITY_PCT <= pct_to_call_wall <= PROXIMITY_PCT
            near_put_wall = pct_to_put_wall is not None and -PROXIMITY_PCT <= pct_to_put_wall <= PROXIMITY_PCT
            if near_call_wall:
                squeeze = True
                bias = "BULLISH"
                label = f"{sym}: pressing into the call wall ({call_wall_strike:g}) while dealers are net short gamma"
                desc = "Dealers short gamma here means their hedging BUYS into strength as price pushes up toward this strike — a break above can accelerate rather than stall."
            elif near_put_wall:
                squeeze = True
                bias = "BEARISH"
                label = f"{sym}: pressing into the put wall ({put_wall_strike:g}) while dealers are net short gamma"
                desc = "Dealers short gamma here means their hedging SELLS into weakness as price pushes down toward this strike — a break below can accelerate rather than find support."

        row_out = {
            "symbol": sym,
            "cmp": spot,
            "expiry": expiry,
            "days_to_expiry": dte,
            "call_wall_strike": call_wall_strike,
            "call_wall_gamma_oi": call_wall_gamma_oi,
            "put_wall_strike": put_wall_strike,
            "put_wall_gamma_oi": put_wall_gamma_oi,
            "flip_point": flip_point,
            "net_gex": round(net_gex, 2),
            "regime": regime,
            "pct_to_call_wall": pct_to_call_wall,
            "pct_to_put_wall": pct_to_put_wall,
            "pct_to_flip": pct_to_flip,
            "squeeze": squeeze,
            "bias": bias,
            "label": label,
            "desc": desc,
        }
        symbols_out.append(row_out)
        if squeeze:
            signals.append(row_out)

    # Watchlist ranked so the most "live" setups surface first: squeeze
    # candidates first, then by closeness to whichever wall is nearer.
    def _sort_key(r):
        closest = min(
            [abs(v) for v in (r["pct_to_call_wall"], r["pct_to_put_wall"]) if v is not None] or [999]
        )
        return (0 if r["squeeze"] else 1, closest)
    symbols_out.sort(key=_sort_key)

    as_of_dt = ts_new_dt + timedelta(hours=5, minutes=30)  # display in IST
    result = {
        "date": today,
        "as_of": as_of_dt.strftime("%H:%M"),
        "total": len(signals),
        "signals": signals,
        "watchlist": symbols_out,
        "is_post_market": post_market,
    }

    _gex_cache = result
    _gex_cache_time = time_module.time()
    return result
