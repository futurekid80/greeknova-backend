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
from datetime import datetime, timezone, date as date_type, timedelta
import time as time_module
import math

from api.uoa import is_market_hours, is_post_market
from services.black_scholes import implied_vol, bs_gamma
from services.fno_universe import LOT_SIZES

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

RV_LOOKBACK_SESSIONS = 10   # close-to-close realized vol window
RV_MIN_SESSIONS = 6         # fewer closes than this -> too noisy, report None
RV_LOOKBACK_CALENDAR_DAYS = 25  # buffer for weekends/holidays to get
                                 # RV_LOOKBACK_SESSIONS trading days
TRADING_DAYS_PER_YEAR = 252
IV_RICH_RATIO = 1.6   # atm_iv / realized_vol at/above this -> "IV rich".
                       # NOTE: IV structurally trades above RV most of the
                       # time (the volatility risk premium -- option sellers
                       # price in a cushion for the unknown), so a plain
                       # >1.0 or >1.3 threshold flags almost every stock on
                       # an ordinary day, not just the outliers. 1.6x is
                       # calibrated to catch names where the premium is
                       # stretched well past the normal VRP baseline --
                       # likely event risk (earnings, corporate action)
                       # rather than a pure gamma-mechanical squeeze.
IV_CHEAP_RATIO = 0.9   # at/below this -> "IV cheap" -- IV at or below
                        # realized vol is the genuinely rare, notable state
                        # (normally IV sits above RV), and the mechanically
                        # clean setup: real gamma amplification without an
                        # inflated premium or IV-crush risk on resolution


def _realized_vol_map(supabase, symbols, today_date):
    """Annualized close-to-close realized volatility per symbol, from the
    spot_daily_bars table (already backfilled/kept current via Kite
    historical_data by the spot-volume-scanner job -- see api/spot_volume_
    scanner.py) rather than a fresh Kite call per symbol on every GEX
    refresh, which would be slow and rate-limit-risky at ~200 symbols
    every few minutes.

    Returns {symbol: rv_decimal}, e.g. 0.284 for 28.4% annualized RV.
    Symbols with fewer than RV_MIN_SESSIONS closes in the lookback window
    are omitted (their IV/RV ratio will simply be null downstream, never
    a guessed number)."""
    cutoff = (today_date - timedelta(days=RV_LOOKBACK_CALENDAR_DAYS)).isoformat()
    bars = _paginated_fetch(lambda lo, hi: supabase.from_("spot_daily_bars")
        .select("symbol,trade_date,close")
        .gte("trade_date", cutoff)
        .lt("trade_date", today_date.isoformat())
        .order("trade_date")
        .range(lo, hi), max_offset=50000)

    by_symbol: dict = {}
    wanted = set(symbols)
    for r in bars:
        sym = r.get("symbol")
        if sym not in wanted:
            continue
        close = r.get("close")
        if close is None or close <= 0:
            continue
        by_symbol.setdefault(sym, []).append((r["trade_date"], float(close)))

    rv_map: dict = {}
    for sym, pts in by_symbol.items():
        pts.sort(key=lambda p: p[0])
        closes = [c for _, c in pts[-(RV_LOOKBACK_SESSIONS + 1):]]
        if len(closes) < RV_MIN_SESSIONS + 1:
            continue
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0
        ]
        if len(log_returns) < RV_MIN_SESSIONS:
            continue
        mean_r = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        daily_sigma = math.sqrt(variance)
        rv_map[sym] = daily_sigma * math.sqrt(TRADING_DAYS_PER_YEAR)
    return rv_map


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
    """Serve the gamma-exposure snapshot. A background scheduler job
    (refresh_gamma_exposure_cache, wired up in main.py) keeps _gex_cache
    warm every few minutes, so this just serves whatever is cached — no
    request should ever pay for the full ~150-stock Black-Scholes scan
    inline. We only fall back to a synchronous compute in the narrow
    window right after a fresh deploy/restart, before the background
    job has run for the first time."""
    global _gex_cache, _gex_cache_time

    if _gex_cache:
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


