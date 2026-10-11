"""
services/technical_levels.py

Shared per-SYMBOL (not per-strike) technical levels: 9/21 EMA cross state,
plus previous-day and previous-week high/low. Two consumers:
  1. "Confirmation" tags next to OI signals on Jungle/UOA (api/options_jungle.py,
     api/uoa.py) -- e.g. "Put Writing" + price above PDH.
  2. The opt-in EMA/PDH/PDL/PWH/PWL push alerts (services/push_checker.py).

All computed off spot_daily_bars (daily OHLCV, already backfilled and kept
current for the 52-week-high and spot-volume-scanner features -- reused
here rather than a new data source, same reasoning as week52_high.py).
"""
import time as _time
import threading
from datetime import date as date_type, timedelta

EMA_FAST = 9
EMA_SLOW = 21

_cache = None
_cache_time = 0
CACHE_TTL = 300  # 5 min -- daily bars don't change intraday, no need to
                 # recompute on every page/check
_refreshing = False
_refresh_lock = threading.Lock()


def _ema(period: int, closes: list):
    """Standard EMA: seed with the SMA of the first `period` closes, then
    roll forward with the usual multiplier. Returns the value at the LAST
    close, or None if there isn't enough history yet."""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for v in closes[period:]:
        e = v * k + e * (1 - k)
    return e


def _week_key(d: date_type):
    y, w, _ = d.isocalendar()
    return (y, w)


def _fetch_and_compute(supabase) -> dict:
    """The actual computation -- ~15 sequential paginated Supabase calls
    (14k+ rows across every symbol's last ~90 days of daily bars). Several
    seconds even when Supabase is healthy. BLOCKING -- only call this
    directly from a background job (push_checker.py's scheduler tick, via
    refresh_levels_sync below), never from a request handler. Updates the
    shared module-level cache on success.
    """
    global _cache, _cache_time
    today = date_type.today()
    cutoff = (today - timedelta(days=60)).isoformat()  # ~40 trading days --
                                                         # a decent EMA21
                                                         # convergence margin,
                                                         # while keeping this
                                                         # ~10 pages instead
                                                         # of the ~15 a full
                                                         # 90-day window needed

    rows = []
    offset = 0
    PAGE = 1000
    while True:
        batch = supabase.from_("spot_daily_bars") \
            .select("symbol,trade_date,high,low,close") \
            .gte("trade_date", cutoff) \
            .order("trade_date", desc=False) \
            .range(offset, offset + PAGE - 1).execute()
        if not batch.data:
            break
        rows.extend(batch.data)
        if len(batch.data) < PAGE:
            break
        offset += PAGE

    by_symbol: dict = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    today_str = today.isoformat()
    cur_week = _week_key(today)

    result = {}
    for sym, bars in by_symbol.items():
        bars.sort(key=lambda r: r["trade_date"])
        closes = [float(b["close"]) for b in bars if b.get("close") is not None]

        ema9 = _ema(EMA_FAST, closes)
        ema21 = _ema(EMA_SLOW, closes)
        ema_bullish = (ema9 > ema21) if (ema9 is not None and ema21 is not None) else None

        completed = [b for b in bars if b["trade_date"] < today_str]
        pdh = float(completed[-1]["high"]) if completed else None
        pdl = float(completed[-1]["low"]) if completed else None

        week_keys = sorted({_week_key(date_type.fromisoformat(b["trade_date"])) for b in bars})
        prev_weeks = [wk for wk in week_keys if wk < cur_week]
        prev_week = prev_weeks[-1] if prev_weeks else None
        prev_week_bars = [
            b for b in bars
            if prev_week and _week_key(date_type.fromisoformat(b["trade_date"])) == prev_week
        ]
        pwh = max((float(b["high"]) for b in prev_week_bars), default=None)
        pwl = min((float(b["low"]) for b in prev_week_bars), default=None)

        result[sym] = {
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema_bullish,
            "pdh": pdh, "pdl": pdl, "pwh": pwh, "pwl": pwl,
        }

    _cache = result
    _cache_time = _time.time()
    return result


