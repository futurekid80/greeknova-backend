import ast

path = "commoditynova/mcx_router.py"
with open(path) as f:
    content = f.read()

old = '''        instruments = get_cached_instruments(supabase)
        if not instruments:
            return {"status": "error", "message": "No instruments cached — run /mcx/seed-now first"}

        candles_cache            = fetch_all_candles(instruments, kite)'''

new = '''        instruments = get_cached_instruments(supabase)
        if not instruments:
            return {"status": "error", "message": "No instruments cached — run /mcx/seed-now first"}

        try:
            from services.kite_auth import get_historical_kite_client
            hist_kite = get_historical_kite_client()
        except Exception:
            hist_kite = kite
        candles_cache            = fetch_all_candles(instruments, hist_kite)'''

assert old in content, "scan-now candle-fetch block not found"
content = content.replace(old, new)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
