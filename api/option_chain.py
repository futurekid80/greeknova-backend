import math
from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

# ── Black-Scholes helpers ──────────────────────────────────────────────────────

def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_price(S, K, T, r, sigma, is_call):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if is_call:
            return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
        else:
            return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
    except:
        return 0.0

def bs_vega(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return S * norm_pdf(d1) * math.sqrt(T)
    except:
        return 0.0

def calculate_iv(market_price, S, K, T, r, is_call, max_iter=100):
    if market_price < 0.1 or T <= 0 or S <= 0:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, is_call)
        vega = bs_vega(S, K, T, r, sigma)
        if vega < 1e-10:
            return None
        diff = price - market_price
        if abs(diff) < 0.01:
            return round(sigma * 100, 2)
        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 5.0))
    return round(sigma * 100, 2)

def calculate_greeks(S, K, T, r, sigma, is_call):
    if T <= 0 or sigma <= 0 or S <= 0:
        return {}
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        delta = norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0
        gamma = norm_pdf(d1) / (S * sigma * math.sqrt(T))
        theta_raw = -(S * norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
        if is_call:
            theta_raw -= r * K * math.exp(-r * T) * norm_cdf(d2)
        else:
            theta_raw += r * K * math.exp(-r * T) * norm_cdf(-d2)
        theta = theta_raw / 365
        vega = S * norm_pdf(d1) * math.sqrt(T) / 100
        return {
            "delta": round(delta, 3),
            "gamma": round(gamma, 5),
            "theta": round(theta, 2),
            "vega":  round(vega, 2),
        }
    except:
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
    data_q = supabase.from_("oi_snapshots")\
        .select("strike, option_type, oi, volume, last_price, expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", latest_ts)

    if expiry:
        data_q = data_q.eq("expiry", expiry)

    rows = data_q.order("strike", desc=False).execute().data

    if not rows:
        return {"symbol": symbol, "chain": [], "spot": spot, "expiry": expiry}

    # ── Expiry & T ─────────────────────────────────────────────────────────────
    available_expiries = sorted(set(r["expiry"] for r in rows))
    active_expiry = expiry or available_expiries[0]
    rows = [r for r in rows if r["expiry"] == active_expiry]

    exp_date = datetime.strptime(active_expiry, "%Y-%m-%d").date()
    today_date = date_type.today()
    days_left = (exp_date - today_date).days
    T = max(days_left, 0.5) / 365   # min 0.5 days to avoid degenerate Greeks
    r_f = 0.065  # ~6.5% risk-free rate

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

    chain = []
    for strike in strikes:
        ce = ce_map.get(strike, {})
        pe = pe_map.get(strike, {})
        ce_ltp = ce.get("last_price", 0) or 0
        pe_ltp = pe.get("last_price", 0) or 0

        ce_iv  = calculate_iv(ce_ltp, spot, strike, T, r_f, True)
        pe_iv  = calculate_iv(pe_ltp, spot, strike, T, r_f, False)
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
                **calculate_greeks(spot, strike, T, r_f, ce_sig, True),
            },
            "pe": {
                "ltp":    pe_ltp,
                "iv":     pe_iv,
                "oi":     pe.get("oi", 0),
                "volume": pe.get("volume", 0),
                **calculate_greeks(spot, strike, T, r_f, pe_sig, False),
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