def refresh_levels_sync(supabase) -> dict:
    """Blocking refresh. Safe to call from a background job (push_checker's
    5-min scheduler tick) where taking several seconds is fine -- this is
    what keeps the cache warm so request-path callers (below) almost never
    hit a cold cache during market hours."""
    return _fetch_and_compute(supabase)


def compute_levels(supabase, force: bool = False) -> dict:
    """Request-path safe: NEVER blocks the caller on the expensive fetch.
    Returns whatever's in the cache right now (possibly stale, possibly
    empty on a cold start) immediately, and -- if the cache is stale or
    missing -- kicks off a background thread to refresh it for next time
    (single-flight, so concurrent requests don't all spawn their own
    refresh). Jungle/UOA's confirmation tags simply don't appear on the
    first request or two after a deploy/cold start rather than the whole
    page hanging for the ~10 sequential Supabase pages this needs.
    """
    global _refreshing
    now = _time.time()
    is_stale = _cache is None or (now - _cache_time) >= CACHE_TTL

    if force or is_stale:
        with _refresh_lock:
            start_refresh = not _refreshing
            if start_refresh:
                _refreshing = True

        if start_refresh:
            def _bg_refresh():
                global _refreshing
                try:
                    _fetch_and_compute(supabase)
                except Exception as e:
                    print(f"[TechLevels] Background refresh failed: {e}")
                finally:
                    with _refresh_lock:
                        _refreshing = False
            threading.Thread(target=_bg_refresh, daemon=True).start()

    return _cache if _cache is not None else {}


# ── Confirmation tag for an existing OI/UOA signal row ──────────────────────
# Which signal_type/interpretation values read as bullish vs bearish -- matches
# the color conventions already used on the Jungle/UOA frontends (emerald =
# bullish-leaning, red/orange = bearish-leaning).
BULLISH_SIGNALS = {"LONG_BUILDUP", "PUT_WRITING", "SHORT_COVERING", "BUYER_DOMINATED"}
BEARISH_SIGNALS = {"SHORT_BUILDUP", "CALL_WRITING", "LONG_UNWINDING", "SELLER_DOMINATED"}


def _confirmation(bullish: bool, bearish: bool, cmp: float, lv: dict):
    if not lv or not cmp or (not bullish and not bearish):
        return None

    pdh, pdl, pwh, pwl = lv.get("pdh"), lv.get("pdl"), lv.get("pwh"), lv.get("pwl")
    ema_bullish = lv.get("ema_bullish")

    if bullish:
        if pdh and cmp > pdh:
            return "Above PDH"
        if ema_bullish:
            return "9/21 Bullish Cross"
        if pwh and cmp > pwh:
            return "Above PWH"
    else:
        if pdl and cmp < pdl:
            return "Below PDL"
        if ema_bullish is False:
            return "9/21 Bearish Cross"
        if pwl and cmp < pwl:
            return "Below PWL"
    return None


def confirmation_for(symbol: str, signal_type: str, cmp: float, levels: dict):
    """Returns a short confirmation string (e.g. "Above PDH", "9/21 Bullish
    Cross") when price action agrees with the direction implied by
    `signal_type` (Jungle's vocabulary: LONG_BUILDUP, PUT_WRITING, etc.), or
    None when it doesn't -- deliberately silent rather than an explicit "not
    confirmed" tag, so it doesn't add noise to rows that haven't confirmed yet."""
    lv = levels.get(symbol)
    return _confirmation(signal_type in BULLISH_SIGNALS, signal_type in BEARISH_SIGNALS, cmp, lv)


def confirmation_for_bias(symbol: str, bias: str, cmp: float, levels: dict):
    """Same as confirmation_for, but for callers (UOA) that already compute
    an explicit BULLISH/BEARISH bias per row instead of a fixed signal-type
    vocabulary."""
    lv = levels.get(symbol)
    return _confirmation(bias == "BULLISH", bias == "BEARISH", cmp, lv)
