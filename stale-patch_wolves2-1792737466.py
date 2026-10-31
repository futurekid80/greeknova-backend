path = "services/wolves_scanner.py"
with open(path, "r") as f:
    content = f.read()
orig = content

# ---------- refresh_tf_cache(): try the dedicated historical client first, cmp_prices as fallback ----------
old_rtc_setup = '''    supabase = get_supabase()
    token_map = _build_token_map(kite, universe)

    today_ist = datetime.now(IST).date()
    daily_from = (today_ist - timedelta(days=2000)).isoformat()  # ~8yr -- gives the monthly resample a real percentile population
    to_date = today_ist.isoformat()

    rows, cached, skipped, errored = [], 0, 0, 0

    for symbol in universe:
        token = token_map.get(symbol)
        if not token:
            skipped += 1
            continue
        daily_candles = _cmp_prices_candles(supabase, symbol, daily_from, to_date, "D")
        if not daily_candles:
            skipped += 1
            continue'''

new_rtc_setup = '''    supabase = get_supabase()
    token_map = _build_token_map(kite, universe)

    today_ist = datetime.now(IST).date()
    daily_from = (today_ist - timedelta(days=2000)).isoformat()  # ~8yr -- gives the monthly resample a real percentile population
    to_date = today_ist.isoformat()

    # BUG FIX (Oct 23 2026) cont'd: when Manish's dedicated historical-only
    # Kite app ("Trading Bot", separate from the main login) is configured
    # and its Historical Data subscription is active, prefer it for the
    # full years-deep daily fetch -- this is what actually makes
    # Weekly/Monthly (Apex Wolf) trustworthy again, since cmp_prices alone
    # only has ~6 months of history. Falls back to cmp_prices reconstruction
    # per-symbol if the historical client is unavailable or a call fails,
    # so this can never take the whole job down.
    historical_kite = None
    try:
        from services.kite_auth import get_historical_kite_client
        historical_kite = get_historical_kite_client()
        print("[Wolves] Using Trading Bot historical client for full-depth daily candles")
    except Exception as e:
        print(f"[Wolves] Historical client unavailable ({e}) -- using cmp_prices reconstruction instead")

    rows, cached, skipped, errored = [], 0, 0, 0

    for symbol in universe:
        token = token_map.get(symbol)
        if not token:
            skipped += 1
            continue

        daily_candles = None
        if historical_kite:
            try:
                daily_candles = historical_kite.historical_data(instrument_token=token, from_date=daily_from, to_date=to_date, interval="day", continuous=False, oi=False)
                time.sleep(0.34)
            except Exception as e:
                print(f"[Wolves] {symbol}: historical_data (Trading Bot) failed -- {e}")
                daily_candles = None
        if not daily_candles:
            daily_candles = _cmp_prices_candles(supabase, symbol, daily_from, to_date, "D")
        if not daily_candles:
            skipped += 1
            continue'''

assert old_rtc_setup in content, "old_rtc_setup not found"
content = content.replace(old_rtc_setup, new_rtc_setup, 1)

# ---------- run_live_tick(): same preference order for the hourly fetch ----------
old_rlt_setup = '''    tf_cache_rows = supabase.from_("wolves_tf_cache").select("*").in_("symbol", universe).execute().data or []
    tf_cache = {r["symbol"]: r for r in tf_cache_rows}'''

new_rlt_setup = '''    tf_cache_rows = supabase.from_("wolves_tf_cache").select("*").in_("symbol", universe).execute().data or []
    tf_cache = {r["symbol"]: r for r in tf_cache_rows}

    historical_kite = None
    try:
        from services.kite_auth import get_historical_kite_client
        historical_kite = get_historical_kite_client()
    except Exception as e:
        print(f"[Wolves] Historical client unavailable for live tick ({e}) -- using cmp_prices reconstruction")'''

assert old_rlt_setup in content, "old_rlt_setup not found"
content = content.replace(old_rlt_setup, new_rlt_setup, 1)

old_rlt_fetch = '''        hourly_candles = _cmp_prices_candles(supabase, symbol, hourly_from, to_date, "h")
        if not hourly_candles:
            skipped += 1
            continue

        hourly_df = _candles_to_df(hourly_candles)'''

new_rlt_fetch = '''        hourly_candles = None
        if historical_kite:
            try:
                hourly_candles = historical_kite.historical_data(instrument_token=token, from_date=hourly_from, to_date=to_date, interval="60minute", continuous=False, oi=False)
                time.sleep(0.34)
            except Exception as e:
                print(f"[Wolves] {symbol}: hourly historical_data (Trading Bot) failed -- {e}")
                hourly_candles = None
        if not hourly_candles:
            hourly_candles = _cmp_prices_candles(supabase, symbol, hourly_from, to_date, "h")
        if not hourly_candles:
            skipped += 1
            continue

        hourly_df = _candles_to_df(hourly_candles)'''

assert old_rlt_fetch in content, "old_rlt_fetch not found"
content = content.replace(old_rlt_fetch, new_rlt_fetch, 1)

assert content != orig
with open("services/wolves_scanner_new2.py", "w") as f:
    f.write(content)

import ast
ast.parse(content)
print("OK - patched and syntax-valid")
