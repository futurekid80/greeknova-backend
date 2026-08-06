"""
spot_volume_scanner.py - Spot (cash-market) Volume Breakout Scanner

Pattern: burst -> pause -> breakout, defined precisely as:
  1. BURST day: volume spikes vs trailing baseline (checked against 5/10/20-day
     averages — a burst after a longer quiet stretch scores higher conviction).
  2. PAUSE: every day after, while price holds above the burst day's LOW and
     below its HIGH — the longer this holds with volume staying quiet
     (dull, contracting volume), the more it's a genuine "coiling" base
     rather than noise. If price closes below the burst day's low, the
     setup is invalidated and dropped — a real base holds its low.
  3. BREAKOUT: price crosses above the burst day's high, ideally with volume
     elevated again — that's the actual signal.

Deliberately rule-based rather than shape-based (no flag/trendline/swing
detection) — the underlying logic (burst, hold support, go quiet, break out
with volume) is what matters; whatever shape that produces on a chart is
incidental, not the target.

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

# Tower Day (Aug 6 2026): a separate, independent detection pass alongside
# the burst/pause/breakout mechanic above. Finds days where volume dwarfs
# everything in the trailing lookback. Deliberately different from the
# burst logic above in two ways: compares against a longer trailing
# AVERAGE (not a 5/10/20d max), and never expires once found -- a
# genuine institutional entry day stays relevant weeks later.
TOWER_LOOKBACK_DAYS = 20
TOWER_THRESHOLD = 3.0

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
    trade_date/open/high/low/close/volume, already sorted ascending."""
    n = len(bars)
    if n < 21:
        return None  # not enough history for a 20-day baseline

    active = None
    last_breakout = None

    for i in range(20, n):
        vol_today = bars[i]["volume"] or 0
        high_today = bars[i]["high"]
        low_today = bars[i].get("low")
        open_today = bars[i].get("open")
        close_today = bars[i].get("close")
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
        # climax rather than accumulation). Tag it and let the trader judge.
        burst_is_green = (close_today >= open_today) if (open_today is not None and close_today is not None) else None

        if active:
            # FEATURE (Jul 27 2026): invalidate the pause if price actually
            # CLOSES below the burst day's own low. A genuine base holds
            # its low and goes quiet; one that breaks down has failed,
            # regardless of whether it later pokes back above the high.
            # Without this, a stock that fully round-tripped and broke
            # support would still sit in "Pausing" looking identical to a
            # healthy, tightly-held setup.
            if (active.get("burst_low") is not None and close_today is not None
                    and close_today < active["burst_low"]):
                active = None
                if is_burst:
                    active = {
                        "burst_date": bars[i]["trade_date"], "burst_idx": i,
                        "burst_high": high_today, "burst_low": low_today,
                        "burst_ratio": burst_ratio, "baseline_used": baseline_used,
                        "burst_is_green": burst_is_green, "pause_vol_ratios": [],
                    }
                continue

            if high_today > active["burst_high"]:
                # Breakout — price crossed above the burst day's high.
                confirmed = ratio5 >= BREAKOUT_VOL_THRESHOLD or ratio10 >= BREAKOUT_VOL_THRESHOLD
                pause_ratios = active.get("pause_vol_ratios") or []
                last_breakout = {
                    "state": "breakout",
                    "burst_date": active["burst_date"],
                    "burst_high": active["burst_high"],
                    "burst_low": active.get("burst_low"),
                    "burst_ratio": active["burst_ratio"],
                    "baseline_used": active["baseline_used"],
                    "burst_is_green": active["burst_is_green"],
                    "avg_pause_vol_ratio": round(sum(pause_ratios) / len(pause_ratios), 2) if pause_ratios else None,
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
                        "burst_high": high_today, "burst_low": low_today,
                        "burst_ratio": burst_ratio, "baseline_used": baseline_used,
                        "burst_is_green": burst_is_green, "pause_vol_ratios": [],
                    }
                continue
            # Still pausing — but if today is an even fresher/stronger burst,
            # roll the watched burst forward to today instead of the stale one.
            if is_burst and burst_ratio > active["burst_ratio"]:
                active = {
                    "burst_date": bars[i]["trade_date"], "burst_idx": i,
                    "burst_high": high_today, "burst_low": low_today,
                    "burst_ratio": burst_ratio, "baseline_used": baseline_used,
                    "burst_is_green": burst_is_green, "pause_vol_ratios": [],
                }
            else:
                # FEATURE (Jul 27 2026): genuine pause day — track this
                # day's volume ratio (consistently vs the 20d baseline, so
                # every day in the pause is measured the same way) to
                # score how "dull" the consolidation has actually been,
                # rather than just assuming it based on price sitting still.
                active.setdefault("pause_vol_ratios", []).append(round(ratio20, 2))
        elif is_burst:
            active = {
                "burst_date": bars[i]["trade_date"], "burst_idx": i,
                "burst_high": high_today, "burst_low": low_today,
                "burst_ratio": burst_ratio, "baseline_used": baseline_used,
                "burst_is_green": burst_is_green, "pause_vol_ratios": [],
            }

    # BUG FIX (Jul 27 2026): if a breakout AND a fresh new burst both land
    # on the same (most recent) day, the breakout must win — see full note
    # in git history. Checking `active` first buried real same-day breakouts.
    if last_breakout and last_breakout["breakout_date"] == bars[-1]["trade_date"]:
        return last_breakout
    if active:
        pause_days = (n - 1) - active["burst_idx"]
        if pause_days > MAX_PAUSE_DAYS:
            # Stale — burst never resolved within the watch window, no
            # longer a meaningful "coiled" setup. Drop it rather than
            # surfacing an increasingly old, less relevant burst forever.
            return None
        # FEATURE (Jul 27 2026): a burst that happened TODAY (pause_days
        # == 0) is still live and unresolved — give it its own state
        # rather than lumping it in with genuinely dormant multi-day pauses.
        state = "bursting" if pause_days == 0 else "pausing"
        pause_ratios = active.get("pause_vol_ratios") or []
        return {
            "state": state,
            "burst_date": active["burst_date"],
            "burst_high": active["burst_high"],
            "burst_low": active.get("burst_low"),
            "burst_ratio": active["burst_ratio"],
            "baseline_used": active["baseline_used"],
            "burst_is_green": active["burst_is_green"],
            "avg_pause_vol_ratio": round(sum(pause_ratios) / len(pause_ratios), 2) if pause_ratios else None,
            "pause_days": pause_days,
        }
    return None


