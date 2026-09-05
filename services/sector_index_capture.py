"""Daily EOD capture for NSE sectoral indices (Sector Strength feature).

Fetches today's completed daily candle for each of the 18 sectoral
indices directly from Kite (using their instrument tokens — indices
don't need instrument-token lookup by tradingsymbol) and upserts into
public.sector_index_daily_bars. Meant to run once a day after market
close, alongside the existing EOD jobs in main.py.
"""
import time

SECTOR_INDEX_TOKENS = {
    "NIFTY BANK":        260105,
    "NIFTY IT":          259849,
    "NIFTY AUTO":        263433,
    "NIFTY METAL":       263689,
    "NIFTY FMCG":        261897,
    "NIFTY PHARMA":      262409,
    "NIFTY REALTY":      261129,
    "NIFTY ENERGY":      261641,
    "NIFTY MEDIA":       263945,
    "NIFTY PSU BANK":    262921,
    "NIFTY PVT BANK":    271113,
    "NIFTY FIN SERVICE": 257801,
    "NIFTY INFRA":       261385,
    "NIFTY CONSR DURBL": 288777,
    "NIFTY HEALTHCARE":  288521,
    "NIFTY OIL AND GAS": 289033,
    "NIFTY CHEMICALS":   420105,
    "NIFTY 50":          256265,
}


def append_todays_sector_index_bar(supabase, kite):
    """Appends just today's now-completed daily bar for every sectoral
    index. Meant to run once a day after market close so
    sector_index_daily_bars stays current going forward."""
    today = time.strftime("%Y-%m-%d")

    rows = []
    for symbol, token in SECTOR_INDEX_TOKENS.items():
        try:
            candles = kite.historical_data(
                instrument_token=token, from_date=today, to_date=today,
                interval="day", continuous=False, oi=False,
            )
            if candles:
                c = candles[-1]
                rows.append({
                    "index_symbol": symbol,
                    "trade_date": str(c["date"])[:10],
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                })
            time.sleep(0.05)
        except Exception as e:
            print(f"[SECTOR_IDX] EOD append {symbol} failed: {e}")

    if rows:
        supabase.table("sector_index_daily_bars")\
            .upsert(rows, on_conflict="index_symbol,trade_date").execute()
    print(f"[SECTOR_IDX] EOD append — {len(rows)} sector index bars added for {today}")
    return {"status": "complete", "bars_added": len(rows), "date": today}
