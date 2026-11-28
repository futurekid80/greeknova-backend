"""
services/stealth_greeks.py  (Nov 28 2026)  PREVIEW ONLY -- nothing here changes the live Strike Stealth scan.

Three extra reads for each saved Strike Stealth hit, built from the option's own Greeks:

1. SIZE   -- the position the added OI amounts to if held in the underlying: OI added x |delta| x spot, in Rs,
             and what that is as a share of the stock's average daily traded value ("Large / Very large for this stock").
2. COST   -- is the stock's implied volatility cheap or rich against its OWN last ~6 months (ATM IV percentile).
3. MOVE   -- how far the stock must move by expiry for a buyer at today's premium to break even, set against the
             stock's normal move to expiry (1 standard deviation, from its ATM IV).

Everything is a READ of data already stored (saved hits, iv_history, spot_daily_bars). Any failure for a contract
just leaves that field out; it never blocks the rest. Member-facing wording stays in words, no thresholds shown.
"""
import math
import time
from datetime import datetime, timedelta, timezone

from services.stealth_baseline import implied_vol, _ncdf, RISK_FREE

# Tag cut-offs. Tuned Nov 28 2026 on Friday's 589 hits so each tag marks roughly the top 8% / next 15% (size) and the
# easiest ~25% / hardest ~20% (move). Kept private, never shown to members.
IV_LOW_PCT = 25.0            # at or below this percentile = cheap for this stock
IV_HIGH_PCT = 75.0           # at or above = rich for this stock
SIZE_LARGE_PCT = 2.5         # delta-size as % of the stock's average daily traded value
SIZE_VERY_LARGE_PCT = 5.0
MOVE_EASY = 0.35             # move needed / normal move: at or below = inside the normal range
MOVE_STRETCH = 0.6           # above this = needs more than a normal move
MIN_IV_DAYS = 30             # need at least this many days of the stock's IV history
IV_LOOKBACK_DAYS = 185

_CACHE: dict = {}            # (date, window) -> (time, result)
_CACHE_TTL = 600


def _pages(builder, page=1000, max_pages=40):
    out = []
    for i in range(max_pages):
        rows = builder().range(i * page, (i + 1) * page - 1).execute().data or []
        out.extend(rows)
        if len(rows) < page:
            break
    return out


def _years_to_expiry(expiry: str, scan_date: str) -> float:
    """Years from the reference moment to expiry (15:30 IST = 10:00 UTC). A finished day is measured from its close."""
    exp = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").replace(hour=10, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    ref = datetime.strptime(scan_date, "%Y-%m-%d").replace(hour=10, tzinfo=timezone.utc)
    if ref > now:
        ref = now
    return max((exp - ref).total_seconds(), 3600.0) / (365.0 * 86400.0)


def _delta(is_call: bool, S: float, K: float, T: float, sigma: float) -> float:
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (RISK_FREE + 0.5 * sigma * sigma) * T) / sq
    return _ncdf(d1) if is_call else _ncdf(d1) - 1.0


