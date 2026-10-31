import ast

path = "api/vix_backfill.py"
with open(path) as f:
    content = f.read()

old = '''    from services.kite_auth import get_kite_client
    kite = get_kite_client()
    supabase = get_supabase()

    end = datetime.now()
    start = end - timedelta(days=days_back)
    result = {"daily_rows": 0, "errors": []}
    try:
        candles = kite.historical_data(
            INDIA_VIX_TOKEN,
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
            "day",
        )'''

new = '''    from services.kite_auth import get_kite_client
    try:
        from services.kite_auth import get_historical_kite_client
        kite = get_historical_kite_client()
        print("[VIX] Using Trading Bot historical client for daily close")
    except Exception as e:
        print(f"[VIX] Historical client unavailable ({e}) -- falling back to main session")
        kite = get_kite_client()
    supabase = get_supabase()

    end = datetime.now()
    start = end - timedelta(days=days_back)
    result = {"daily_rows": 0, "errors": []}
    try:
        candles = kite.historical_data(
            INDIA_VIX_TOKEN,
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
            "day",
        )'''

assert old in content, "append_vix_daily_close block not found"
content = content.replace(old, new)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
