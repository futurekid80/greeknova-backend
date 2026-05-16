from utils.db import get_supabase
from datetime import datetime, timezone, timedelta
import math
import time

# ── Simple in-memory cache (5 min TTL) ───────────────────────────────────────
_cache: dict = {}
_cache_ttl = 300  # 5 minutes

def _get_cache(key: str):
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < _cache_ttl:
            return val
    return None

def _set_cache(key: str, val):
    _cache[key] = (val, time.time())

# ── Black-Scholes IV via Newton-Raphson ──────────────────────────────────────

def bs_price(S, K, T, r, sigma, opt_type):
    """Black-Scholes option price"""
    if T <= 0 or sigma <= 0:
        return max(0, S - K) if opt_type == 'CE' else max(0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    nd1 = norm_cdf(d1)
    nd2 = norm_cdf(d2)
    if opt_type == 'CE':
        return S * nd1 - K * math.exp(-r * T) * nd2
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def bs_vega(S, K, T, r, sigma):
    """Black-Scholes vega"""
    if T <= 0 or sigma <= 0:
        return 0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return S * math.sqrt(T) * norm_pdf(d1)

def norm_cdf(x):
    """Standard normal CDF — Abramowitz & Stegun approximation"""
    if x < 0:
        return 1 - norm_cdf(-x)
    k = 1 / (1 + 0.2316419 * x)
    poly = k * (0.319381530 + k * (-0.356563782 + k * (1.781477937 + k * (-1.821255978 + k * 1.330274429))))
    return 1 - norm_pdf(x) * poly

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def calculate_iv(market_price, S, K, T, r, opt_type, max_iter=100, tol=1e-6):
    """Newton-Raphson IV solver"""
    if T <= 0 or market_price <= 0:
        return None
    intrinsic = max(0, S - K) if opt_type == 'CE' else max(0, K - S)
    if market_price <= intrinsic:
        return None

    # Initial guess using Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * market_price / S

    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, opt_type)
        vega  = bs_vega(S, K, T, r, sigma)
        if vega < 1e-10:
            break
        diff = market_price - price
        if abs(diff) < tol:
            break
        sigma = sigma + diff / vega
        if sigma <= 0:
            sigma = 0.001

    return round(sigma * 100, 2) if 0 < sigma < 5 else None  # Return as % capped at 500%


RISK_FREE_RATE = 0.065  # 6.5% India 10yr Gsec

SYMBOLS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY",
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH","TATACONSUM",
    "TATASTEEL","ADANIENT","POWERGRID","NTPC","ONGC","JSWSTEEL","COALINDIA",
    "BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA",
    "DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE",
    "INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT",
    "ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS",
    "HAL","INDIGO","PFC","RECLTD","SAIL","TATAPOWER","VEDL",
]


