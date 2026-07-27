"""
spot_volume_scanner.py - Spot (cash-market) Volume Breakout Scanner

Pattern: burst -> pause -> breakout, defined precisely as:
  1. BURST day: volume spikes vs trailing baseline (checked against 5/10/20-day
     averages — a burst after a longer quiet stretch scores higher conviction).
  2. PAUSE: every day after, while price stays below the burst day's HIGH —
     the longer this holds with volume staying quiet, the more it's "coiling".
  3. BREAKOUT: price crosses above the burst day's high, ideally with volume
     elevated again — that's the actual signal.

Built specifically to sidestep the false signals futures volume gives during
rollover weeks (volume artificially splits across near/far month contracts) —
this scanner works entirely off NSE cash-market (spot) volume instead.

Data sources:
  - spot_daily_bars   : historical daily OHLCV per symbol (backfilled once via
                         kite.historical_data, kept current by a daily EOD job)
  - cmp_prices.volume : live intraday cash-market volume, captured every
                         5 min alongside CMP price — used to evaluate TODAY's
                         still-in-progress candle before EOD, so the scanner
                         reflects live intraday state, not just yesterday's.
"""
from datetime import datetime, timedelta, date as date_type
import time
import pytz

def _now_ist():
    """Railway's datetime.now() returns naive UTC. Every date/timestamp
    computation in this module needs IST wall-clock time (market hours,
    calendar days, and the scan's own generated_at display all assume
    it) — this is the single source of truth for 'now' in this file."""
    return datetime.now(pytz.timezone("Asia/Kolkata"))

BURST_THRESHOLD = 2.0   # volume >= 2x baseline counts as a burst
BREAKOUT_VOL_THRESHOLD = 1.5  # volume >= 1.5x baseline confirms a breakout
MAX_PAUSE_DAYS = 10  # drop a burst from "pausing" if it hasn't broken out
                     # within this many trading days — beyond this it's
                     # stopped being a tight coil and is just noise
LOOKBACK_DAYS_FOR_SCAN = 40    # how far back to look for an active burst/pause

INDEX_TOKENS = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
    "FINNIFTY":  257801,
}


def _get_instrument_tokens(kite, symbols):
    """Map symbol -> NSE instrument token, for indices + stocks."""
    token_map = dict(INDEX_TOKENS)
    try:
        instruments = kite.instruments("NSE")
        symset = set(symbols)
        for inst in instruments:
            if inst["tradingsymbol"] in symset:
                token_map[inst["tradingsymbol"]] = inst["instrument_token"]
    except Exception as e:
        print(f"[SPOT_VOL] Instruments fetch failed: {e}")
    return token_map


def backfill_spot_daily_bars(supabase, kite, symbols, days_back=180):
    """One-time (or re-runnable) backfill of historical daily OHLCV per
    symbol, needed to establish the 5/10/20-day volume baselines and find
    any already-in-progress burst/pause pattern. Safe to re-run — upserts
    on (symbol, trade_date)."""
    all_symbols = list(symbols) + list(INDEX_TOKENS.keys())
    token_map = _get_instrument_tokens(kite, symbols)

    from_date = (_now_ist().date() - timedelta(days=days_back)).isoformat()
    to_date = _now_ist().date().isoformat()

    total_bars = 0
    for sym in all_symbols:
        token = token_map.get(sym)
        if not token:
            print(f"[SPOT_VOL] {sym}: no instrument token, skipping")
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
            rows = []
            for c in candles:
                rows.append({
                    "symbol": sym,
                    "trade_date": str(c["date"])[:10],
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": int(c.get("volume", 0)),
                })
            if rows:
                for i in range(0, len(rows), 500):
                    supabase.table("spot_daily_bars")\
                        .upsert(rows[i:i + 500], on_conflict="symbol,trade_date").execute()
                total_bars += len(rows)
                print(f"[SPOT_VOL] {sym}: backfilled {len(rows)} daily bars")
            time.sleep(0.1)
        except Exception as e:
            print(f"[SPOT_VOL] {sym}: backfill failed — {e}")

    print(f"[SPOT_VOL] Backfill complete — {total_bars} total bars across {len(all_symbols)} symbols")
    return {"status": "complete", "total_bars": total_bars, "symbols": len(all_symbols)}


