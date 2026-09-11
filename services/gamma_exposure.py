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

import httpx


def _paginated_fetch(build_query, page_size=1000, max_offset=200000, max_retries=3):
    """Runs `build_query(offset, offset+page_size-1)` in a loop, paging through
    a Supabase query via .range(), until a short page signals the end.

    Wrapped with a small retry-with-backoff on each page: Supabase's
    connection pool occasionally drops mid-loop with an
    httpx.RemoteProtocolError / ConnectionTerminated when a lot of .range()
    calls fire back-to-back on the same client — retrying that one page
    (rather than failing the whole endpoint) clears it almost every time.
    """
    rows = []
    for offset in range(0, max_offset, page_size):
        batch = None
        last_err = None
        for attempt in range(max_retries):
            try:
                batch = build_query(offset, offset + page_size - 1).execute()
                break
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_err = e
                time_module.sleep(0.4 * (attempt + 1))
        if batch is None:
            raise last_err
        if not batch.data:
            break
        rows.extend(batch.data)
        if len(batch.data) < page_size:
            break
    return rows


_gex_cache: dict = {}
_gex_cache_time: float = 0
GEX_CACHE_TTL = 240  # 4 minutes, same cadence as the old scanner

RISK_FREE_RATE = 0.07  # rough India short-rate assumption; gamma is not
                        # very sensitive to this, so a fixed estimate is fine
MIN_OPEN_OI = 5000      # ignore illiquid strikes — same floor as before
MAX_STRIKE_MONEYNESS = 0.35  # ignore strikes more than 35% away from spot —
                              # deep OTM/ITM legs have near-zero gamma and
                              # unreliable IV solves (thin/stale quotes)
PROXIMITY_PCT = 1.5     # "on the verge" = within this % of a wall, not yet crossed
BREAKOUT_RANGE_PCT = 8.0  # "actively squeezing" = already past the wall, up to this
                           # far beyond it — further than that and it's just an old
                           # move, not a live squeeze event anymore
ALERT_LOOKBACK_MIN = 45   # how far back to pull live Alerts-feed events for
                           # confirming a squeeze at its exact wall strike


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

    try:
        return _compute_gamma_exposure(date)
    except Exception as e:
        # Supabase connection blips (RemoteProtocolError etc.) happen
        # intermittently under the pagination load this endpoint does.
        # _paginated_fetch already retries each page; if it still fails,
        # prefer serving the last good (even if stale) result over a 500 —
        # a few-minutes-old GEX snapshot is far more useful to the frontend
        # than "Failed to fetch".
        print(f"[gamma_exposure] compute failed: {e}")
        if _gex_cache:
            return _gex_cache
        raise


