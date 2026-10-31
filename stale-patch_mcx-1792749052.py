import ast

path = "commoditynova/mcx_scheduler.py"
with open(path) as f:
    content = f.read()

old = '''    try:
        kite = get_kite_client()
        instruments = get_cached_instruments(supabase)
        if not instruments:
            logger.warning("MCX scan skipped — no instruments cached yet")
            return
        fresh_candles = fetch_all_candles(instruments, kite)
        candles_cache.update(fresh_candles)'''

new = '''    try:
        kite = get_kite_client()
        instruments = get_cached_instruments(supabase)
        if not instruments:
            logger.warning("MCX scan skipped — no instruments cached yet")
            return
        # FIX (Oct 23 2026): kite.historical_data() has no real permission on
        # the main Production app key (same root cause as the CPR/Wolves/VIX
        # fixes earlier today). Use the dedicated Trading Bot historical
        # client for the candle fetch specifically; everything else in this
        # job (instruments/quotes/ignition scan) keeps using the main `kite`.
        try:
            from services.kite_auth import get_historical_kite_client
            hist_kite = get_historical_kite_client()
        except Exception as e:
            logger.warning(f"MCX historical client unavailable ({e}) -- falling back to main session")
            hist_kite = kite
        fresh_candles = fetch_all_candles(instruments, hist_kite)
        candles_cache.update(fresh_candles)'''

assert old in content, "run_scan_job candle-fetch block not found"
content = content.replace(old, new)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