def refresh_gamma_exposure_cache():
    """Background job: recompute the gamma-exposure snapshot and update
    the module-level cache. Runs on a schedule from main.py so requests
    to /gamma-squeeze never trigger the slow ~150-stock scan themselves."""
    global _gex_cache, _gex_cache_time
    try:
        result = _compute_gamma_exposure()
        _gex_cache = result
        _gex_cache_time = time_module.time()
        print(f"[gamma_exposure] background refresh OK — {len(result.get('watchlist', []))} stocks")
    except Exception as e:
        print(f"[gamma_exposure] background refresh failed: {e}")


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

    rv_map = _realized_vol_map(supabase, eligible_symbols, today_date)

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
        iv_by_strike: dict = {}  # strike -> {"CE": iv, "PE": iv}
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
            iv_by_strike.setdefault(strike, {})[opt] = iv

        if not per_strike:
            continue

        # ── ATM implied vol: average of CE/PE IV at the strike nearest
        # spot (whichever sides solved), vs. realized vol -> is the
        # market pricing in more move than the stock's actually been
        # making (IV rich -> likely event risk / expensive premium) or
        # about the same/less (IV fair-to-cheap -> the clean mechanical
        # squeeze setup)? ──────────────────────────────────────────────
        atm_strike = min(iv_by_strike.keys(), key=lambda k: abs(k - spot))
        atm_ivs = list(iv_by_strike[atm_strike].values())
        atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
        realized_vol = rv_map.get(sym)
        iv_rv_ratio = round(atm_iv / realized_vol, 2) if atm_iv and realized_vol else None
        if iv_rv_ratio is None:
            iv_regime = None
        elif iv_rv_ratio >= IV_RICH_RATIO:
            iv_regime = "RICH"
        elif iv_rv_ratio <= IV_CHEAP_RATIO:
            iv_regime = "CHEAP"
        else:
            iv_regime = "FAIR"

        strikes_sorted = sorted(per_strike.keys())
        net_per_strike = {k: (v["CE"] - v["PE"]) for k, v in per_strike.items()}

        call_wall = max(per_strike.items(), key=lambda kv: kv[1]["CE"]) if any(v["CE"] > 0 for v in per_strike.values()) else None
        put_wall = max(per_strike.items(), key=lambda kv: kv[1]["PE"]) if any(v["PE"] > 0 for v in per_strike.values()) else None

        net_gex = sum(net_per_strike.values())

        # Rupee-scaled notional GEX -- None when we don't have a live lot
        # size for this symbol yet (never guess; fall back to null on the
        # frontend, not a wrong number).
        lot_size = LOT_SIZES.get(sym)
        RUPEE_SCALE = 1e7  # express in Rs. Crores for readability
        # NOTE: Kite's `oi` field (and therefore our `net_gex = gamma * oi`)
        # is already total open interest in SHARES, not in number of lots --
        # confirmed by the fact the existing relative Net GEX numbers only
        # make sense at share-quantity scale. So the rupee formula must NOT
        # multiply by lot_size again (that double-counts it and inflates
        # every figure by another factor of lot_size). lot_size is kept
        # around only to gate on "do we have a live lot size for this
        # symbol" -- it does not enter the math.
        net_gex_rupees_cr = (
            round(net_gex * spot * spot * 0.01 / RUPEE_SCALE, 2)
            if lot_size else None
        )

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
        net_gex_near_spot_rupees_cr = (
            round(local_net_gex * spot * spot * 0.01 / RUPEE_SCALE, 2)
            if lot_size else None
        )

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

        # ── ATM +/-3 strike ladder: the full local neighborhood around spot,
        # not just the single biggest wall on each side. Deliberately
        # includes strikes already ITM (price has passed through them) --
        # that's exactly where "are writers still defending this level or
        # are they leaving" matters, and it's what a single call/put wall
        # number can't show. Each rung reuses the same day's-open OI
        # baseline and +/-45% BUILDING/STEADY/UNWINDING thresholds as the
        # existing single-strike OI-trend check above, just applied strike
        # by strike instead of only at squeeze_strike. ────────────────────
        atm_idx = strikes_sorted.index(atm_strike)
        strike_ladder = []
        for k in strikes_sorted[max(0, atm_idx - 3):atm_idx + 4]:
            rung = {
                "strike": k,
                "is_atm": k == atm_strike,
                "is_call_wall": call_wall_strike is not None and k == call_wall_strike,
                "is_put_wall": put_wall_strike is not None and k == put_wall_strike,
            }
            for opt, oi_key, trend_key, pct_key in (
                ("CE", "call_oi", "call_oi_trend_label", "call_oi_trend_pct"),
                ("PE", "put_oi", "put_oi_trend_label", "put_oi_trend_pct"),
            ):
                oi_now = latest_oi_lookup.get((sym, k, opt))
                oi_open_v = oi_open_by_key.get((sym, k, opt))
                rung[oi_key] = oi_now
                if oi_now and oi_open_v:
                    pct = round((oi_now - oi_open_v) / oi_open_v * 100, 1)
                    rung[pct_key] = pct
                    if pct <= -40:
                        rung[trend_key] = "UNWINDING"
                    elif pct >= 40:
                        rung[trend_key] = "BUILDING"
                    else:
                        rung[trend_key] = "STEADY"
                else:
                    rung[pct_key] = None
                    rung[trend_key] = None
            strike_ladder.append(rung)

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

        # ── "Clear Runway": is there room to run past the wall that's being
        # broken, or is another wall parked right on top of it? Finds the
        # next major gamma-weighted wall beyond the squeeze strike, in the
        # direction of the move, and whether that gap is wide enough
        # (>=2% of spot) to call it clear rather than a trap.
        next_wall_strike = None
        next_wall_gamma_oi = None
        runway_pct = None
        runway_clear = None
        if squeeze_option_type == "CE" and call_wall_strike is not None:
            beyond = [(k, v["CE"]) for k, v in per_strike.items() if v["CE"] > 0 and k > call_wall_strike]
            if beyond:
                next_wall_strike, _next_gex = max(beyond, key=lambda kv: kv[1])
                next_wall_gamma_oi = round(_next_gex, 2)
                runway_pct = round((next_wall_strike - call_wall_strike) / spot * 100, 2)
        elif squeeze_option_type == "PE" and put_wall_strike is not None:
            beyond = [(k, v["PE"]) for k, v in per_strike.items() if v["PE"] > 0 and k < put_wall_strike]
            if beyond:
                next_wall_strike, _next_gex = max(beyond, key=lambda kv: kv[1])
                next_wall_gamma_oi = round(_next_gex, 2)
                runway_pct = round((put_wall_strike - next_wall_strike) / spot * 100, 2)
        if runway_pct is not None:
            runway_clear = runway_pct >= 2.0

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
            "strike_ladder": strike_ladder,
            "flip_point": flip_point,
            "net_gex": round(net_gex, 2),
            "net_gex_near_spot": round(local_net_gex, 2),
            "lot_size": lot_size,
            "net_gex_rupees_cr": net_gex_rupees_cr,
            "net_gex_near_spot_rupees_cr": net_gex_near_spot_rupees_cr,
            "regime": regime,
            "atm_iv": round(atm_iv * 100, 1) if atm_iv else None,
            "realized_vol": round(realized_vol * 100, 1) if realized_vol else None,
            "iv_rv_ratio": iv_rv_ratio,
            "iv_regime": iv_regime,
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
            "next_wall_strike": next_wall_strike,
            "next_wall_gamma_oi": next_wall_gamma_oi,
            "runway_pct": runway_pct,
            "runway_clear": runway_clear,
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


