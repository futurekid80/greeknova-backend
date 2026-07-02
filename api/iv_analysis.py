from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
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

    return round(sigma * 100, 2) if 0 < sigma < 5 else None


RISK_FREE_RATE = 0.065  # 6.5% India 10yr Gsec

# IVR thresholds — market standard (CBOE / tastytrade methodology)
# IVR 0-25   = Low IV       → options relatively cheap historically
# IVR 25-50  = Normal IV    → no clear edge
# IVR 50-75  = Elevated IV  → above average, mild selling edge
# IVR 75-100 = High IV      → expensive, strong premium selling environment

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
    "PAYTM","NYKAA","PERSISTENT","DIXON",
    "BSE","MCX","TMPV","LTIM","GODREJPROP","DIVISLAB","COFORGE","ANGELONE","CDSL","OIL",

# How many trading days to look back for IVR/IVP
# Market standard = 252 (1 year). We use whatever history exists,
# growing naturally toward 252 as oi_snapshots + iv_history accumulates.
IV_LOOKBACK_DAYS = 252


def _persist_iv_history(supabase, iv_rows: list):
    """
    Persist today's computed ATM IV to iv_history table.
    Upserts on (trade_date, symbol) — safe to call multiple times.
    """
    if not iv_rows:
        return
    try:
        supabase.from_("iv_history") \
            .upsert(iv_rows, on_conflict="trade_date,symbol") \
            .execute()
        print(f"[IV] Persisted {len(iv_rows)} IV rows to iv_history")
    except Exception as e:
        print(f"[IV] iv_history persist failed: {e}")


def _load_iv_history(supabase, symbols: list, lookback_days: int = IV_LOOKBACK_DAYS) -> dict:
    """
    Load historical ATM IV from iv_history table.
    Returns {symbol: [(date_str, iv_pct), ...]} sorted oldest→newest.
    This is the long-term store — grows toward 252 trading days.
    """
    cutoff = (date_type.today() - timedelta(days=lookback_days + 30)).isoformat()
    try:
        rows = supabase.from_("iv_history") \
            .select("trade_date, symbol, atm_iv") \
            .gte("trade_date", cutoff) \
            .in_("symbol", symbols) \
            .order("trade_date", desc=False) \
            .limit(lookback_days * len(symbols)) \
            .execute()
        result = {}
        for r in (rows.data or []):
            sym = r["symbol"]
            iv  = r.get("atm_iv")
            if iv is not None:
                result.setdefault(sym, []).append((r["trade_date"], float(iv)))
        return result
    except Exception as e:
        print(f"[IV] iv_history load failed: {e}")
        return {}


