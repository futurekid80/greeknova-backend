import requests
import time
from datetime import datetime

# --- 1. CPR CALCULATOR ---
def get_cpr_levels():
    url = "https://public.coindcx.com/market_data/candlesticks"
    # Get daily candles for BTC/INR (B-BTC_INR)
    params = {"pair": "B-BTC_INR", "interval": "1d", "limit": 2}
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        # Yesterday's candle is the second to last one
        yesterday = data['data'][0]
        h, l, c = float(yesterday['high']), float(yesterday['low']), float(yesterday['close'])
        
        pivot = (h + l + c) / 3
        bc = (h + l) / 2
        tc = (pivot - bc) + pivot
        
        # Sort TC/BC in case they are flipped (Narrow vs Wide CPR)
        cpr_top = max(tc, bc)
        cpr_bottom = min(tc, bc)
        
        return {"P": pivot, "TC": cpr_top, "BC": cpr_bottom}
    except Exception as e:
        print(f"Error calculating CPR: {e}")
        return None

# --- 2. LIVE MONITOR ---
last_trade_id = 0
levels = get_cpr_levels()

def scan_market():
    global last_trade_id, levels
    url = "https://public.coindcx.com/market_data/trade_history"
    params = {"pair": "B-BTC_INR", "limit": 50}
    
    try:
        response = requests.get(url, params=params)
        trades = response.json()
        trades.reverse()

        THRESHOLD = 0.01 # 0.01 BTC
        
        for trade in trades:
            if trade['tid'] <= last_trade_id:
                continue
            
            last_trade_id = trade['tid']
            q, p = float(trade['q']), float(trade['p'])
            is_maker = trade['m']

            if q >= THRESHOLD:
                timestamp = datetime.now().strftime("%H:%M:%S")
                # Detect proximity to CPR
                zone = ""
                if levels['BC'] <= p <= levels['TC']:
                    zone = "🎯 INSIDE CPR"
                elif p > levels['TC']:
                    zone = "📈 ABOVE CPR"
                else:
                    zone = "📉 BELOW CPR"

                type_label = "MAKER" if is_maker else "TAKER (Aggressive)"
                color = "🟢" if not is_maker else "⚪"
                
                print(f"[{timestamp}] {color} {type_label} | Vol: {q:.4f} | ₹{p:,.0f} | {zone}")

    except Exception as e:
        print(f"Error: {e}")

if levels:
    print(f"--- ⚡️ CPR LOADED ⚡️ ---")
    print(f"TC: {levels['TC']:,.0f} | P: {levels['P']:,.0f} | BC: {levels['BC']:,.0f}")
    print("Monitoring for Footprints... (Ctrl+C to stop)\n")

    while True:
        scan_market()
        time.sleep(5)