def _find_tower_day(bars):
    """Scan every day in a symbol's available history for its most recent
    "tower day" -- volume at least TOWER_THRESHOLD times the trailing
    TOWER_LOOKBACK_DAYS average. If several days qualify, the MOST RECENT
    one wins, not the biggest ratio -- picking the biggest-ever ratio
    systematically favored old days from whenever a stock's baseline
    volume happened to be thinnest, often surfacing an unrelated, already-
    cold spike instead of whatever's actually driving the current chart.
    Never expires -- returned as long as it remains the most recent
    qualifying day found, regardless of how long ago it happened."""
    n = len(bars)
    if n < TOWER_LOOKBACK_DAYS + 1:
        return None

    best = None
    for i in range(TOWER_LOOKBACK_DAYS, n):
        vol_today = bars[i]["volume"] or 0
        if vol_today <= 0:
            continue
        avg20 = sum(b["volume"] for b in bars[i - TOWER_LOOKBACK_DAYS:i]) / TOWER_LOOKBACK_DAYS
        if avg20 <= 0:
            continue
        ratio = vol_today / avg20
        # BUG FIX (Aug 6 2026): "biggest ratio wins" systematically favored
        # old days from whenever a stock's baseline volume happened to be
        # thinnest -- e.g. PAYTM's real ignition was 10-Jul (price broke
        # from 1263 to 1342 on real volume), but a 27-Apr day won instead
        # purely because the 20-day average was tiny back then, making an
        # unrelated, already-cold spike outrank the actual current setup.
        # Picking the MOST RECENT qualifying day instead correctly follows
        # whatever's actually driving the chart right now.
        if ratio >= TOWER_THRESHOLD:
            best = {
                "tower_date": bars[i]["trade_date"],
                "tower_ratio": round(ratio, 2),
                "tower_volume": vol_today,
                "avg_volume_20d": round(avg20),
                "tower_high": bars[i]["high"],
                "tower_low": bars[i]["low"],
                "tower_close": bars[i]["close"],
                "tower_idx": i,
            }

    if best is None:
        return None

    latest = bars[-1]
    best["days_since"] = (n - 1) - best["tower_idx"]
    best["cmp"] = latest["close"]
    if best["tower_close"] and best["tower_close"] > 0:
        best["price_chg_since_pct"] = round((best["cmp"] - best["tower_close"]) / best["tower_close"] * 100, 2)
    else:
        best["price_chg_since_pct"] = None
    del best["tower_idx"]
    return best