def get_iv_analysis(symbol: str = None, date: str = None):
    """
    Calculate IV, IVR, IVP, Expected Move for one symbol or all symbols.

    IVR/IVP methodology (market standard — CBOE / tastytrade):
    - Uses up to 252 trading days of ATM IV history
    - History sourced from: iv_history table (long-term) + oi_snapshots (recent 30d)
    - IVR = (current IV - period low) / (period high - period low) × 100
    - IVP = % of days in lookback where IV was BELOW current IV
    - As history grows toward 252d, IVR/IVP becomes increasingly reliable
    """
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    symbols = [symbol.upper()] if symbol else SYMBOLS

    cache_key = f"iv_{','.join(symbols)}_{today}"
    cached = _get_cache(cache_key)
    if cached:
        print(f"[IV] Serving from cache: {cache_key}")
        return cached

    # ── Latest snapshot timestamp ─────────────────────────────────────────────
    ts_q = supabase.from_("oi_snapshots") \
        .select("timestamp") \
        .eq("symbol", "NIFTY") \
        .gte("timestamp", f"{today}T00:00:00+00:00") \
        .order("timestamp", desc=True) \
        .limit(1).execute()

    if not ts_q.data:
        ts_q = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .eq("symbol", "NIFTY") \
            .order("timestamp", desc=True) \
            .limit(1).execute()

    if not ts_q.data:
        return {"error": "No data available", "results": []}

    latest_ts = ts_q.data[0]["timestamp"]

    # ── CMP for all symbols ───────────────────────────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices") \
            .select("symbol, cmp") \
            .order("timestamp", desc=True) \
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
        batch = supabase.from_("oi_snapshots") \
            .select("symbol, strike, option_type, last_price, expiry, oi") \
            .eq("timestamp", latest_ts) \
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        snap_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    from collections import defaultdict
    sym_options: dict = defaultdict(list)
    for row in snap_data:
        sym_options[row["symbol"]].append(row)

    # ── Load long-term IV history from iv_history table ───────────────────────
    # This is the persistent store that grows toward 252 trading days
    iv_history_store = _load_iv_history(supabase, symbols)
    print(f"[IV] iv_history loaded: {len(iv_history_store)} symbols, "
          f"max {max((len(v) for v in iv_history_store.values()), default=0)} days")

    # ── Batch-fetch last 30 days from oi_snapshots ────────────────────────────
    # Used to fill gaps + compute today's IV
    base_date = datetime.now(timezone.utc).date()

    hist_dates: dict = {}
    for i in range(1, 31):
        d = (base_date - timedelta(days=i)).isoformat()
        day_ts = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .eq("symbol", "NIFTY") \
            .gte("timestamp", f"{d}T00:00:00+00:00") \
            .lt("timestamp",  f"{d}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(1).execute()
        if day_ts.data:
            hist_dates[d] = day_ts.data[0]["timestamp"]

    hist_cmp_by_date: dict = {}
    for hist_d in list(hist_dates.keys()):
        cmp_q = supabase.from_("cmp_prices") \
            .select("symbol, cmp") \
            .gte("timestamp", f"{hist_d}T00:00:00+00:00") \
            .lt("timestamp",  f"{hist_d}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(500).execute().data or []
        day_cmp: dict = {}
        seen_syms: set = set()
        for r in cmp_q:
            if r["symbol"] not in seen_syms:
                day_cmp[r["symbol"]] = float(r["cmp"])
                seen_syms.add(r["symbol"])
        hist_cmp_by_date[hist_d] = day_cmp

    hist_opts_by_date: dict = {}
    for hist_d in list(hist_dates.keys()):
        opts_q = supabase.from_("oi_snapshots") \
            .select("symbol, strike, option_type, last_price, expiry") \
            .gte("timestamp", f"{hist_d}T09:00:00+00:00") \
            .lt("timestamp",  f"{hist_d}T23:59:59+00:00") \
            .order("timestamp", desc=True) \
            .limit(5000).execute().data or []
        day_opts: dict = {}
        seen_keys: set = set()
        for r in opts_q:
            key = f"{r['symbol']}_{r['strike']}_{r['option_type']}"
            if key not in seen_keys:
                sym_k = r["symbol"]
                if sym_k not in day_opts:
                    day_opts[sym_k] = []
                day_opts[sym_k].append(r)
                seen_keys.add(key)
        hist_opts_by_date[hist_d] = day_opts

    results = []
    # Collect today's IV for all symbols to persist to iv_history
    today_iv_rows = []

    for sym in symbols:
        cmp = cmp_map.get(sym, 0)
        if cmp <= 0:
            continue

        options = sym_options.get(sym, [])
        if not options:
            continue

        expiries = sorted(set(r["expiry"] for r in options if r["expiry"] and r["expiry"] >= today))
        if not expiries:
            expiries = sorted(set(r["expiry"] for r in options if r["expiry"]))
        if not expiries:
            continue

        nearest_expiry = expiries[0]

        try:
            exp_dt = datetime.strptime(nearest_expiry, "%Y-%m-%d")
            now_dt = datetime.strptime(today, "%Y-%m-%d")
            dte = (exp_dt - now_dt).days
            T = max(dte / 365, 1/365)
        except:
            continue

        expiry_options = [r for r in options if r["expiry"] == nearest_expiry]
        strikes = sorted(set(r["strike"] for r in expiry_options))
        if not strikes:
            continue

        atm_strike = min(strikes, key=lambda x: abs(x - cmp))

        atm_ce = next((r["last_price"] for r in expiry_options
                       if r["strike"] == atm_strike and r["option_type"] == "CE"), None)
        atm_pe = next((r["last_price"] for r in expiry_options
                       if r["strike"] == atm_strike and r["option_type"] == "PE"), None)

        if not atm_ce or not atm_pe:
            continue

        atm_ce = float(atm_ce)
        atm_pe = float(atm_pe)

        iv_ce = calculate_iv(atm_ce, cmp, atm_strike, T, RISK_FREE_RATE, 'CE')
        iv_pe = calculate_iv(atm_pe, cmp, atm_strike, T, RISK_FREE_RATE, 'PE')

        iv_values = [v for v in [iv_ce, iv_pe] if v is not None]
        if not iv_values:
            continue
        current_iv = round(sum(iv_values) / len(iv_values), 2)

        atm_straddle      = atm_ce + atm_pe
        expected_move_pts  = round(atm_straddle * 0.68, 1)
        expected_move_pct  = round((expected_move_pts / cmp) * 100, 2)
        upper_range        = round(cmp + expected_move_pts, 1)
        lower_range        = round(cmp - expected_move_pts, 1)
        expected_move_2sd_pts = round(atm_straddle * 1.36, 1)
        upper_range_2sd    = round(cmp + expected_move_2sd_pts, 1)
        lower_range_2sd    = round(cmp - expected_move_2sd_pts, 1)

        # ── Build IV history: iv_history table + recent oi_snapshots ─────────
        # Strategy:
        #   1. Start with persistent iv_history (can be up to 252 days)
        #   2. Fill recent 30 days from oi_snapshots (catches gaps + recent data)
        #   3. Deduplicate by date — iv_history takes precedence for old dates
        #   4. Add today's current_iv

        # From iv_history table (older, persistent)
        hist_from_store = {d: iv for d, iv in iv_history_store.get(sym, [])}

        # From oi_snapshots (recent 30 days)
        for hist_d in list(hist_dates.keys()):
            if hist_d == today:
                continue
            # Only recompute if not already in iv_history store
            if hist_d in hist_from_store:
                continue
            try:
                h_cmp = hist_cmp_by_date.get(hist_d, {}).get(sym, cmp)
                hist_opts = hist_opts_by_date.get(hist_d, {}).get(sym, [])
                if not hist_opts:
                    continue

                hist_expiries = sorted(set(
                    r["expiry"] for r in hist_opts
                    if r["expiry"] and r["expiry"] >= hist_d
                ))
                if not hist_expiries:
                    hist_expiries = sorted(set(r["expiry"] for r in hist_opts if r["expiry"]))
                if not hist_expiries:
                    continue

                h_expiry = hist_expiries[0]
                h_dte    = (datetime.strptime(h_expiry, "%Y-%m-%d") - datetime.strptime(hist_d, "%Y-%m-%d")).days
                h_T      = max(h_dte / 365, 1/365)

                h_expiry_opts = [r for r in hist_opts if r["expiry"] == h_expiry]
                h_strikes = sorted(set(r["strike"] for r in h_expiry_opts))
                if not h_strikes:
                    continue

                h_atm = min(h_strikes, key=lambda x: abs(x - h_cmp))
                h_ce  = next((float(r["last_price"]) for r in h_expiry_opts
                              if r["strike"] == h_atm and r["option_type"] == "CE"), None)
                h_pe  = next((float(r["last_price"]) for r in h_expiry_opts
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
                    if avg_iv >= 5.0:
                        hist_from_store[hist_d] = avg_iv

            except Exception:
                continue

        # Merge all historical IVs (sorted by date)
        all_hist_sorted = sorted(hist_from_store.items())  # [(date, iv), ...]
        hist_ivs = [iv for _, iv in all_hist_sorted]

        # ── IVR and IVP — market standard methodology ─────────────────────────
        # Include current_iv in the full dataset for percentile calculation
        all_ivs = hist_ivs + [current_iv]
        n_days = len(all_ivs)

        if n_days >= 2:
            iv_period_high = max(all_ivs)
            iv_period_low  = min(all_ivs)
            iv_range = iv_period_high - iv_period_low

            # IVR: where does current IV sit in the period range?
            # 0 = at period low, 100 = at period high
            ivr = round(((current_iv - iv_period_low) / iv_range) * 100, 1) if iv_range > 0 else 50.0

            # IVP: what % of historical days had IV BELOW current IV?
            # Standard: use hist_ivs only (not including today) for cleaner percentile
            ivp = round(sum(1 for x in hist_ivs if x < current_iv) / len(hist_ivs) * 100, 1) \
                if hist_ivs else None
        else:
            ivr = None
            ivp = None
            iv_period_high = current_iv
            iv_period_low  = current_iv

        # History quality label
        if n_days >= 200:
            history_quality = "52W"
        elif n_days >= 100:
            history_quality = f"{n_days}d"
        elif n_days >= 30:
            history_quality = f"{n_days}d"
        else:
            history_quality = f"{n_days}d"

        # ── IV signal labels — market standard thresholds ─────────────────────
        if ivr is None:
            iv_signal = "INSUFFICIENT_DATA"
            iv_label  = "Need more history"
        elif ivr >= 75:
            iv_signal = "HIGH_IV"
            iv_label  = f"High IV — IVR {ivr:.0f}"
        elif ivr >= 50:
            iv_signal = "ELEVATED_IV"
            iv_label  = f"Elevated IV — IVR {ivr:.0f}"
        elif ivr >= 25:
            iv_signal = "NORMAL_IV"
            iv_label  = f"Normal IV — IVR {ivr:.0f}"
        else:
            iv_signal = "LOW_IV"
            iv_label  = f"Low IV — IVR {ivr:.0f}"

        # Strategy signals — SEBI compliant (descriptive, not prescriptive)
        strategies = []
        if ivr is not None:
            if ivr >= 75 and dte >= 7:
                strategies = ["Iron Condor", "Short Strangle"]
            elif ivr >= 75 and dte < 7:
                strategies = ["Short Straddle", "Credit Spreads"]
            elif ivr <= 25 and dte >= 14:
                strategies = ["Long Straddle", "Long Strangle"]
            elif ivr <= 25 and dte < 14:
                strategies = ["Debit Spreads"]
            else:
                strategies = ["Calendar Spread", "Directional spreads"]

        # ── Collect for iv_history persistence ───────────────────────────────
        today_iv_rows.append({
            "trade_date": today,
            "symbol":     sym,
            "atm_strike": atm_strike,
            "atm_iv":     current_iv,
            "atm_ce_iv":  iv_ce,
            "atm_pe_iv":  iv_pe,
            "expiry":     nearest_expiry,
            "dte":        dte,
            "cmp":        cmp,
        })

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
            "iv_period_high":      round(iv_period_high, 2),
            "iv_period_low":       round(iv_period_low, 2),
            "iv_52w_high":         round(iv_period_high, 2),  # kept for backward compat
            "iv_52w_low":          round(iv_period_low, 2),   # kept for backward compat
            "ivr":                 ivr,
            "ivp":                 ivp,
            "iv_history_days":     n_days,
            "history_quality":     history_quality,
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

    # Sort by IVR descending
    results.sort(key=lambda x: (x["ivr"] or 0), reverse=True)

    # ── Persist today's IV to iv_history ─────────────────────────────────────
    # This runs silently — failures don't affect the response
    # Over time this builds toward 252-day IVR/IVP
    if today_iv_rows:
        _persist_iv_history(supabase, today_iv_rows)

    result = {
        "date":      today,
        "timestamp": latest_ts,
        "total":     len(results),
        "results":   results,
    }
    _set_cache(cache_key, result)
    return result
