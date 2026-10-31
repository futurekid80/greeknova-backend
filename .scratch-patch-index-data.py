import re

path = "/root/mnt/optionspulse/main.py"
with open(path) as f:
    content = f.read()

old = '''@app.get("/index-data")
def index_data():
    import time
    cache = getattr(index_data, '_cache', None)
    cache_time = getattr(index_data, '_cache_time', 0)
    if cache and (time.time() - cache_time) < 60:
        return cache

    supabase = get_supabase()'''

new = '''@app.get("/index-data")
def index_data():
    import time
    # FIX (Oct 24 2026): "page loading is repetitive" -- this endpoint does
    # 2-3 sequential Supabase round trips (NIFTY FUT timestamp lookup,
    # SENSEX-completeness check, then the batch OI+cmp fetch), measured at
    # Railway p50 ~7-8s and spiking to 29s under load. The 60s cache was
    # never wrong, but it's too short to matter once market hours end --
    # outside trading hours the underlying oi_snapshots data literally
    # cannot change (capture only runs during market hours), so every
    # request past the first one in a given evening/weekend was paying the
    # full multi-second cost for an answer that was already sitting in
    # cache, unchanged, for hours. Extend the TTL to 10 minutes whenever
    # it's not a live trading session.
    from utils.market_calendar import is_trading_day, today_ist
    import pytz
    _ist_now = datetime.now(pytz.timezone("Asia/Kolkata"))
    _market_hours_now = is_trading_day(today_ist()) and (9, 15) <= (_ist_now.hour, _ist_now.minute) <= (15, 30)
    cache_ttl = 60 if _market_hours_now else 600
    cache = getattr(index_data, '_cache', None)
    cache_time = getattr(index_data, '_cache_time', 0)
    if cache and (time.time() - cache_time) < cache_ttl:
        return cache

    supabase = get_supabase()'''

assert old in content, "old block not found"
content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched")
