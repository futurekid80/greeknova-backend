import os

path = os.path.expanduser("~/mnt/optionspulse/api/positional_intelligence.py")
with open(path) as f:
    src = f.read()

old_header = '''import time as time_module
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_cache: dict = {}
_cache_time: float = 0.0
_CACHE_TTL = 300  # 5 min during market, 3600 post-market'''

new_header = '''import time as time_module
import threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_cache: dict = {}
_cache_time: float = 0.0
_CACHE_TTL = 300  # 5 min during market, 3600 post-market
_compute_lock = threading.Lock()


def _current_ttl():
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    is_market = now_ist.weekday() < 5 and (9 * 60 + 15) <= (now_ist.hour * 60 + now_ist.minute) <= (15 * 60 + 40)
    return 300 if is_market else 3600'''

assert old_header in src, "header anchor not found"
src = src.replace(old_header, new_header, 1)

old_def = "def get_positional_intelligence(min_consec: int = 0):\n    global _cache, _cache_time"
new_def = "def _get_positional_intelligence_uncached(min_consec: int = 0):\n    global _cache, _cache_time"
assert old_def in src, "def anchor not found"
src = src.replace(old_def, new_def, 1)

wrapper = '''def get_positional_intelligence(min_consec: int = 0):
    """Oct 21 2026 PERF FIX (launch-day incident): the real computation
    below (_get_positional_intelligence_uncached) is expensive and was
    being called with no lock, so once real concurrent launch traffic
    showed up, several requests landing right as the 5-min cache
    expired would all trigger a full recompute in parallel ("thundering
    herd"), fighting each other for DB connections -- traced via
    repeated 15-30s p90/p95/p99 latency spikes on launch day. Now only
    the first caller past an expired cache actually recomputes; anyone
    else queued behind the lock gets served the result that call just
    cached, instead of starting its own redundant recompute."""
    cache_key = str(min_consec)
    ttl = _current_ttl()
    cached = _cache.get(cache_key)
    if cached and (time_module.time() - _cache_time) < ttl:
        return cached
    with _compute_lock:
        cached = _cache.get(cache_key)
        if cached and (time_module.time() - _cache_time) < ttl:
            return cached
        return _get_positional_intelligence_uncached(min_consec)


'''

marker = "def _get_positional_intelligence_uncached(min_consec: int = 0):"
idx = src.index(marker)
src = src[:idx] + wrapper + src[idx:]

with open(os.path.expanduser("~/mnt/optionspulse/api/positional_intelligence_new.py"), "w") as f:
    f.write(src)
print("OK, wrote positional_intelligence_new.py, len", len(src))