def _compute_gamma_exposure(date: str = None):
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

    rows = _paginated_fetch(lambda lo, hi: supabase.from_("oi_snapshots")
        .select("*")
        .gte("timestamp", window_start)
        .lte("timestamp", ts_new)
        .range(lo, hi))

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
    cmp_raw = _paginated_fetch(lambda lo, hi: supabase.from_("cmp_prices")
        .select("*")
        .gte("timestamp", f"{today}T00:00:00+00:00")
        .order("timestamp", desc=True)
        .range(lo, hi), max_offset=10000)
    cmp_map, seen_cmp = {}, set()
    for c in cmp_raw:
        if c["symbol"] not in seen_cmp:
            cmp_map[c["symbol"]] = c["cmp"]
            seen_cmp.add(c["symbol"])

    # ── Recent live Alerts-feed events, for confirming a squeeze with actual
    # order flow at its exact wall strike (not just the modeled gamma) ──────
    alert_cutoff = (ts_new_dt - timedelta(minutes=ALERT_LOOKBACK_MIN)).isoformat()
    alert_raw = _paginated_fetch(lambda lo, hi: supabase.from_("alert_log")
        .select("symbol,signal,strike,option_type,oi_pct,vol_pct,ltp,created_at")
        .gte("created_at", alert_cutoff)
        .order("created_at", desc=True)
        .range(lo, hi), max_offset=20000)
    alerts_by_strike: dict = {}
    for a in alert_raw:
        try:
            key = (a["symbol"], float(a["strike"]), a.get("option_type"))
        except (TypeError, ValueError):
            continue
        alerts_by_strike.setdefault(key, []).append(a)

    # ── Day's opening OI at each strike (for the OI-trend check below) —
    # only the nearest/eligible expiry per symbol, same filter as chain_rows,
    # so a symbol's two expiries never get mixed into one trend number ─────
    oi_open_raw = _paginated_fetch(lambda lo, hi: supabase.from_("oi_snapshots")
        .select("symbol,strike,option_type,expiry,timestamp,oi")
        .gte("timestamp", f"{today}T00:00:00+00:00")
        .lte("timestamp", ts_new)
        .order("timestamp")
        .range(lo, hi))
    oi_open_by_key: dict = {}
    for r in oi_open_raw:
        if nearest_expiry_map.get(r.get("symbol")) != r.get("expiry"):
            continue
        key = (r.get("symbol"), float(r["strike"]), r.get("option_type"))
        if key not in oi_open_by_key:   # ascending order -> first hit = day's open
            oi_open_by_key[key] = r.get("oi") or 0

    # Latest OI (raw, not gamma-weighted) per strike, same nearest-expiry
    # chain_rows already used for the gamma calc above.
    latest_oi_lookup: dict = {}
    for r in chain_rows:
        key = (r["symbol"], float(r["strike"]), r.get("option_type"))
        latest_oi_lookup[key] = r.get("oi") or 0

    # ── Group option-chain rows by symbol ──────────────────────────────────
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

        # ── Regime (short/long gamma): driven by the LOCAL gamma balance
        # around spot (+-5% moneyness), not the full-chain cumulative sum.
        # Cumulative-from-the-lowest-strike is what public "gamma flip"
        # charts usually plot, but on real data it can cross sign once far
        # out in a thin, near-worthless tail strike and then never cross
        # again — comparing spot to THAT crossing mislabels the regime even
        # though the near-the-money gamma (the part that actually drives
        # dealer hedging flow) is unambiguous. The near-spot sign is the
        # reliable read of whether dealers are net short or long gamma
        # right now.
        LOCAL_BAND_PCT = 0.05
        local_net_gex = sum(
            v for k, v in net_per_strike.items() if abs(k - spot) / spot <= LOCAL_BAND_PCT
        )
        regime = "SHORT_GAMMA" if local_net_gex < 0 else "LONG_GAMMA"

        # ── Flip point: walk strikes low -> high, collect every sign
        # crossing of the cumulative net GEX, then report whichever
        # crossing sits closest to spot (if any is within a plausible
        # range) — a real level, not just an artifact of chain noise.
        flip_point = None
        crossings = []
        cum = 0.0
        prev_strike, prev_cum = None, None
        for k in strikes_sorted:
            cum += net_per_strike[k]
            if prev_cum is not None and ((prev_cum < 0 <= cum) or (prev_cum > 0 >= cum)):
                span = k - prev_strike
                if span > 0 and (cum - prev_cum) != 0:
                    frac = (0 - prev_cum) / (cum - prev_cum)
                    crossings.append(round(prev_strike + frac * span, 2))
                else:
                    crossings.append(k)
            prev_strike, prev_cum = k, cum
        if crossings:
            nearest = min(crossings, key=lambda f: abs(f - spot))
            if abs(nearest - spot) / spot <= 0.06:  # only report a flip that's plausibly in play
                flip_point = nearest

        call_wall_strike = call_wall[0] if call_wall else None
        call_wall_gamma_oi = round(call_wall[1]["CE"], 2) if call_wall else None
        put_wall_strike = put_wall[0] if put_wall else None
        put_wall_gamma_oi = round(put_wall[1]["PE"], 2) if put_wall else None

        pct_to_call_wall = round((call_wall_strike - spot) / spot * 100, 2) if call_wall_strike else None
        pct_to_put_wall = round((spot - put_wall_strike) / spot * 100, 2) if put_wall_strike else None
        pct_to_flip = round((spot - flip_point) / flip_point * 100, 2) if flip_point else None

        squeeze = False
        stage = None       # "ACTIVE_SQUEEZE" | "ON_THE_VERGE"
        bias = None
        label = ""
        desc = ""
        squeeze_strike = None
        squeeze_option_type = None
        confirmations = []

        if regime == "SHORT_GAMMA":
            # pct_to_call_wall > 0  -> wall still above spot (approaching)
            # pct_to_call_wall < 0  -> spot already pushed past the wall (live squeeze)
            near_call_wall = pct_to_call_wall is not None and -BREAKOUT_RANGE_PCT <= pct_to_call_wall <= PROXIMITY_PCT
            near_put_wall = pct_to_put_wall is not None and -BREAKOUT_RANGE_PCT <= pct_to_put_wall <= PROXIMITY_PCT
            # If both walls are somehow in range (tight chain, walls close together),
            # prefer whichever is actually being broken right now over one merely approached.
            call_active = near_call_wall and pct_to_call_wall < 0
            put_active = near_put_wall and pct_to_put_wall < 0
            if call_active or (near_call_wall and not put_active):
                squeeze = True
                bias = "BULLISH"
                squeeze_strike, squeeze_option_type = call_wall_strike, "CE"
                stage = "ACTIVE_SQUEEZE" if call_active else "ON_THE_VERGE"
                if stage == "ACTIVE_SQUEEZE":
                    label = f"{sym}: ACTIVE squeeze — price already through the call wall ({call_wall_strike:g}) while dealers are net short gamma"
                    desc = "Dealers short gamma here means their hedging BUYS into strength as price keeps pushing up through this strike — the move can keep accelerating rather than stall."
                else:
                    label = f"{sym}: on the verge — pressing into the call wall ({call_wall_strike:g}) while dealers are net short gamma"
                    desc = "Dealers short gamma here means their hedging BUYS into strength as price approaches this strike — a break above can accelerate rather than stall."
            elif near_put_wall:
                squeeze = True
                bias = "BEARISH"
                squeeze_strike, squeeze_option_type = put_wall_strike, "PE"
                stage = "ACTIVE_SQUEEZE" if put_active else "ON_THE_VERGE"
                if stage == "ACTIVE_SQUEEZE":
                    label = f"{sym}: ACTIVE squeeze — price already through the put wall ({put_wall_strike:g}) while dealers are net short gamma"
                    desc = "Dealers short gamma here means their hedging SELLS into weakness as price keeps pushing down through this strike — the move can keep accelerating rather than find support."
                else:
                    label = f"{sym}: on the verge — pressing into the put wall ({put_wall_strike:g}) while dealers are net short gamma"
                    desc = "Dealers short gamma here means their hedging SELLS into weakness as price approaches this strike — a break below can accelerate rather than find support."

        # ── OI trend at the exact squeeze strike, single (nearest) expiry only:
        # is the wall being unwound (confirms the break, dealers/writers
        # capitulating) or still being built (fresh writing re-defending the
        # level -> watch for a trap/rebound rather than continuation)? ─────
        oi_open = None
        oi_current = None
        oi_trend_pct = None
        oi_trend_label = None
        if squeeze_strike is not None:
            oi_open = oi_open_by_key.get((sym, squeeze_strike, squeeze_option_type))
            oi_current = latest_oi_lookup.get((sym, squeeze_strike, squeeze_option_type))
            if oi_open and oi_current:
                oi_trend_pct = round((oi_current - oi_open) / oi_open * 100, 1)
                if oi_trend_pct <= -45:
                    oi_trend_label = "UNWINDING"   # OI draining out -> wall dissolving, break looks real
                elif oi_trend_pct >= 45:
                    oi_trend_label = "BUILDING"    # OI still being added -> wall being defended, rebound risk
                else:
                    oi_trend_label = "STEADY"

        if squeeze_strike is not None:
            matches = alerts_by_strike.get((sym, squeeze_strike, squeeze_option_type), [])
            confirmations = [
                {
                    "signal": a.get("signal"),
                    "oi_pct": a.get("oi_pct"),
                    "vol_pct": a.get("vol_pct"),
                    "ltp": a.get("ltp"),
                    "created_at": a.get("created_at"),
                }
                for a in matches[:3]
            ]

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
            "net_gex_near_spot": round(local_net_gex, 2),
            "regime": regime,
            "pct_to_call_wall": pct_to_call_wall,
            "pct_to_put_wall": pct_to_put_wall,
            "pct_to_flip": pct_to_flip,
            "squeeze": squeeze,
            "stage": stage,
            "squeeze_strike": squeeze_strike,
            "squeeze_option_type": squeeze_option_type,
            "confirmed_by_alerts": len(confirmations) > 0,
            "confirmations": confirmations,
            "oi_open": oi_open,
            "oi_current": oi_current,
            "oi_trend_pct": oi_trend_pct,
            "oi_trend_label": oi_trend_label,
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
        stage_rank = 0 if r["stage"] == "ACTIVE_SQUEEZE" else 1 if r["stage"] == "ON_THE_VERGE" else 2
        return (stage_rank, closest)
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

    # ── Persist every squeeze-relevant row to gex_signal_log so history
    # doesn't just vanish with the next 4-min cache refresh — needed for
    # after-the-fact case studies ("what did the tool actually show at
    # the time?") rather than relying on alert_log alone. Best-effort:
    # never let a logging failure break the live page. ────────────────
    try:
        log_rows = [
            {
                "as_of_data_ts": ts_new,
                "symbol": r["symbol"],
                "cmp": r["cmp"],
                "expiry": r["expiry"],
                "days_to_expiry": r["days_to_expiry"],
                "call_wall_strike": r["call_wall_strike"],
                "put_wall_strike": r["put_wall_strike"],
                "flip_point": r["flip_point"],
                "net_gex": r["net_gex"],
                "net_gex_near_spot": r["net_gex_near_spot"],
                "regime": r["regime"],
                "pct_to_call_wall": r["pct_to_call_wall"],
                "pct_to_put_wall": r["pct_to_put_wall"],
                "stage": r["stage"],
                "squeeze_strike": r["squeeze_strike"],
                "squeeze_option_type": r["squeeze_option_type"],
                "bias": r["bias"],
                "confirmed_by_alerts": r["confirmed_by_alerts"],
                "oi_open": r["oi_open"],
                "oi_current": r["oi_current"],
                "oi_trend_pct": r["oi_trend_pct"],
                "oi_trend_label": r["oi_trend_label"],
            }
            for r in symbols_out
            if r["stage"] is not None
        ]
        if log_rows:
            supabase.from_("gex_signal_log").insert(log_rows).execute()
    except Exception as e:
        print(f"[gex_signal_log] failed to persist snapshot: {e}")

    _gex_cache = result
    _gex_cache_time = time_module.time()
    return result