def get_iv_analysis(symbol: str = None, date: str = None):
    """
    Calculate IV, IVR, Expected Move for one symbol or all symbols.
    Uses ATM options from latest snapshot + historical IV from stored data.
    """
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    symbols = [symbol.upper()] if symbol else SYMBOLS

    # Check cache first
    cache_key = f"iv_{','.join(symbols)}_{today}"
    cached = _get_cache(cache_key)
    if cached:
        print(f"[IV] Serving from cache: {cache_key}")
        return cached

    # ── Latest snapshot timestamp ─────────────────────────────────────────────
    ts_q = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    if not ts_q.data:
        # Fallback to last available
        ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .order("timestamp", desc=True)\
            .limit(1).execute()

    if not ts_q.data:
        return {"error": "No data available", "results": []}

    latest_ts = ts_q.data[0]["timestamp"]

    # ── CMP for all symbols ───────────────────────────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        cmp_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    cmp_map = {}
    seen = set()
    for c in cmp_raw:
        if c["symbol"] not in seen:
            cmp_map[c["symbol"]] = float(c["cmp"])
            seen.add(c["symbol"])

    # ── Latest options snapshot ───────────────────────────────────────────────
    snap_data = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("symbol, strike, option_type, last_price, expiry, oi")\
            .eq("timestamp", latest_ts)\
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        snap_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # Group by symbol
    from collections import defaultdict
    sym_options: dict = defaultdict(list)
    for row in snap_data:
        sym_options[row["symbol"]].append(row)

    # ── Historical IV for IVR calculation ─────────────────────────────────────
    # Probe one EOD timestamp per day — fast, avoids timeout
    # For each of last 30 days, get the LAST snapshot of that day (15:30 IST)
    hist_dates: dict = {}
    base_date = datetime.now(timezone.utc).date()
    for i in range(1, 31):  # last 30 days, skip today
        d = (base_date - timedelta(days=i)).isoformat()
        day_ts = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if day_ts.data:
            hist_dates[d] = day_ts.data[0]["timestamp"]

    hist_timestamps = list(hist_dates.values())  # one EOD timestamp per day

    results = []

    for sym in symbols:
        cmp = cmp_map.get(sym, 0)
        if cmp <= 0:
            continue

        options = sym_options.get(sym, [])
        if not options:
            continue

        # ── Find nearest expiry ───────────────────────────────────────────────
        expiries = sorted(set(r["expiry"] for r in options if r["expiry"] and r["expiry"] >= today))
        if not expiries:
            expiries = sorted(set(r["expiry"] for r in options if r["expiry"]))
        if not expiries:
            continue

        nearest_expiry = expiries[0]

        # Days to expiry
        try:
            exp_dt = datetime.strptime(nearest_expiry, "%Y-%m-%d")
            now_dt = datetime.strptime(today, "%Y-%m-%d")
            dte = (exp_dt - now_dt).days
            T = max(dte / 365, 1/365)  # minimum 1 day
        except:
            continue

        # ── Find ATM strike ───────────────────────────────────────────────────
        expiry_options = [r for r in options if r["expiry"] == nearest_expiry]
        strikes = sorted(set(r["strike"] for r in expiry_options))
        if not strikes:
            continue

        atm_strike = min(strikes, key=lambda x: abs(x - cmp))

        # ATM CE and PE prices
        atm_ce = next((r["last_price"] for r in expiry_options
                       if r["strike"] == atm_strike and r["option_type"] == "CE"), None)
        atm_pe = next((r["last_price"] for r in expiry_options
                       if r["strike"] == atm_strike and r["option_type"] == "PE"), None)

        if not atm_ce or not atm_pe:
            continue

        atm_ce = float(atm_ce)
        atm_pe = float(atm_pe)

        # ── Calculate IV for ATM CE and PE ────────────────────────────────────
        iv_ce = calculate_iv(atm_ce, cmp, atm_strike, T, RISK_FREE_RATE, 'CE')
        iv_pe = calculate_iv(atm_pe, cmp, atm_strike, T, RISK_FREE_RATE, 'PE')

        # Average IV (use both if available)
        iv_values = [v for v in [iv_ce, iv_pe] if v is not None]
        if not iv_values:
            continue
        current_iv = round(sum(iv_values) / len(iv_values), 2)

        # ── Expected Move ─────────────────────────────────────────────────────
        # ATM straddle price × 0.68 = 1 standard deviation move
        atm_straddle = atm_ce + atm_pe
        expected_move_pts  = round(atm_straddle * 0.68, 1)
        expected_move_pct  = round((expected_move_pts / cmp) * 100, 2)
        upper_range = round(cmp + expected_move_pts, 1)
        lower_range = round(cmp - expected_move_pts, 1)

        # 2SD range (95% probability)
        expected_move_2sd_pts = round(atm_straddle * 1.36, 1)
        upper_range_2sd = round(cmp + expected_move_2sd_pts, 1)
        lower_range_2sd = round(cmp - expected_move_2sd_pts, 1)

        # ── Historical IV for IVR ─────────────────────────────────────────────
        # Sample IV from historical EOD snapshots
        hist_ivs = []

        # We compute IV for up to 30 historical days
        for hist_ts in hist_timestamps[:30]:
            hist_d = hist_ts[:10]
            if hist_d == today:
                continue  # skip today — already have current_iv

            try:
                # Get ATM options for this historical timestamp
                hist_opts = supabase.from_("oi_snapshots")\
                    .select("strike, option_type, last_price, expiry")\
                    .eq("symbol", sym)\
                    .gte("timestamp", f"{hist_d}T00:00:00+00:00")\
                    .lt("timestamp",  f"{hist_d}T23:59:59+00:00")\
                    .order("timestamp", desc=True)\
                    .limit(200).execute().data or []

                if not hist_opts:
                    continue

                # Use same expiry structure logic
                hist_expiries = sorted(set(r["expiry"] for r in hist_opts if r["expiry"] and r["expiry"] >= hist_d))
                if not hist_expiries:
                    hist_expiries = sorted(set(r["expiry"] for r in hist_opts if r["expiry"]))
                if not hist_expiries:
                    continue

                h_expiry = hist_expiries[0]
                try:
                    h_exp_dt = datetime.strptime(h_expiry, "%Y-%m-%d")
                    h_now_dt = datetime.strptime(hist_d, "%Y-%m-%d")
                    h_dte = (h_exp_dt - h_now_dt).days
                    h_T = max(h_dte / 365, 1/365)
                except:
                    continue

                # Get historical CMP — use date range not exact timestamp
                hist_cmp_q = supabase.from_("cmp_prices")\
                    .select("cmp")\
                    .eq("symbol", sym)\
                    .gte("timestamp", f"{hist_d}T00:00:00+00:00")\
                    .lt("timestamp",  f"{hist_d}T23:59:59+00:00")\
                    .order("timestamp", desc=True)\
                    .limit(1).execute()
                h_cmp = float(hist_cmp_q.data[0]["cmp"]) if hist_cmp_q.data else cmp

                h_expiry_opts = [r for r in hist_opts if r["expiry"] == h_expiry]
                h_strikes = sorted(set(r["strike"] for r in h_expiry_opts))
                if not h_strikes:
                    continue
                h_atm = min(h_strikes, key=lambda x: abs(x - h_cmp))

                h_ce = next((float(r["last_price"]) for r in h_expiry_opts
                             if r["strike"] == h_atm and r["option_type"] == "CE"), None)
                h_pe = next((float(r["last_price"]) for r in h_expiry_opts
                             if r["strike"] == h_atm and r["option_type"] == "PE"), None)

                h_iv_vals = []
                if h_ce:
                    h_iv = calculate_iv(h_ce, h_cmp, h_atm, h_T, RISK_FREE_RATE, 'CE')
                    if h_iv: h_iv_vals.append(h_iv)
                if h_pe:
                    h_iv = calculate_iv(h_pe, h_cmp, h_atm, h_T, RISK_FREE_RATE, 'PE')
                    if h_iv: h_iv_vals.append(h_iv)

                if h_iv_vals:
                    avg_iv = round(sum(h_iv_vals) / len(h_iv_vals), 2)
                    if avg_iv >= 5.0:  # filter bad data — holidays, low liquidity
                        hist_ivs.append(avg_iv)

            except Exception as e:
                continue

        # ── IVR and IVP calculation ───────────────────────────────────────────
        all_ivs = hist_ivs + [current_iv]

        if len(all_ivs) >= 2:
            iv_52w_high = max(all_ivs)
            iv_52w_low  = min(all_ivs)
            iv_range = iv_52w_high - iv_52w_low

            ivr = round(((current_iv - iv_52w_low) / iv_range) * 100, 1) if iv_range > 0 else 50.0
            ivp = round(sum(1 for x in all_ivs if x <= current_iv) / len(all_ivs) * 100, 1)
        else:
            ivr = None
            ivp = None
            iv_52w_high = current_iv
            iv_52w_low  = current_iv

        # IVR interpretation
        if ivr is None:
            iv_signal = "INSUFFICIENT_DATA"
            iv_label  = "Need more history"
        elif ivr >= 75:
            iv_signal = "HIGH_IV"
            iv_label  = "IV expensive — consider selling premium"
        elif ivr >= 50:
            iv_signal = "ELEVATED_IV"
            iv_label  = "IV above average — neutral to selling bias"
        elif ivr >= 25:
            iv_signal = "NORMAL_IV"
            iv_label  = "IV in normal range"
        else:
            iv_signal = "LOW_IV"
            iv_label  = "IV cheap — consider buying premium"

        # Strategy suggestions based on IVR + DTE
        strategies = []
        if ivr is not None:
            if ivr >= 75 and dte >= 7:
                strategies.append("Iron Condor")
                strategies.append("Short Strangle")
            elif ivr >= 75 and dte < 7:
                strategies.append("Short Straddle")
                strategies.append("Credit Spreads")
            elif ivr <= 25 and dte >= 14:
                strategies.append("Long Straddle")
                strategies.append("Long Strangle")
            elif ivr <= 25 and dte < 14:
                strategies.append("Debit Spreads")
            else:
                strategies.append("Calendar Spread")
                strategies.append("Directional spreads")

        results.append({
            "symbol":              sym,
            "cmp":                 cmp,
            "expiry":              nearest_expiry,
            "dte":                 dte,
            "atm_strike":          atm_strike,
            "atm_ce_ltp":          atm_ce,
            "atm_pe_ltp":          atm_pe,
            "atm_straddle":        round(atm_straddle, 2),
            "iv_ce":               iv_ce,
            "iv_pe":               iv_pe,
            "current_iv":          current_iv,
            "iv_52w_high":         round(iv_52w_high, 2),
            "iv_52w_low":          round(iv_52w_low, 2),
            "ivr":                 ivr,
            "ivp":                 ivp,
            "iv_history_days":     len(all_ivs),
            "iv_signal":           iv_signal,
            "iv_label":            iv_label,
            "strategies":          strategies,
            "expected_move_pts":   expected_move_pts,
            "expected_move_pct":   expected_move_pct,
            "upper_range":         upper_range,
            "lower_range":         lower_range,
            "upper_range_2sd":     upper_range_2sd,
            "lower_range_2sd":     lower_range_2sd,
            "is_index":            sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        })

    # Sort by IVR descending (highest IV first — most actionable)
    results.sort(key=lambda x: (x["ivr"] or 0), reverse=True)

    result = {
        "date":      today,
        "timestamp": latest_ts,
        "total":     len(results),
        "results":   results,
    }
    _set_cache(cache_key, result)
    return result
