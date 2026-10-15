import math
from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type
from utils.market_calendar import today_ist, trading_days_between
from services.black_scholes import (
    implied_vol76, bs76_gamma, bs76_theta_per_day, bs76_vega_per_point, _norm_cdf,
)

# ── Black-76 helpers ────────────────────────────────────────────────────────
# Oct 2026: this used to be a third, independent copy of plain Black-Scholes
# (spot-based, calendar-days/365) -- now delegates to the same Black-76
# engine (futures-based, 252-trading-days) as gamma_exposure.py, so IV and
# Greeks shown here match what the gamma-squeeze/vega pages compute instead
# of quietly disagreeing with them by a few percent.

def calculate_iv(market_price, F, K, T, r, is_call):
    if market_price < 0.1 or T <= 0 or F <= 0:
        return None
    iv = implied_vol76(market_price, F, K, T, r, "CE" if is_call else "PE")
    return round(iv * 100, 2) if iv else None

def calculate_greeks(F, K, T, r, sigma, is_call):
    if T <= 0 or sigma <= 0 or F <= 0:
        return {}
    opt = "CE" if is_call else "PE"
    try:
        d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
        delta_undiscounted = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
        delta = math.exp(-r * T) * delta_undiscounted
        gamma = bs76_gamma(F, K, T, r, sigma)
        theta = bs76_theta_per_day(F, K, T, r, sigma, opt)  # already negative (decay) for a long option
        vega = bs76_vega_per_point(F, K, T, r, sigma)  # already scaled per 1 IV point (1%)
        return {
            "delta": round(delta, 3),
            "gamma": round(gamma, 5),
            "theta": round(theta, 2),
            "vega":  round(vega, 2),
        }
    except Exception:
        return {}

# ── Main function ──────────────────────────────────────────────────────────────

INDEX_MAP = {
    "NIFTY":    "NSE:NIFTY 50",
    "BANKNIFTY":"NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
}