def get_volume_breakout_scan(supabase, symbols):
    """Main scan: returns every symbol currently in a burst/pause/breakout
    state, using completed daily bars plus today's live intraday data (if
    the market is currently open and today's bar isn't final yet)."""
    all_symbols = list(symbols) + list(INDEX_TOKENS.keys())

    bars_res = supabase.from_("spot_daily_bars")\
        .select("symbol, trade_date, open, high, low, close, volume")\
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
    live_low: dict = {}
    live_vol: dict = {}
    for r in (live_res.data or []):
        sym = r["symbol"]
        px = float(r["cmp"] or 0)
        vol = int(r["volume"] or 0)
        if px > 0:
            live_open.setdefault(sym, px)  # first seen this day = open
            live_last[sym] = px            # last seen (ascending order) = latest price
            live_high[sym] = max(live_high.get(sym, 0), px)
            live_low[sym] = min(live_low.get(sym, px), px)
        live_vol[sym] = max(live_vol.get(sym, 0), vol)  # cumulative, so max = latest

    results = []
    tower_results = []
    for sym, bars in by_symbol.items():
        bars_sorted = sorted(bars, key=lambda b: b["trade_date"])
        # During market hours, always replace any existing "today" row with
        # live data — see git history for the full note on why a premature
        # EOD write must never be allowed to freeze the scanner's view of
        # the current day. Outside market hours, trust the DB row (the
        # official EOD bar via Kite historical_data).
        now_ist = _now_ist()
        # BUG FIX (Aug 3 2026): CAS goes live today — F&O now trades until
        # 3:40 PM, not 3:30 PM. Extended so live intraday data keeps
        # folding in through the actual close instead of freezing 10 min
        # early. Separately, note the underlying STOCK's own continuous
        # trading stops at 3:15 PM for CAS-covered names (auction runs
        # 3:15-3:30) — cmp_prices ticks during that window may reflect a
        # frozen/indicative price rather than a live LTP. Worth watching
        # once CAS is actually live to see how Kite's quote() responds
        # during that specific 15-minute window.
        market_open = (9, 15) <= (now_ist.hour, now_ist.minute) < (15, 40)
        if market_open:
            if bars_sorted and bars_sorted[-1]["trade_date"] == today_str:
                bars_sorted = bars_sorted[:-1]
            if sym in live_high:
                bars_sorted = bars_sorted + [{
                    "trade_date": today_str,
                    "open": live_open.get(sym), "high": live_high[sym],
                    "low": live_low.get(sym), "close": live_last.get(sym),
                    "volume": live_vol.get(sym, 0),
                }]
        elif sym in live_high and (not bars_sorted or bars_sorted[-1]["trade_date"] != today_str):
            bars_sorted = bars_sorted + [{
                "trade_date": today_str,
                "open": live_open.get(sym), "high": live_high[sym],
                "low": live_low.get(sym), "close": live_last.get(sym),
                "volume": live_vol.get(sym, 0),
            }]
        pattern = _find_active_pattern(bars_sorted)
        if pattern:
            pattern["symbol"] = sym
            # BUG FIX (Aug 5 2026): "cmp" was set from live_high (the day's
            # running HIGH), not the actual latest price -- harmless on days
            # a stock mostly trends up (high stays close to current price),
            # but badly misleading on a day like this: BSE opened near its
            # high (3610) then fell steadily all morning to ~3494, and the
            # scanner kept showing 3610 as "CMP" long after the real price
            # had moved well away from it. Same issue existed in the
            # after-hours fallback (used the day's high instead of its
            # close). Now uses the genuine last-seen price in both cases.
            pattern["cmp"] = live_last.get(sym) or (bars_sorted[-1]["close"] if bars_sorted else None)
            results.append(pattern)

        tower = _find_tower_day(bars_sorted)
        if tower:
            tower["symbol"] = sym
            tower_results.append(tower)

    _state_rank = {"breakout": 0, "bursting": 1, "pausing": 2}
    results.sort(key=lambda r: (_state_rank.get(r["state"], 3), -r.get("burst_ratio", 0)))
    tower_results.sort(key=lambda t: -t["tower_ratio"])
    return {
        "scanned": len(by_symbol),
        "matches": len(results),
        "results": results,
        "tower_days": tower_results,
        "tower_matches": len(tower_results),
        "generated_at": _now_ist().isoformat(),
    }
