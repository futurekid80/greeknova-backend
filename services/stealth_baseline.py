"""
services/stealth_baseline.py  (Nov 3 2026)

Two fixes for the Option Strike Stealth Buildup EOD window, both about reading
"did the price really move?" honestly:

1. OFFICIAL previous close as the premium baseline.
   The scanner used to take yesterday's LAST SNAPSHOT price (a last-traded
   price taken after the bell, e.g. CONCOR 425 CE = 9.80) as the "old premium".
   Charts show the exchange's official close (10.35) -- a different number on thin
   strikes. Kite's quote `ohlc.close` is that official previous close, so the
   EOD window now uses it (falls back to the snapshot price if Kite is
   unavailable or has no close for the contract).

2. Writing vs Buying judged on IMPLIED VOLATILITY, not the raw premium move.
   A call premium can fall simply because the stock fell (delta) or because a
   day of time passed (theta) -- neither is anybody "writing". We compare the
   option's implied vol at yesterday's close (prev spot, prev close price) with
   its implied vol now (spot now, price now): IV down => sellers (WRITING),
   IV up => buyers (BUYING), little change => Quiet.
   Black-Scholes, r = 6.5%, no dividends; European-style approximation is fine
   for a direction label. Any failure falls back to the old premium-% rule.
"""
import math
from datetime import datetime, timezone, timedelta

RISK_FREE = 0.065
IV_REL_THRESHOLD = 0.03      # +-3% relative change in IV (30.0 -> 29.1) separates Writing/Buying from Quiet
BSE_SYMBOLS = {"SENSEX", "BANKEX"}
INDEX_SPOT_KEYS = {
    "NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX", "BANKEX": "BSE:BANKEX",
}

_OPT_CLOSE_CACHE: dict = {}   # ist_date -> {tradingsymbol: official prev close}
_SPOT_CLOSE_CACHE: dict = {}  # ist_date -> {symbol: prev close of the underlying}


def _kite():
    from services.kite_auth import get_kite_client
    return get_kite_client()


def _quote_batches(kite, keys, size=400):
    out = {}
    for i in range(0, len(keys), size):
        try:
            out.update(kite.quote(keys[i:i + size]) or {})
        except Exception as e:
            print(f"[StealthBaseline] quote batch failed: {e}")
    return out


def get_prev_option_closes(day: str, items) -> dict:
    """items: iterable of (symbol, tradingsymbol). Returns {tradingsymbol: official previous close}.
    Cached per day -- the previous close never changes during the session."""
    cache = _OPT_CLOSE_CACHE.setdefault(day, {})
    for old_day in [d for d in _OPT_CLOSE_CACHE if d != day]:
        _OPT_CLOSE_CACHE.pop(old_day, None)
    todo = {}
    for sym, tsym in items:
        if tsym not in cache:
            todo[f"{'BFO' if sym in BSE_SYMBOLS else 'NFO'}:{tsym}"] = tsym
    if todo:
        try:
            quotes = _quote_batches(_kite(), list(todo.keys()))
            for key, tsym in todo.items():
                c = ((quotes.get(key) or {}).get("ohlc") or {}).get("close")
                cache[tsym] = float(c) if c else 0.0   # 0.0 = "asked, none available" so we don't re-ask every tick
        except Exception as e:
            print(f"[StealthBaseline] option prev-close fetch failed: {e}")
    return {t: v for t, v in cache.items() if v and v > 0}


def get_prev_spots(day: str, symbols) -> dict:
    cache = _SPOT_CLOSE_CACHE.setdefault(day, {})
    for old_day in [d for d in _SPOT_CLOSE_CACHE if d != day]:
        _SPOT_CLOSE_CACHE.pop(old_day, None)
    todo = {}
    for s in symbols:
        if s not in cache:
            todo[INDEX_SPOT_KEYS.get(s, f"NSE:{s}")] = s
    if todo:
        try:
            quotes = _quote_batches(_kite(), list(todo.keys()), 450)
            for key, s in todo.items():
                c = ((quotes.get(key) or {}).get("ohlc") or {}).get("close")
                cache[s] = float(c) if c else 0.0
        except Exception as e:
            print(f"[StealthBaseline] spot prev-close fetch failed: {e}")
    return {s: v for s, v in cache.items() if v and v > 0}


# ---------------------------------------------------------------- Black-Scholes
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, sigma, is_call, r=RISK_FREE):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    disc = math.exp(-r * T)
    if is_call:
        return S * _ncdf(d1) - K * disc * _ncdf(d2)
    return K * disc * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price, S, K, T, is_call, r=RISK_FREE):
    """Bisection. Returns None when the price cannot be explained (below intrinsic, absurd, bad inputs)."""
    try:
        if price <= 0 or S <= 0 or K <= 0 or T <= 0:
            return None
        lo, hi = 0.01, 4.0
        if price < bs_price(S, K, T, lo, is_call, r) - 1e-9 or price > bs_price(S, K, T, hi, is_call, r):
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            if bs_price(S, K, T, mid, is_call, r) > price:
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2
    except Exception:
        return None


def _expiry_dt(expiry) -> datetime:
    d = datetime.fromisoformat(str(expiry)[:10])
    return d.replace(hour=10, minute=0, tzinfo=timezone.utc)   # 15:30 IST


def _ts(ts) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def iv_change(opt_type, strike, expiry, spot_old, prem_old, ts_old, spot_new, prem_new, ts_new):
    """(iv_old, iv_new) as fractions, or None if either can't be solved."""
    try:
        exp = _expiry_dt(expiry)
        t_old = (exp - _ts(ts_old)).total_seconds() / (365 * 86400)
        t_new = (exp - _ts(ts_new)).total_seconds() / (365 * 86400)
        is_call = opt_type == "CE"
        iv_o = implied_vol(prem_old, spot_old, float(strike), t_old, is_call)
        iv_n = implied_vol(prem_new, spot_new, float(strike), t_new, is_call)
        if iv_o and iv_n:
            return iv_o, iv_n
    except Exception:
        pass
    return None


def activity_from_iv(opt_type: str, iv_old: float, iv_new: float):
    side = "Call" if opt_type == "CE" else "Put"
    rel = iv_new / iv_old - 1.0
    if rel <= -IV_REL_THRESHOLD:
        return "WRITING", f"{side} Writing"
    if rel >= IV_REL_THRESHOLD:
        return "BUYING", f"{side} Buying"
    return "NEUTRAL", f"{side} Quiet"