def append_todays_spot_bar(supabase, kite, symbols):
    """Daily EOD job — appends just today's now-completed daily bar for
    every symbol. Much lighter than a full backfill; meant to run once a
    day after market close so spot_daily_bars stays current going forward."""
    all_symbols = list(symbols) + list(INDEX_TOKENS.keys())
    token_map = _get_instrument_tokens(kite, symbols)
    today = _now_ist().date().isoformat()

    rows = []
    for sym in all_symbols:
        token = token_map.get(sym)
        if not token:
            continue
        try:
            candles = kite.historical_data(
                instrument_token=token, from_date=today, to_date=today,
                interval="day", continuous=False, oi=False,
            )
            if candles:
                c = candles[-1]
                rows.append({
                    "symbol": sym,
                    "trade_date": str(c["date"])[:10],
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": int(c.get("volume", 0)),
                })
            time.sleep(0.05)
        except Exception as e:
            print(f"[SPOT_VOL] EOD append {sym} failed: {e}")

    if rows:
        for i in range(0, len(rows), 500):
            supabase.table("spot_daily_bars")\
                .upsert(rows[i:i + 500], on_conflict="symbol,trade_date").execute()
    print(f"[SPOT_VOL] EOD append — {len(rows)} bars added for {today}")
    return {"status": "complete", "bars_added": len(rows), "date": today}


def _find_active_pattern(bars):
    """Given a symbol's daily bars (ascending by date), walk forward and
    return the most recent active burst/pause/breakout state, or None if
    no pattern is currently active. bars: list of dicts with
    trade_date/open/high/close/volume, already sorted ascending."""
    n = len(bars)
    if n < 21:
        return None  # not enough history for a 20-day baseline

    active = None  # {"burst_date", "burst_high", "burst_ratio", "baseline_used", "burst_is_green"}
    last_breakout = None

    for i in range(20, n):
        vol_today = bars[i]["volume"] or 0
        high_today = bars[i]["high"]
        if vol_today <= 0:
            continue

        avg5 = sum(b["volume"] for b in bars[i - 5:i]) / 5
        avg10 = sum(b["volume"] for b in bars[i - 10:i]) / 10
        avg20 = sum(b["volume"] for b in bars[i - 20:i]) / 20

        ratio5 = vol_today / avg5 if avg5 > 0 else 0
        ratio10 = vol_today / avg10 if avg10 > 0 else 0
        ratio20 = vol_today / avg20 if avg20 > 0 else 0

        # Longer baseline = higher conviction. A 20-day burst beats a 5-day one.
        is_burst = False
        baseline_used, burst_ratio = None, 0
        if ratio20 >= BURST_THRESHOLD:
            is_burst, baseline_used, burst_ratio = True, "20d", round(ratio20, 2)
        elif ratio10 >= BURST_THRESHOLD:
            is_burst, baseline_used, burst_ratio = True, "10d", round(ratio10, 2)
        elif ratio5 >= BURST_THRESHOLD:
            is_burst, baseline_used, burst_ratio = True, "5d", round(ratio5, 2)

        # BUG FIX / FEATURE (Jul 27 2026): a volume burst on a green candle
        # (close >= open — buyers stepping in) reads very differently from
        # one on a red candle (close < open — often capitulation/selling
        # climax rather than accumulation). The algorithm can't tell these
        # apart mechanically, so tag it and let the trader judge — e.g.
        # TVSMOTOR's clean quiet-days-then-green-burst setup vs INDIGO's
        # burst landing on the 4th red day of a selloff are very different
        # setups even though both technically qualify as "a burst".
        open_today = bars[i].get("open")
        close_today = bars[i].get("close")
        burst_is_green = (close_today >= open_today) if (open_today is not None and close_today is not None) else None

        if active:
            if high_today > active["burst_high"]:
                # Breakout — price crossed above the burst day's high.
                confirmed = ratio5 >= BREAKOUT_VOL_THRESHOLD or ratio10 >= BREAKOUT_VOL_THRESHOLD
                last_breakout = {
                    "state": "breakout",
                    "burst_date": active["burst_date"],
                    "burst_high": active["burst_high"],
                    "burst_ratio": active["burst_ratio"],
                    "baseline_used": active["baseline_used"],
                    "burst_is_green": active["burst_is_green"],
                    "breakout_date": bars[i]["trade_date"],
                    "breakout_vol_ratio": round(max(ratio5, ratio10), 2),
                    "confirmed": confirmed,
                    "pause_days": i - active["burst_idx"],
                }
                active = None
                # A fresh burst can start the very same day it broke out.
                if is_burst:
                    active = {
                        "burst_date": bars[i]["trade_date"], "burst_idx": i,
                        "burst_high": high_today, "burst_ratio": burst_ratio,
                        "baseline_used": baseline_used, "burst_is_green": burst_is_green,
                    }
                continue
            # Still pausing — but if today is an even fresher/stronger burst,
            # roll the watched burst forward to today instead of the stale one.
            if is_burst and burst_ratio > active["burst_ratio"]:
                active = {
                    "burst_date": bars[i]["trade_date"], "burst_idx": i,
                    "burst_high": high_today, "burst_ratio": burst_ratio,
                    "baseline_used": baseline_used, "burst_is_green": burst_is_green,
                }
        elif is_burst:
            active = {
                "burst_date": bars[i]["trade_date"], "burst_idx": i,
                "burst_high": high_today, "burst_ratio": burst_ratio,
                "baseline_used": baseline_used, "burst_is_green": burst_is_green,
            }

    if active:
        pause_days = (n - 1) - active["burst_idx"]
        if pause_days > MAX_PAUSE_DAYS:
            # Stale — burst never resolved within the watch window, no
            # longer a meaningful "coiled" setup. Drop it rather than
            # surfacing an increasingly old, less relevant burst forever.
            return None
        return {
            "state": "pausing",
            "burst_date": active["burst_date"],
            "burst_high": active["burst_high"],
            "burst_ratio": active["burst_ratio"],
            "baseline_used": active["baseline_used"],
            "burst_is_green": active["burst_is_green"],
            "pause_days": pause_days,
        }
    if last_breakout and last_breakout["breakout_date"] == bars[-1]["trade_date"]:
        # Only surface a breakout if it happened on the most recent bar —
        # older, already-played-out breakouts aren't actionable anymore.
        return last_breakout
    return None


