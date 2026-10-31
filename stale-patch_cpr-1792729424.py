import re

path = "api/cpr.py"
with open(path, "r") as f:
    content = f.read()
orig_len = len(content)

# ---------- Fix 1: compute_and_store_cpr() daily OHLC source ----------
old_daily = '''    supabase = get_supabase()
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        print(f"[CPR] Kite auth failed: {e}")
        return {"error": str(e)}

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        from utils.market_calendar import is_trading_day
        next_day = _get_next_trading_day(today)
        # Skip holidays
        while not is_trading_day(next_day):
            next_day = _get_next_trading_day(next_day)
        trade_date = next_day.isoformat()

    print(f"[CPR] Computing for trade_date: {trade_date}")'''

new_daily = '''    supabase = get_supabase()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        from utils.market_calendar import is_trading_day
        next_day = _get_next_trading_day(today)
        # Skip holidays
        while not is_trading_day(next_day):
            next_day = _get_next_trading_day(next_day)
        trade_date = next_day.isoformat()

    print(f"[CPR] Computing for trade_date: {trade_date}")'''

assert old_daily in content, "old_daily block not found"
content = content.replace(old_daily, new_daily, 1)

old_ohlc_loop = '''    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

    INDEX_TOKENS = {
        "NIFTY":     256265,
        "BANKNIFTY": 260105,
        "FINNIFTY":  257801,
        "MIDCPNIFTY":288009,
        "SENSEX":    265,
    }
    token_map: dict = {**INDEX_TOKENS}
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            if inst["tradingsymbol"] in STOCKS:
                token_map[inst["tradingsymbol"]] = inst["instrument_token"]
        print(f"[CPR] Got {len(token_map)} instrument tokens")
    except Exception as e:
        print(f"[CPR] Instruments fetch failed: {e}")

    from_date = (today - timedelta(days=10)).isoformat()
    to_date   = today.isoformat()

    for sym in all_symbols:
        token = token_map.get(sym)
        if not token:
            continue
        try:
            candles = kite.historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval="day",
                continuous=False,
                oi=False,
            )
            if candles:
                # Always use previous trading day's candle — never today's
                matched = None
                for c in reversed(candles):
                    c_date = str(c["date"])[:10]
                    if c_date == prev_str:
                        matched = c
                        break
                # Fallback: use second-to-last if prev_str not found
                if not matched and len(candles) >= 2:
                    matched = candles[-2]
                    print(f"[CPR] {sym}: no match for {prev_str}, using {str(candles[-2]['date'])[:10]}")
                elif not matched:
                    matched = candles[-1]

                ohlc_map[sym] = {
                    "high":  float(matched["high"]),
                    "low":   float(matched["low"]),
                    "close": float(matched["close"]),
                }
            time.sleep(0.05)
        except Exception as e:
            print(f"[CPR] historical_data {sym}: {e}")

    print(f"[CPR] Got OHLC for {len(ohlc_map)} symbols")'''

new_ohlc_loop = '''    all_symbols = INDICES + STOCKS
    ohlc_map: dict = {}

    # BUG FIX (Oct 23 2026): this used to pull previous-day OHLC via
    # kite.historical_data(), which requires Zerodha's separate paid
    # Historical Data add-on. Manish's Kite Connect app is a personal/
    # multiuser API, not the public Connect API, so that call has never
    # had real access -- it worked by coincidence while a cached/trial
    # grant lasted, then started failing every symbol with "Insufficient
    # permission for that call" from 2026-10-22 onward. The job swallowed
    # the per-symbol errors and silently stored ZERO rows for two days
    # straight, so the CPR Scanner kept serving 2026-10-21's stale numbers
    # under every later trade_date with no indication anything was wrong
    # (e.g. OIL kept showing as "Extremely Narrow" days after that was
    # true). Fixed by deriving prev-day high/low/close ourselves from
    # cmp_prices, which is populated independently every ~5 min during
    # market hours via kite.quote() (a call this API key DOES have access
    # to) -- no Historical Data permission needed at all.
    day_start = f"{prev_str}T00:00:00+00:00"
    day_end   = (prev_trading_day + timedelta(days=1)).isoformat() + "T00:00:00+00:00"

    for sym in all_symbols:
        try:
            rows = supabase.from_("cmp_prices")\\
                .select("cmp, timestamp")\\
                .eq("symbol", sym)\\
                .gte("timestamp", day_start)\\
                .lt("timestamp", day_end)\\
                .order("timestamp")\\
                .limit(2000)\\
                .execute()
            cmps = [float(r["cmp"]) for r in (rows.data or []) if r.get("cmp")]
            if cmps:
                ohlc_map[sym] = {
                    "high":  max(cmps),
                    "low":   min(cmps),
                    "close": cmps[-1],  # last tick of the day (rows ordered ascending)
                }
        except Exception as e:
            print(f"[CPR] cmp_prices fetch {sym}: {e}")

    print(f"[CPR] Got OHLC for {len(ohlc_map)} symbols (source: cmp_prices for {prev_str})")'''

assert old_ohlc_loop in content, "old_ohlc_loop block not found"
content = content.replace(old_ohlc_loop, new_ohlc_loop, 1)