def get_option_chain(symbol: str = "NIFTY", expiry: str = None):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Spot price ─────────────────────────────────────────────────────────────
    spot = None
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        if symbol in INDEX_MAP:
            q = kite.quote([INDEX_MAP[symbol]])
            spot = q[INDEX_MAP[symbol]]["last_price"]
    except Exception as e:
        print(f"Spot fetch failed: {e}")

    # ── Latest timestamp ───────────────────────────────────────────────────────
    ts_q = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()

    if not ts_q.data:
        return {"symbol": symbol, "chain": [], "spot": spot, "expiry": expiry}

    latest_ts = ts_q.data[0]["timestamp"]

    # ── Snapshot data ──────────────────────────────────────────────────────────
    # Fetch ALL expiries for this symbol/timestamp first (not just the requested
    # one) so `available_expiries` always reflects the full list -- previously
    # this query filtered by `expiry` up front, so once the frontend echoed back
    # a specific expiry on its second call, the response's own `expiries` list
    # collapsed to just that one, making the expiry tabs disappear after refresh.
    all_rows = supabase.from_("oi_snapshots")\
        .select("strike, option_type, oi, volume, last_price, expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", latest_ts)\
        .order("strike", desc=False).execute().data

    if not all_rows:
        return {"symbol": symbol, "chain": [], "spot": spot, "expiry": expiry}

    # ── Expiry & T ─────────────────────────────────────────────────────────────
    available_expiries = sorted(set(r["expiry"] for r in all_rows))
    active_expiry = expiry or available_expiries[0]
    rows = [r for r in all_rows if r["expiry"] == active_expiry]

    exp_date = datetime.strptime(active_expiry, "%Y-%m-%d").date()
    today_date = today_ist()  # Oct 2026 fix: was date_type.today() (UTC) -- wrong
                               # between 12:00-5:30 AM IST, same bug fixed
                               # elsewhere per market_calendar.py's own notes.
    days_left = (exp_date - today_date).days
    trading_days_left = trading_days_between(today_date, exp_date)
    T = max(trading_days_left, 0.5) / 252   # min 0.5 trading-day to avoid degenerate Greeks
    r_f = 0.065  # ~6.5% risk-free rate -- used only as a discount rate under
                 # Black-76, not as a drift assumption (see option_chain Greeks above)

    # ── Estimate spot if Kite unavailable ─────────────────────────────────────
    if not spot:
        strikes_dict = {}
        for row in rows:
            s = row["strike"]
            if s not in strikes_dict:
                strikes_dict[s] = {}
            strikes_dict[s][row["option_type"]] = row["last_price"]
        best, best_diff = None, float("inf")
        for s, v in strikes_dict.items():
            if "CE" in v and "PE" in v and v["CE"] > 0 and v["PE"] > 0:
                diff = abs(v["CE"] - v["PE"])
                if diff < best_diff:
                    best_diff = diff
                    best = s
        spot = best or rows[len(rows)//2]["strike"]

    # ── Build chain ────────────────────────────────────────────────────────────
    strikes = sorted(set(r["strike"] for r in rows))
    ce_map = {r["strike"]: r for r in rows if r["option_type"] == "CE"}
    pe_map = {r["strike"]: r for r in rows if r["option_type"] == "PE"}
    atm = min(strikes, key=lambda s: abs(s - spot))

    # Futures price for this expiry (Black-76 underlying) -- fall back to
    # spot for whichever symbol/expiry doesn't have a liquid futures print.
    fut_row = next((r for r in rows if r["option_type"] == "FUT" and (r.get("last_price") or 0) > 0), None)
    F = fut_row["last_price"] if fut_row else spot

    chain = []
    for strike in strikes:
        ce = ce_map.get(strike, {})
        pe = pe_map.get(strike, {})
        ce_ltp = ce.get("last_price", 0) or 0
        pe_ltp = pe.get("last_price", 0) or 0

        ce_iv  = calculate_iv(ce_ltp, F, strike, T, r_f, True)
        pe_iv  = calculate_iv(pe_ltp, F, strike, T, r_f, False)
        ce_sig = (ce_iv / 100) if ce_iv else 0.25
        pe_sig = (pe_iv / 100) if pe_iv else 0.25

        chain.append({
            "strike":   strike,
            "is_atm":   strike == atm,
            "ce": {
                "ltp":    ce_ltp,
                "iv":     ce_iv,
                "oi":     ce.get("oi", 0),
                "volume": ce.get("volume", 0),
                **calculate_greeks(F, strike, T, r_f, ce_sig, True),
            },
            "pe": {
                "ltp":    pe_ltp,
                "iv":     pe_iv,
                "oi":     pe.get("oi", 0),
                "volume": pe.get("volume", 0),
                **calculate_greeks(F, strike, T, r_f, pe_sig, False),
            },
        })

    return {
        "symbol":    symbol,
        "spot":      spot,
        "expiry":    active_expiry,
        "days_left": days_left,
        "expiries":  available_expiries,
        "timestamp": latest_ts,
        "chain":     chain,
    }


# ── IV / Volume spike log (intraday, for BTST option-writing planning) ──────
# Oct 15 2026: Manish wants to know not just "is this strike spiking right
# now" (the live chain highlighting) but "did any strike spike at ANY point
# today", even if it's since faded back to normal by the time he looks --
# that's the point of the question: a strike that ran hot at 11:30 AM is a
# candidate for writing into the close and carrying overnight (BTST), and
# that signal is lost the moment the next 5-min oi_snapshots capture rolls
# in unless something keeps a record of it. oi_snapshots already holds one
# row per strike/side every 5 minutes during market hours (main.py's
# full_capture job) -- this replays today's snapshots chronologically and
# flags the same kind of spike the live chain highlights, using the same
# "unusual vs its own neighboring strikes right now" definition (not vs its
# own earlier value -- a strike's IV normally drifts with spot/time without
# that being a signal; what matters for writers is when a strike decouples
# from the smile around it), then collapses consecutive flagged snapshots
# into one episode per strike/side/metric instead of one row per 5-min tick.
_SPIKE_CACHE: dict = {}
_SPIKE_CACHE_TTL = 120  # seconds

IV_SPIKE_WINDOW = 3          # strikes each side used as the "neighborhood"
IV_SPIKE_THRESHOLD = 1.15    # 15% above local neighbor average
VOL_SPIKE_WINDOW = 3
VOL_SPIKE_THRESHOLD = 2.0    # 100% above local neighbor average -- volume is naturally spikier than IV
MIN_OI_FOR_IV = 500
MIN_VOL_FOR_IV = 50          # was 1 -- see MIN_VOL_FOR_SPIKE note below

# Oct 15 2026 (second pass): first live run on NIFTY flagged 169 "spikes" in
# one day, almost all deep-OTM far wings reading as "spiking" continuously
# for hours. A ratio-only check breaks down near zero -- a strike with
# neighbors averaging 10 contracts of volume reads as a "250% spike" the
# moment it prints 25, which is noise, not a writer signal. Require the
# current reading to also clear an absolute floor, not just a relative one,
# so a spike is only ever flagged where there's enough real activity for it
# to mean something.
MIN_VOL_FOR_SPIKE = 5000      # contracts -- below this, ratio alone can't flag a spike
MIN_VOL_BASELINE_FOR_SPIKE = 500  # neighbor average must itself be non-trivial


def _local_avg(values_by_strike: dict, strikes_sorted: list, idx: int, window: int):
    vals = []
    for off in range(-window, window + 1):
        if off == 0:
            continue
        j = idx + off
        if 0 <= j < len(strikes_sorted):
            v = values_by_strike.get(strikes_sorted[j])
            if v is not None:
                vals.append(v)
    if len(vals) < 2:
        return None
    return sum(vals) / len(vals)


def get_option_chain_spikes(symbol: str = "NIFTY", expiry: str = None):
    import time as _time
    cache_key = f"{symbol}:{expiry or ''}"
    cached = _SPIKE_CACHE.get(cache_key)
    if cached and _time.time() - cached[1] < _SPIKE_CACHE_TTL:
        return cached[0]

    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    all_rows = supabase.from_("oi_snapshots")\
        .select("timestamp, strike, option_type, oi, volume, last_price, expiry")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=False).execute().data

    if not all_rows:
        result = {"symbol": symbol, "expiry": expiry, "spikes": [], "snapshots_scanned": 0}
        _SPIKE_CACHE[cache_key] = (result, _time.time())
        return result

    available_expiries = sorted(set(r["expiry"] for r in all_rows if r["option_type"] != "FUT"))
    active_expiry = expiry or (available_expiries[0] if available_expiries else None)
    rows = [r for r in all_rows if r["expiry"] == active_expiry]

    if not rows:
        result = {"symbol": symbol, "expiry": active_expiry, "spikes": [], "snapshots_scanned": 0}
        _SPIKE_CACHE[cache_key] = (result, _time.time())
        return result

    exp_date = datetime.strptime(active_expiry, "%Y-%m-%d").date()
    today_date = today_ist()
    trading_days_left = trading_days_between(today_date, exp_date)
    T = max(trading_days_left, 0.5) / 252
    r_f = 0.065

    # Group by timestamp, in chronological order.
    by_ts: dict = {}
    for r in rows:
        by_ts.setdefault(r["timestamp"], []).append(r)
    timestamps = sorted(by_ts.keys())

    # Running state per (strike, side, metric) -> open episode dict, keyed
    # so a run of consecutive flagged snapshots becomes one episode.
    open_episodes: dict = {}
    closed_episodes: list = []

    def _close(key):
        ep = open_episodes.pop(key, None)
        if ep:
            closed_episodes.append(ep)

    for ts in timestamps:
        ts_rows = by_ts[ts]
        ce_map = {r["strike"]: r for r in ts_rows if r["option_type"] == "CE"}
        pe_map = {r["strike"]: r for r in ts_rows if r["option_type"] == "PE"}
        strikes = sorted(set(list(ce_map.keys()) + list(pe_map.keys())))
        if not strikes:
            continue

        fut_row = next((r for r in ts_rows if r["option_type"] == "FUT" and (r.get("last_price") or 0) > 0), None)
        if fut_row:
            F = fut_row["last_price"]
        else:
            # same ATM-straddle estimate as the live chain
            best, best_diff = None, float("inf")
            for s in strikes:
                c, p = ce_map.get(s), pe_map.get(s)
                if c and p and (c.get("last_price") or 0) > 0 and (p.get("last_price") or 0) > 0:
                    diff = abs(c["last_price"] - p["last_price"])
                    if diff < best_diff:
                        best_diff, best = diff, s
            F = best or strikes[len(strikes) // 2]

        iv_by_strike = {"ce": {}, "pe": {}}
        vol_by_strike = {"ce": {}, "pe": {}}
        liquid_by_strike = {"ce": {}, "pe": {}}
        for side, smap in (("ce", ce_map), ("pe", pe_map)):
            is_call = side == "ce"
            for s in strikes:
                row = smap.get(s)
                if not row:
                    continue
                vol = row.get("volume") or 0
                oi = row.get("oi") or 0
                vol_by_strike[side][s] = vol
                liquid = oi >= MIN_OI_FOR_IV or vol >= MIN_VOL_FOR_IV
                liquid_by_strike[side][s] = liquid
                if liquid:
                    iv = calculate_iv(row.get("last_price") or 0, F, s, T, r_f, is_call)
                    if iv is not None:
                        iv_by_strike[side][s] = iv

        for side in ("ce", "pe"):
            for idx, s in enumerate(strikes):
                # IV spike
                iv_val = iv_by_strike[side].get(s)
                iv_key = (s, side, "IV")
                is_iv_spike = False
                iv_avg = None
                if iv_val is not None:
                    iv_avg = _local_avg(iv_by_strike[side], strikes, idx, IV_SPIKE_WINDOW)
                    if iv_avg and iv_avg > 0 and iv_val >= iv_avg * IV_SPIKE_THRESHOLD:
                        is_iv_spike = True
                if is_iv_spike:
                    ep = open_episodes.get(iv_key)
                    if not ep:
                        ep = {"strike": s, "side": side.upper(), "metric": "IV", "start_time": ts,
                              "peak_time": ts, "peak_value": iv_val, "baseline": round(iv_avg, 2)}
                        open_episodes[iv_key] = ep
                    ep["end_time"] = ts
                    if iv_val > ep["peak_value"]:
                        ep["peak_value"], ep["peak_time"], ep["baseline"] = iv_val, ts, round(iv_avg, 2)
                else:
                    _close(iv_key)

                # Volume spike
                vol_val = vol_by_strike[side].get(s)
                vol_key = (s, side, "VOL")
                is_vol_spike = False
                vol_avg = None
                if vol_val is not None and vol_val >= MIN_VOL_FOR_SPIKE:
                    vol_avg = _local_avg(vol_by_strike[side], strikes, idx, VOL_SPIKE_WINDOW)
                    if vol_avg and vol_avg >= MIN_VOL_BASELINE_FOR_SPIKE and vol_val >= vol_avg * VOL_SPIKE_THRESHOLD:
                        is_vol_spike = True
                if is_vol_spike:
                    ep = open_episodes.get(vol_key)
                    if not ep:
                        ep = {"strike": s, "side": side.upper(), "metric": "VOL", "start_time": ts,
                              "peak_time": ts, "peak_value": vol_val, "baseline": round(vol_avg, 1)}
                        open_episodes[vol_key] = ep
                    ep["end_time"] = ts
                    if vol_val > ep["peak_value"]:
                        ep["peak_value"], ep["peak_time"], ep["baseline"] = vol_val, ts, round(vol_avg, 1)
                else:
                    _close(vol_key)

    # Close out anything still open at the end of the scan (still spiking as
    # of the latest snapshot) -- these are the "live right now" ones too.
    for key in list(open_episodes.keys()):
        _close(key)

    latest_ts = timestamps[-1] if timestamps else None
    for ep in closed_episodes:
        ep["ongoing"] = ep["end_time"] == latest_ts
        if ep["baseline"]:
            ep["magnitude_pct"] = round((ep["peak_value"] / ep["baseline"] - 1) * 100, 1)
        else:
            ep["magnitude_pct"] = None

    closed_episodes.sort(key=lambda e: e["peak_time"], reverse=True)

    result = {
        "symbol": symbol,
        "expiry": active_expiry,
        "spikes": closed_episodes,
        "snapshots_scanned": len(timestamps),
        "latest_timestamp": latest_ts,
    }
    _SPIKE_CACHE[cache_key] = (result, _time.time())
    return result