def get_volume_breakout_scan(supabase, symbols):
    """Main scan: returns every symbol currently in a burst/pause/breakout
    state, using completed daily bars plus today's live intraday data (if
    the market is currently open and today's bar isn't final yet)."""
    all_symbols = list(symbols) + list(INDEX_TOKENS.keys())

    bars_res = supabase.from_("spot_daily_bars")\
        .select("symbol, trade_date, open, high, close, volume")\
        .in_("symbol", all_symbols)\
        .order("trade_date", desc=False)\
        .limit(50000).execute()

    by_symbol: dict = {}
    for r in (bars_res.data or []):
        by_symbol.setdefault(r["symbol"], []).append(r)

    # Live "today so far" from intraday cmp_prices captures, in case today's
    # completed daily bar isn't in spot_daily_bars yet (market still open).
    today_str = _now_ist().date().isoformat()
    live_res = supabase.from_("cmp_prices")\
        .select("symbol, cmp, volume, timestamp")\
        .gte("timestamp", f"{today_str}T00:00:00+00:00")\
        .in_("symbol", all_symbols)\
        .order("timestamp", desc=False).limit(20000).execute()

    live_open: dict = {}
    live_last: dict = {}
    live_high: dict = {}
    live_vol: dict = {}
    for r in (live_res.data or []):
        sym = r["symbol"]
        px = float(r["cmp"] or 0)
        vol = int(r["volume"] or 0)
        if px > 0:
            live_open.setdefault(sym, px)  # first seen this day = open
            live_last[sym] = px            # last seen (ascending order) = latest price
            live_high[sym] = max(live_high.get(sym, 0), px)
        live_vol[sym] = max(live_vol.get(sym, 0), vol)  # cumulative, so max = latest

    results = []
    for sym, bars in by_symbol.items():
        bars_sorted = sorted(bars, key=lambda b: b["trade_date"])
        # Fold in today's live data as a provisional last bar, if we have
        # any and it's not already the last completed bar in the table.
        if sym in live_high and (not bars_sorted or bars_sorted[-1]["trade_date"] != today_str):
            bars_sorted = bars_sorted + [{
                "trade_date": today_str,
                "open": live_open.get(sym), "high": live_high[sym],
                "close": live_last.get(sym), "volume": live_vol.get(sym, 0),
            }]
        pattern = _find_active_pattern(bars_sorted)
        if pattern:
            pattern["symbol"] = sym
            pattern["cmp"] = live_high.get(sym) or (bars_sorted[-1]["high"] if bars_sorted else None)
            results.append(pattern)

    results.sort(key=lambda r: (r["state"] != "breakout", -r.get("burst_ratio", 0)))
    return {
        "scanned": len(by_symbol),
        "matches": len(results),
        "results": results,
        # BUG FIX (Jul 27 2026): datetime.now() on Railway returns naive
        # UTC — the frontend then displayed that raw clock reading labeled
        # as IST (showing e.g. "08:25 am IST" at actual 1:55 pm IST, a
        # dead giveaway of the 5:30 UTC offset). Use IST-aware datetime
        # explicitly, consistent with the rest of the app's convention.
        "generated_at": _now_ist().isoformat(),
    }
