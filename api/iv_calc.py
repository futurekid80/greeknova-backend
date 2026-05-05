import math
from datetime import date

def black_scholes_iv(option_price: float, S: float, K: float, T: float, r: float = 0.065, option_type: str = 'CE') -> float:
    """Calculate IV using Newton-Raphson method"""
    if T <= 0 or option_price <= 0 or S <= 0 or K <= 0:
        return 0.0
    
    def bs_price(sigma):
        if sigma <= 0:
            return 0
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        d2 = d1 - sigma*math.sqrt(T)
        nd1 = norm_cdf(d1)
        nd2 = norm_cdf(d2)
        if option_type == 'CE':
            return S*nd1 - K*math.exp(-r*T)*nd2
        else:
            return K*math.exp(-r*T)*norm_cdf(-d2) - S*norm_cdf(-d1)
    
    def vega(sigma):
        if sigma <= 0:
            return 0
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        return S*math.sqrt(T)*norm_pdf(d1)
    
    # Newton-Raphson
    sigma = 0.3  # initial guess
    for _ in range(100):
        price = bs_price(sigma)
        v = vega(sigma)
        if v < 1e-10:
            break
        diff = option_price - price
        sigma = sigma + diff/v
        if abs(diff) < 0.0001:
            break
        if sigma <= 0:
            sigma = 0.001
    
    return round(sigma * 100, 2) if 0 < sigma < 5 else 0.0

def norm_cdf(x):
    return 0.5*(1 + math.erf(x/math.sqrt(2)))

def norm_pdf(x):
    return math.exp(-0.5*x**2)/math.sqrt(2*math.pi)

def add_iv_to_strikes(strikes: list, cmp: float, expiry_str: str) -> list:
    """Add IV to each strike row"""
    try:
        exp_date = date.fromisoformat(expiry_str[:10])
        T = max((exp_date - date.today()).days, 1) / 365.0
    except:
        T = 30/365.0
    
    for strike in strikes:
        S = cmp
        K = strike['strike']
        
        if strike['ce_ltp'] > 0:
            strike['ce_iv'] = black_scholes_iv(strike['ce_ltp'], S, K, T, option_type='CE')
        else:
            strike['ce_iv'] = 0
            
        if strike['pe_ltp'] > 0:
            strike['pe_iv'] = black_scholes_iv(strike['pe_ltp'], S, K, T, option_type='PE')
        else:
            strike['pe_iv'] = 0
        
        # ATM IV average
        if strike.get('is_atm') and strike['ce_iv'] > 0 and strike['pe_iv'] > 0:
            strike['atm_iv'] = round((strike['ce_iv'] + strike['pe_iv']) / 2, 2)
    
    return strikes