def _iv_history(supabase, symbols, scan_date):
    """{symbol: [atm_iv, ...]} for the last ~6 months up to scan_date (one value per day)."""
    start = (datetime.strptime(scan_date, "%Y-%m-%d") - timedelta(days=IV_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    out = {}
    syms = sorted(symbols)
    for i in range(0, len(syms), 8):
        chunk = syms[i:i + 8]
        rows = _pages(lambda: supabase.from_("iv_history").select("symbol,trade_date,atm_iv")
                      .in_("symbol", chunk).gte("trade_date", start).lte("trade_date", scan_date)
                      .order("trade_date"))
        seen = set()
        for r in rows:
            k = (r["symbol"], r["trade_date"])
            if k in seen or r.get("atm_iv") in (None, 0):
                continue
            seen.add(k)
            out.setdefault(r["symbol"], []).append((r["trade_date"], float(r["atm_iv"])))
    return out


def _avg_turnover(supabase, symbols, scan_date):
    """{symbol: average daily traded value in Rs over the 20 sessions up to scan_date} (cash market)."""
    start = (datetime.strptime(scan_date, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    out = {}
    syms = sorted(symbols)
    for i in range(0, len(syms), 25):
        chunk = syms[i:i + 25]
        rows = _pages(lambda: supabase.from_("spot_daily_bars").select("symbol,trade_date,close,volume")
                      .in_("symbol", chunk).gte("trade_date", start).lte("trade_date", scan_date)
                      .order("trade_date", desc=True))
        by = {}
        for r in rows:
            try:
                v = float(r["close"]) * float(r["volume"])
            except Exception:
                continue
            if v > 0:
                by.setdefault(r["symbol"], []).append(v)
        for s, vals in by.items():
            vals = vals[:20]
            if len(vals) >= 8:
                out[s] = sum(vals) / len(vals)
    return out


def _iv_tag(history):
    """(today's atm iv, percentile, 'low'|'normal'|'high') from [(date, iv)...] ending today, or (None, None, None)."""
    if not history or len(history) < MIN_IV_DAYS:
        return None, None, None
    today = history[-1][1]
    past = [v for _, v in history[:-1]]
    if not past:
        return today, None, None
    pct = sum(1 for v in past if v <= today) / len(past) * 100.0
    tag = "low" if pct <= IV_LOW_PCT else ("high" if pct >= IV_HIGH_PCT else "normal")
    return today, round(pct, 1), tag


def enrich_events(supabase, events, scan_date):
    """events: saved Strike Stealth rows. Returns {event_key: {...extra fields...}}."""
    symbols = {e["symbol"] for e in events if e.get("symbol")}
    try:
        iv_hist = _iv_history(supabase, symbols, scan_date)
    except Exception as ex:
        print(f"[StealthGreeks] iv history failed: {ex}")
        iv_hist = {}
    try:
        turnover = _avg_turnover(supabase, symbols, scan_date)
    except Exception as ex:
        print(f"[StealthGreeks] turnover failed: {ex}")
        turnover = {}
    iv_tags = {s: _iv_tag(h) for s, h in iv_hist.items()}

    out = {}
    for e in events:
        key = e.get("event_key")
        try:
            S = float(e.get("cmp") or 0)
            K = float(e.get("strike") or 0)
            prem = float(e.get("new_premium") or 0)
            added = float((e.get("new_oi") or 0) - (e.get("old_oi") or 0))
            is_call = e.get("option_type") == "CE"
            if not key or S <= 0 or K <= 0 or prem <= 0 or added <= 0:
                continue
            T = _years_to_expiry(e["expiry"], scan_date)
            row = {}

            sigma = implied_vol(prem, S, K, T, is_call)
            if sigma:
                d = abs(_delta(is_call, S, K, T, sigma))
                size_rs = added * d * S
                row["delta"] = round(d, 3)
                row["iv_now"] = round(sigma * 100, 1)
                row["size_rs"] = round(size_rs)
                to = turnover.get(e["symbol"])
                if to:
                    pct_to = size_rs / to * 100.0
                    row["size_pct_of_turnover"] = round(pct_to, 2)
                    row["size_tag"] = ("very_large" if pct_to >= SIZE_VERY_LARGE_PCT
                                       else "large" if pct_to >= SIZE_LARGE_PCT else None)

            atm_iv, iv_pct, iv_tag = iv_tags.get(e["symbol"], (None, None, None))
            if iv_tag:
                row["iv_percentile"] = iv_pct
                row["iv_tag"] = iv_tag

            # Move needed for a buyer at today's premium to break even at expiry, vs the stock's normal move to expiry.
            be = K + prem if is_call else K - prem
            need_pct = (be - S) / S * 100.0            # signed: + means up, - means down
            row["breakeven"] = round(be, 2)
            row["move_needed_pct"] = round(need_pct, 2)
            base_iv = (atm_iv / 100.0) if atm_iv else (sigma or None)
            if base_iv:
                normal = base_iv * math.sqrt(T) * 100.0
                row["normal_move_pct"] = round(normal, 2)
                ratio = abs(need_pct) / normal if normal > 0 else None
                if ratio is not None:
                    row["move_ratio"] = round(ratio, 2)
                    row["move_tag"] = ("easy" if ratio <= MOVE_EASY else "stretch" if ratio <= MOVE_STRETCH else "hard")
            if row:
                out[key] = row
        except Exception as ex:
            print(f"[StealthGreeks] {e.get('tradingsymbol')}: {ex}")
    return out


def greeks_for_day(supabase, date=None, window="eod", min_money=500000):
    """Enrich the saved hits for a day. Cached for a few minutes."""
    from services.option_stealth_buildup import get_saved_events
    saved = get_saved_events(supabase, date, window, "all")
    day = saved.get("date")
    if not day:
        return {"date": None, "window": window, "rows": {}}
    ck = (day, window)
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    evs = [e for e in saved.get("events", [])
           if ((e.get("new_oi") or 0) - (e.get("old_oi") or 0)) * (e.get("new_premium") or 0) >= min_money]
    res = {"date": day, "window": window, "count": len(evs), "rows": enrich_events(supabase, evs, day)}
    _CACHE[ck] = (time.time(), res)
    return res