def get_gex_by_strike(symbol: str = "NIFTY", date: str = None):
    """Per-strike GEX breakdown for one symbol's nearest expiry — powers the
    GEX-by-strike chart. Reuses the same IV-solve + Black-Scholes gamma
    approach as _compute_gamma_exposure, scoped to a single symbol so it's
    cheap to call directly (no multi-symbol pagination)."""
    supabase = get_supabase()
    symbol = symbol.upper()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_date = date_type.fromisoformat(today) if date else datetime.now(timezone.utc).date()

    new_row = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    if not new_row.data:
        return {"symbol": symbol, "strikes": [], "spot": None, "error": "no data"}

    ts_new = new_row.data[0]["timestamp"]

    chain_raw = supabase.from_("oi_snapshots")\
        .select("strike,option_type,expiry,oi,last_price")\
        .eq("symbol", symbol)\
        .eq("timestamp", ts_new)\
        .execute().data or []

    expiries = sorted(set(
        r["expiry"] for r in chain_raw
        if r.get("expiry") and r["expiry"] >= today_date.isoformat()
    ))
    if not expiries:
        return {"symbol": symbol, "strikes": [], "spot": None, "error": "no active expiry"}
    expiry = expiries[0]

    cmp_q = supabase.from_("cmp_prices")\
        .select("cmp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()
    spot = float(cmp_q.data[0]["cmp"]) if cmp_q.data else None
    if not spot:
        return {"symbol": symbol, "strikes": [], "spot": None, "error": "no spot price"}

    try:
        dte = (date_type.fromisoformat(expiry) - today_date).days
    except Exception:
        dte = 0
    T = max(dte, 0) / 365.0
    if T <= 0:
        T = 0.25 / 365.0

    per_strike: dict = {}
    for r in chain_raw:
        if r.get("expiry") != expiry or r.get("option_type") not in ("CE", "PE"):
            continue
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
        gex = gamma * oi
        per_strike.setdefault(strike, {"CE": 0.0, "PE": 0.0})
        per_strike[strike][opt] += gex

    if not per_strike:
        return {"symbol": symbol, "strikes": [], "spot": spot, "error": "no strikes solved"}

    strikes_sorted = sorted(per_strike.keys())
    net_per_strike = {k: (v["CE"] - v["PE"]) for k, v in per_strike.items()}

    call_wall = max(per_strike.items(), key=lambda kv: kv[1]["CE"]) if any(v["CE"] > 0 for v in per_strike.values()) else None
    put_wall = max(per_strike.items(), key=lambda kv: kv[1]["PE"]) if any(v["PE"] > 0 for v in per_strike.values()) else None

    LOCAL_BAND_PCT = 0.05
    local_net_gex = sum(v for k, v in net_per_strike.items() if abs(k - spot) / spot <= LOCAL_BAND_PCT)
    regime = "SHORT_GAMMA" if local_net_gex < 0 else "LONG_GAMMA"

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
        if abs(nearest - spot) / spot <= 0.06:
            flip_point = nearest

    strikes_out = [
        {
            "strike": k,
            "ce_gex": round(per_strike[k]["CE"], 2),
            "pe_gex": round(-per_strike[k]["PE"], 2),
            "net_gex": round(net_per_strike[k], 2),
        }
        for k in strikes_sorted
    ]

    return {
        "symbol": symbol,
        "expiry": expiry,
        "days_to_expiry": dte,
        "spot": spot,
        "regime": regime,
        "call_wall_strike": call_wall[0] if call_wall else None,
        "put_wall_strike": put_wall[0] if put_wall else None,
        "flip_point": flip_point,
        "strikes": strikes_out,
        "as_of": ts_new,
    }
