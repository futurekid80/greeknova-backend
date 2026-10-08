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