# ---------- Fix 2: compute_and_store_weekly_monthly_cpr() weekly/monthly OHLC source ----------
old_weekly_head = '''    supabase = get_supabase()
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        return {"error": str(e)}

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        from utils.market_calendar import is_trading_day
        next_day = _get_next_trading_day(today)
        while not is_trading_day(next_day):
            next_day = _get_next_trading_day(next_day)
        trade_date = next_day.isoformat()

    INDEX_TOKENS = {
        "NIFTY":     256265,
        "BANKNIFTY": 260105,
        "FINNIFTY":  257801,
        "MIDCPNIFTY":288009,
        "SENSEX":    265,
    }
    token_map = {**INDEX_TOKENS}
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            if inst["tradingsymbol"] in STOCKS:
                token_map[inst["tradingsymbol"]] = inst["instrument_token"]
    except Exception as e:
        print(f"[CPR Weekly] Instruments fetch failed: {e}")

    all_symbols = INDICES + STOCKS'''

new_weekly_head = '''    supabase = get_supabase()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if not trade_date:
        from utils.market_calendar import is_trading_day
        next_day = _get_next_trading_day(today)
        while not is_trading_day(next_day):
            next_day = _get_next_trading_day(next_day)
        trade_date = next_day.isoformat()

    all_symbols = INDICES + STOCKS'''

assert old_weekly_head in content, "old_weekly_head block not found"
content = content.replace(old_weekly_head, new_weekly_head, 1)

old_weekly_loop = '''    from_date = prev_month_start.isoformat()
    to_date   = today.isoformat()

    print(f"[CPR Weekly] Week range: {week_start} to {week_end}")
    print(f"[CPR Weekly] Month range: {prev_month_start} to {prev_month_end}")

    weekly_ohlc  = {}
    monthly_ohlc = {}

    for sym in all_symbols:
        token = token_map.get(sym)
        if not token:
            continue
        try:
            candles = kite.historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval="day",
                continuous=False,
                oi=False,
            )
            if not candles:
                continue

            week_candles = [
                c for c in candles
                if week_start.isoformat() <= str(c["date"])[:10] <= week_end.isoformat()
            ]
            if week_candles:
                weekly_ohlc[sym] = {
                    "high":  max(float(c["high"]) for c in week_candles),
                    "low":   min(float(c["low"]) for c in week_candles),
                    "close": float(week_candles[-1]["close"]),
                }

            month_candles = [
                c for c in candles
                if prev_month_start.isoformat() <= str(c["date"])[:10] <= prev_month_end.isoformat()
            ]
            if month_candles:
                monthly_ohlc[sym] = {
                    "high":  max(float(c["high"]) for c in month_candles),
                    "low":   min(float(c["low"]) for c in month_candles),
                    "close": float(month_candles[-1]["close"]),
                }

            time.sleep(0.05)
        except Exception as e:
            print(f"[CPR Weekly] {sym}: {e}")

    print(f"[CPR Weekly] Got weekly OHLC: {len(weekly_ohlc)}, monthly: {len(monthly_ohlc)}")'''

new_weekly_loop = '''    print(f"[CPR Weekly] Week range: {week_start} to {week_end}")
    print(f"[CPR Weekly] Month range: {prev_month_start} to {prev_month_end}")

    weekly_ohlc  = {}
    monthly_ohlc = {}

    # BUG FIX (Oct 23 2026): same root cause as compute_and_store_cpr() --
    # kite.historical_data() has no real permission on this API key, so
    # this derives weekly/monthly OHLC from cmp_prices ticks instead.
    week_start_ts  = f"{week_start.isoformat()}T00:00:00+00:00"
    week_end_ts    = (week_end + timedelta(days=1)).isoformat() + "T00:00:00+00:00"
    month_start_ts = f"{prev_month_start.isoformat()}T00:00:00+00:00"
    month_end_ts   = (prev_month_end + timedelta(days=1)).isoformat() + "T00:00:00+00:00"

    for sym in all_symbols:
        try:
            week_rows = supabase.from_("cmp_prices")\\
                .select("cmp, timestamp")\\
                .eq("symbol", sym)\\
                .gte("timestamp", week_start_ts)\\
                .lt("timestamp", week_end_ts)\\
                .order("timestamp")\\
                .limit(5000)\\
                .execute()
            week_cmps = [float(r["cmp"]) for r in (week_rows.data or []) if r.get("cmp")]
            if week_cmps:
                weekly_ohlc[sym] = {
                    "high":  max(week_cmps),
                    "low":   min(week_cmps),
                    "close": week_cmps[-1],
                }

            month_rows = supabase.from_("cmp_prices")\\
                .select("cmp, timestamp")\\
                .eq("symbol", sym)\\
                .gte("timestamp", month_start_ts)\\
                .lt("timestamp", month_end_ts)\\
                .order("timestamp")\\
                .limit(20000)\\
                .execute()
            month_cmps = [float(r["cmp"]) for r in (month_rows.data or []) if r.get("cmp")]
            if month_cmps:
                monthly_ohlc[sym] = {
                    "high":  max(month_cmps),
                    "low":   min(month_cmps),
                    "close": month_cmps[-1],
                }
        except Exception as e:
            print(f"[CPR Weekly] {sym}: {e}")

    print(f"[CPR Weekly] Got weekly OHLC: {len(weekly_ohlc)}, monthly: {len(monthly_ohlc)} (source: cmp_prices)")'''

assert old_weekly_loop in content, "old_weekly_loop block not found"
content = content.replace(old_weekly_loop, new_weekly_loop, 1)

assert len(content) != orig_len
with open("api/cpr_new.py", "w") as f:
    f.write(content)

import ast
ast.parse(content)
print("OK - patched and syntax-valid, written to api/cpr_new.py")
