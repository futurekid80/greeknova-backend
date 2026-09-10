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
