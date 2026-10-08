"""Minimal, dependency-free Black-Scholes helpers: implied volatility (solved
from a traded premium) and gamma. No numpy/scipy needed — math.erf gives us
the normal CDF exactly, so this has zero new dependencies to install.
"""
import math

SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """Black-Scholes theoretical premium. T in years, r and sigma as decimals."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # At/after expiry (or degenerate inputs) — just the intrinsic value.
        if option_type == "CE":
            return max(0.0, S - K)
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Gamma is identical for a call and a put at the same strike/expiry/IV."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def _vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_vol(price: float, S: float, K: float, T: float, r: float, option_type: str,
                 initial_guess: float = 0.35, max_iter: int = 50, tol: float = 1e-4):
    """Back out IV from a traded premium via Newton-Raphson, falling back to
    bisection if Newton drifts out of bounds (common for deep ITM/OTM strikes
    where vega is tiny). Returns None if no sane solution exists — e.g. the
    premium violates a no-arbitrage bound, or the strike is effectively dead
    (illiquid stale premium)."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None

    # No-arbitrage sanity bounds — a premium outside these isn't a real
    # market price to invert (stale/bad tick), so don't manufacture an IV.
    if option_type == "CE":
        intrinsic = max(0.0, S - K * math.exp(-r * T))
        upper = S
    else:
        intrinsic = max(0.0, K * math.exp(-r * T) - S)
        upper = K
    if price < intrinsic - 1e-6 or price > upper + 1e-6:
        return None

    sigma = initial_guess
    for _ in range(max_iter):
        model_price = bs_price(S, K, T, r, sigma, option_type)
        diff = model_price - price
        if abs(diff) < tol:
            return max(sigma, 1e-4)
        v = _vega(S, K, T, r, sigma)
        if v < 1e-8:
            break
        sigma -= diff / v
        if sigma <= 0 or sigma > 5:
            break
    else:
        return max(sigma, 1e-4) if 0 < sigma <= 5 else None

    # Newton didn't converge cleanly — bisection is slower but never diverges.
    lo, hi = 1e-4, 5.0
    f_lo = bs_price(S, K, T, r, lo, option_type) - price
    f_hi = bs_price(S, K, T, r, hi, option_type) - price
    if f_lo * f_hi > 0:
        return None  # root isn't bracketed — genuinely no solution in range
    for _ in range(60):
        mid = (lo + hi) / 2
        f_mid = bs_price(S, K, T, r, mid, option_type) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def bs76_price(F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """Black-76: same shape as Black-Scholes, but F (the traded FUTURES
    price) stands in for spot everywhere a forward price is needed, and r
    is used only as a discount rate -- not as a drift assumption. This is
    the model Indian retail platforms (Sensibull and others) actually use
    for NSE options: futures already price in the interest+dividend carry,
    so there's no separate dividend-yield guess to get wrong, and no need
    to assume spot drifts at the risk-free rate (which breaks down exactly
    when futures trade at a discount to spot -- rare in India, but the
    plain Black-Scholes formula can't even represent it cleanly)."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        df = math.exp(-r * max(T, 0))
        if option_type == "CE":
            return df * max(0.0, F - K)
        return df * max(0.0, K - F)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    df = math.exp(-r * T)
    if option_type == "CE":
        return df * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    else:
        return df * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def bs76_gamma(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Gamma w.r.t. the futures price. Since the futures price moves ~1:1
    with spot intraday, this is what GEX/dealer-hedging logic should use
    in place of spot-gamma -- same number for practical hedging purposes,
    but solved against the price the option chain is actually quoted off."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    df = math.exp(-r * T)
    return df * _norm_pdf(d1) / (F * sigma * math.sqrt(T))


def _vega76(F: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    df = math.exp(-r * T)
    return df * F * _norm_pdf(d1) * math.sqrt(T)


def implied_vol76(price: float, F: float, K: float, T: float, r: float, option_type: str,
                   initial_guess: float = 0.35, max_iter: int = 50, tol: float = 1e-4):
    """Same Newton-with-bisection-fallback approach as implied_vol(), but
    solving Black-76 against the futures price."""
    if price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None

    df = math.exp(-r * T)
    if option_type == "CE":
        intrinsic = df * max(0.0, F - K)
        upper = df * F
    else:
        intrinsic = df * max(0.0, K - F)
        upper = df * K
    if price < intrinsic - 1e-6 or price > upper + 1e-6:
        return None

    sigma = initial_guess
    for _ in range(max_iter):
        model_price = bs76_price(F, K, T, r, sigma, option_type)
        diff = model_price - price
        if abs(diff) < tol:
            return max(sigma, 1e-4)
        v = _vega76(F, K, T, r, sigma)
        if v < 1e-8:
            break
        sigma -= diff / v
        if sigma <= 0 or sigma > 5:
            break
    else:
        return max(sigma, 1e-4) if 0 < sigma <= 5 else None

    lo, hi = 1e-4, 5.0
    f_lo = bs76_price(F, K, T, r, lo, option_type) - price
    f_hi = bs76_price(F, K, T, r, hi, option_type) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        f_mid = bs76_price(F, K, T, r, mid, option_type) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def bs76_theta_per_day(F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """Black-76 theta per option (per share), per CALENDAR day (calendar,
    not trading, because decay happens every day including weekends --
    T itself is what uses the 252-trading-day convention)."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    sq = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    df = math.exp(-r * T)
    first = -(df * F * _norm_pdf(d1) * sigma) / (2.0 * sq)
    if option_type == "CE":
        th = first + r * df * F * _norm_cdf(d1) - r * df * K * _norm_cdf(d2)
    else:
        th = first - r * df * F * _norm_cdf(-d1) + r * df * K * _norm_cdf(-d2)
    return th / 365.0


def bs76_vega_per_point(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per share for a 1 volatility-point (1%) move in IV, Black-76."""
    return _vega76(F, K, T, r, sigma) * 0.01


def bs_theta_per_day(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """Black-Scholes theta per option (per share), per CALENDAR day.
    Negative for a long option; callers use abs() for "decay size"."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    first = -(S * _norm_pdf(d1) * sigma) / (2.0 * sq)
    if option_type == "CE":
        th = first - r * K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        th = first + r * K * math.exp(-r * T) * _norm_cdf(-d2)
    return th / 365.0


def bs_vega_per_point(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per share for a 1 volatility-point (1%) move in IV."""
    return _vega(S, K, T, r, sigma) * 0.01
