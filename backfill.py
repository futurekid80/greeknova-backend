import os, sys, time, requests
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
from datetime import date, timedelta

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY')
from supabase import create_client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

NSE_HOLIDAYS = {date(2026,5,1), date(2026,5,27), date(2026,6,26)}

def get_trading_dates(start, end):
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in NSE_HOLIDAYS:
            days.append(d)
        d += timedelta(days=1)
    return days

TRADING_DATES = get_trading_dates(date(2026,5,12), date(2026,6,25))
SYMBOLS = set(["RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN","SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH","TATACONSUM","TATASTEEL","ADANIENT","POWERGRID","NTPC","ONGC","JSWSTEEL","COALINDIA","BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA","DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE","INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT","ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS","HAL","INDIGO","PFC","RECLTD","SAIL","TATAPOWER","VEDL","PAYTM","NYKAA","PERSISTENT","DIXON","BSE","MCX","TMPV","GODREJPROP","DIVISLAB","COFORGE","ANGELONE","CDSL","OIL","TVSMOTOR","BHARATFORG","MOTHERSON","LUPIN","TORNTPHARM","AUROPHARMA","GODREJCP","MARICO","DABUR","PIDILITIND","MUTHOOTFIN","SBICARD","ICICIPRULI","IDFCFIRSTB","FEDERALBNK","ETERNAL","POLYCAB","VOLTAS","IEX","ASTRAL"])

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
session = requests.Session()
try:
    session.get("https://www.nseindia.com", headers=headers, timeout=8)
    time.sleep(1)
except: pass

print("\n=== Delivery data backfill ===")
existing = supabase.from_("delivery_data").select("trade_date").limit(500).execute()
existing_dates = {r["trade_date"] for r in (existing.data or [])}
print(f"Already have: {len(existing_dates)} dates")

saved = 0
failed = 0

for d in TRADING_DATES:
    ds = d.isoformat()
    if ds in existing_dates:
        print(f"  skip {ds}")
        continue

    # Format: DDMMYYYY
    date_str = d.strftime('%d%m%Y')
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

    try:
        res = session.get(url, headers=headers, timeout=12)
        if res.status_code != 200:
            print(f"  ✗ {ds} status={res.status_code}")
            failed += 1
            continue

        lines = res.text.strip().split('\n')
        # Header: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
        records = []
        for line in lines[1:]:
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 15: continue
            sym = parts[0].strip().upper()
            series = parts[1].strip()
            if sym not in SYMBOLS: continue
            if series not in ('EQ', 'BE', 'BZ'): continue
            try:
                traded = int(float(parts[10].replace(',','') or 0))
                deliv = int(float(parts[13].replace(',','') or 0))
                pct = float(parts[14].replace(',','') or 0)
                records.append({
                    "trade_date": ds,
                    "symbol": sym,
                    "traded_qty": traded,
                    "deliverable_qty": deliv,
                    "delivery_pct": pct,
                })
            except: continue

        if records:
            for i in range(0, len(records), 100):
                supabase.from_("delivery_data").upsert(records[i:i+100]).execute()
            print(f"  ✅ {ds} — {len(records)} stocks saved")
            saved += 1
        else:
            print(f"  ⚠️ {ds} — no matching symbols")
            failed += 1

    except Exception as e:
        print(f"  ✗ {ds} — {e}")
        failed += 1

    time.sleep(0.4)

print(f"\nDone: {saved} dates saved, {failed} failed")
print("✅ All done!")
