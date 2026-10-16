"""
Wolves Scanner -- Oct 16 2026, v2.

Ports the "3 Wolves" ADX+RSI+BB confluence signal from Manish's local
Fyers/Streamlit scanner onto Kite data, so he can compare it against the
local tool and eventually retire it.

THE WOLVES RULE (ported verbatim from the live logic in his file --
build_wolves_events_today / evaluate_timeframe v2):
  Bullish: Hourly RSI > 60 AND (hourly breaks the upper Bollinger Band, or
  is already above it with a recent compression breakout) AND a timeframe's
  ADX freshly crosses above 25 with bullish DI. Each timeframe that has the
  ADX cross produces one Wolf: Hourly -> Scout Wolf, Daily -> Alpha Wolf,
  Weekly/Monthly -> Apex Wolf.
  Bearish: the exact mirror (hourly RSI < 40, lower-band break/breakdown,
  ADX cross with bearish DI) -- ported from _v32_is_hourly_bear_bb /
  _v32_wolves_tier in his file, which the first pass of this port missed.

ARCHITECTURE (v2 -- rewritten after the v1 "full re-fetch every scan" design
turned out too slow to run live): Daily/Weekly/Monthly only change once a
day, so their ADX-cross state is computed ONCE after close
(refresh_tf_cache) and cached in wolves_tf_cache. Live ticks during market
hours (run_live_tick) only re-fetch Hourly candles -- one Kite call per
symbol instead of two, and a ~30-day window instead of years -- then
combine with the cached Daily/Weekly/Monthly flags. This is what makes a
5-minute live cadence (matching his local tool's "Core Live" refresh)
actually feasible under Kite's rate limits.

v3 adds the standalone "Fresh Events" layer Manish actually relies on day
to day -- he prioritizes a lone ADX Cross per timeframe to mend/amend
positions, confluence or not:
  - Standalone ADX Cross events, Hourly/Daily/Weekly/Monthly, independent
    of the RSI/BB gate above (reuses the same adx_cross_bull/bear flags).
  - "ADX Full Alignment" -- a distinct, stronger event when all 4
    timeframes cross together in the same direction.
  - Standalone RSI Cross events (prev RSI crossing 60/40), same 4
    timeframes.
  - The Grandfather/Father/Son multi-timeframe RSI setup (a named system,
    confirmed against its public description since it isn't in Manish's
    source file): Monthly RSI ("Grandfather") and Weekly RSI ("Father")
    both > 70, Daily RSI ("Son") pulled back into 40-45 = bullish setup;
    Daily RSI > 70 = exit. Bearish mirror: Monthly+Weekly RSI < 30, Daily
    RSI in 55-60 = setup, Daily RSI < 30 = exit. RSI(14) throughout, per
    Manish's confirmation. These read Daily/Weekly/Monthly RSI off
    wolves_tf_cache as a state check (not a fresh cross), since a cross
    needs the prior day's RSI, which isn't stored.

STILL NOT PORTED: the Decision Score / Rank (A+/A/B/C) / Confidence
system and Sector Tailwind from the local tool -- scoped out as a further
fast-follow.
"""
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz

from utils.db import get_supabase

IST = pytz.timezone("Asia/Kolkata")

ADX_LEN = 14
RSI_LEN = 14
BB_LEN = 20
BB_STD = 2.0
ADX_THRESHOLD = 25.0
RSI_BULL = 60.0
RSI_BEAR = 40.0

INDEX_TOKENS = {
    "NIFTY": 256265,
    "BANKNIFTY": 260105,
    "FINNIFTY": 257801,
    "MIDCPNIFTY": 288009,
    "SENSEX": 265,
}

WOLF_TIER = {
    "Hourly": "Scout Wolf",
    "Daily": "Alpha Wolf",
    "Weekly": "Apex Wolf",
    "Monthly": "Apex Wolf",
}


def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """ADX/+DI/-DI/RSI/Bollinger Bands + BB-width compression -- ported
    verbatim from the live version in the source file."""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    high, low, close = out["high"], out["low"], out["close"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = rma(tr, ADX_LEN)
    plus_di = 100 * rma(pd.Series(plus_dm, index=out.index), ADX_LEN) / atr
    minus_di = 100 * rma(pd.Series(minus_dm, index=out.index), ADX_LEN) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    out["ADX"] = rma(dx, ADX_LEN)
    out["+DI"] = plus_di
    out["-DI"] = minus_di

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = rma(gain, RSI_LEN) / rma(loss, RSI_LEN)
    out["RSI"] = 100 - (100 / (1 + rs))

    out["BB Mid"] = close.rolling(BB_LEN).mean()
    bb_std_val = close.rolling(BB_LEN).std()
    out["BB Upper"] = out["BB Mid"] + BB_STD * bb_std_val
    out["BB Lower"] = out["BB Mid"] - BB_STD * bb_std_val

    mid = pd.to_numeric(out["BB Mid"], errors="coerce")
    upper = pd.to_numeric(out["BB Upper"], errors="coerce")
    lower = pd.to_numeric(out["BB Lower"], errors="coerce")
    width = (upper - lower) / mid.replace(0, np.nan)
    out["BB Width"] = width

    def pct_rank_latest(values: pd.Series) -> float:
        clean = pd.Series(values).dropna()
        return float(clean.rank(pct=True).iloc[-1]) if not clean.empty else np.nan

    out["BB Width Pct"] = width.rolling(100, min_periods=30).apply(pct_rank_latest, raw=False)
    out["BB Compressed"] = out["BB Width Pct"] <= 0.20

    return out


def safe_num(value, default=np.nan) -> float:
    try:
        v = float(value)
        return default if pd.isna(v) else v
    except Exception:
        return default


def evaluate_timeframe(df: pd.DataFrame, timeframe: str) -> dict:
    """adx_cross_bull/bear, rsi_bull/bear, bb_upper_cross/bb_lower_cross,
    bb_compression_breakout/breakdown, bb_bull/bb_bear -- ported from the
    live evaluate_timeframe, both bullish and bearish sides."""
    empty = {
        "timeframe": timeframe,
        "adx_cross_bull": False, "adx_cross_bear": False,
        "rsi_bull": False, "rsi_bear": False,
        "rsi_cross_bull": False, "rsi_cross_bear": False,
        "bb_upper_cross": False, "bb_lower_cross": False,
        "bb_compression_breakout": False, "bb_compression_breakdown": False,
        "bb_bull": False, "bb_bear": False,
        "close": np.nan, "adx": np.nan, "rsi": np.nan, "di_gap": np.nan, "bb_width_pct": np.nan,
    }
    if df is None or len(df) < 25:
        return empty

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = safe_num(latest.get("close"))
    prev_close = safe_num(prev.get("close"))
    adx = safe_num(latest.get("ADX"))
    prev_adx = safe_num(prev.get("ADX"))
    plus_di = safe_num(latest.get("+DI"))
    minus_di = safe_num(latest.get("-DI"))
    rsi = safe_num(latest.get("RSI"))
    prev_rsi = safe_num(prev.get("RSI"))
    upper = safe_num(latest.get("BB Upper"))
    lower = safe_num(latest.get("BB Lower"))
    prev_upper = safe_num(prev.get("BB Upper"))
    prev_lower = safe_num(prev.get("BB Lower"))
    bb_width_pct = safe_num(latest.get("BB Width Pct"))
    compressed_recent = bool(df["BB Compressed"].tail(12).fillna(False).astype(bool).any()) if "BB Compressed" in df.columns else False

    adx_cross = bool(prev_adx <= ADX_THRESHOLD and adx > ADX_THRESHOLD)
    adx_cross_bull = bool(adx_cross and plus_di > minus_di)
    adx_cross_bear = bool(adx_cross and minus_di > plus_di)

    rsi_cross_bull = bool(prev_rsi <= RSI_BULL and rsi > RSI_BULL)
    rsi_cross_bear = bool(prev_rsi >= RSI_BEAR and rsi < RSI_BEAR)

    bb_upper_cross = bool(prev_close <= prev_upper and close > upper)
    bb_lower_cross = bool(prev_close >= prev_lower and close < lower)
    bb_compression_breakout = bool(compressed_recent and bb_upper_cross)
    bb_compression_breakdown = bool(compressed_recent and bb_lower_cross)
    bb_bull = bool(close > upper)
    bb_bear = bool(close < lower)

    return {
        "timeframe": timeframe,
        "adx_cross_bull": adx_cross_bull, "adx_cross_bear": adx_cross_bear,
        "rsi_bull": bool(rsi > RSI_BULL), "rsi_bear": bool(rsi < RSI_BEAR),
        "rsi_cross_bull": rsi_cross_bull, "rsi_cross_bear": rsi_cross_bear,
        "bb_upper_cross": bb_upper_cross, "bb_lower_cross": bb_lower_cross,
        "bb_compression_breakout": bb_compression_breakout, "bb_compression_breakdown": bb_compression_breakdown,
        "bb_bull": bb_bull, "bb_bear": bb_bear,
        "close": close, "adx": adx, "rsi": rsi,
        "di_gap": abs(plus_di - minus_di) if not (pd.isna(plus_di) or pd.isna(minus_di)) else np.nan,
        "bb_width_pct": bb_width_pct,
    }


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.set_index("date").sort_index()
    out = x.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index()


def _build_token_map(kite, symbols: list[str]) -> dict[str, int]:
    token_map = {s: t for s, t in INDEX_TOKENS.items() if s in symbols}
    stocks_needed = set(symbols) - set(token_map)
    if not stocks_needed:
        return token_map
    try:
        for inst in kite.instruments("NSE"):
            sym = inst["tradingsymbol"]
            if sym in stocks_needed:
                token_map[sym] = inst["instrument_token"]
    except Exception as e:
        print(f"[Wolves] instruments() fetch failed: {e}")
    return token_map


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# EOD: refresh the Daily/Weekly/Monthly ADX-cross cache (once per day)
# ---------------------------------------------------------------------------

def refresh_tf_cache(symbols: list[str] | None = None) -> dict:
    """Fetches daily candles once per symbol (years of history, native Kite
    'day' interval -- one call), derives Weekly/Monthly via resample, and
    caches each timeframe's adx_cross_bull/bear + raw values. Live ticks
    read this instead of re-fetching daily/weekly/monthly every 5 minutes."""
    from services.kite_auth import get_kite_client
    from api.iv_analysis import SYMBOLS as _ALL_SYMBOLS

    universe = symbols or list(_ALL_SYMBOLS)
    kite = get_kite_client()
    supabase = get_supabase()
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
        try:
            daily_candles = kite.historical_data(instrument_token=token, from_date=daily_from, to_date=to_date, interval="day", continuous=False, oi=False)
            time.sleep(0.34)
        except Exception as e:
            print(f"[Wolves] {symbol}: daily fetch failed -- {e}")
            errored += 1
            continue

        daily_df = _candles_to_df(daily_candles)
        if daily_df.empty:
            skipped += 1
            continue

        daily_ind = add_indicators(daily_df)
        weekly_ind = add_indicators(resample_ohlcv(daily_df, "W-FRI"))
        monthly_ind = add_indicators(resample_ohlcv(daily_df, "ME"))

        row = {"symbol": symbol, "computed_date": today_ist.isoformat()}
        for tf_name, tf_df in [("daily", daily_ind), ("weekly", weekly_ind), ("monthly", monthly_ind)]:
            tf_eval = evaluate_timeframe(tf_df, tf_name.capitalize())
            row[f"{tf_name}_adx_cross_bull"] = tf_eval["adx_cross_bull"]
            row[f"{tf_name}_adx_cross_bear"] = tf_eval["adx_cross_bear"]
            row[f"{tf_name}_close"] = round(tf_eval["close"], 2) if not pd.isna(tf_eval["close"]) else None
            row[f"{tf_name}_adx"] = round(tf_eval["adx"], 2) if not pd.isna(tf_eval["adx"]) else None
            row[f"{tf_name}_rsi"] = round(tf_eval["rsi"], 2) if not pd.isna(tf_eval["rsi"]) else None
            row[f"{tf_name}_rsi_cross_bull"] = tf_eval["rsi_cross_bull"]
            row[f"{tf_name}_rsi_cross_bear"] = tf_eval["rsi_cross_bear"]
        rows.append(row)
        cached += 1

    if rows:
        try:
            supabase.from_("wolves_tf_cache").upsert(rows, on_conflict="symbol").execute()
        except Exception as e:
            print(f"[Wolves] tf_cache upsert failed: {e}")

    print(f"[Wolves] TF cache refresh: {cached} cached, {skipped} skipped, {errored} errored")
    return {"cached": cached, "skipped": skipped, "errored": errored}


# ---------------------------------------------------------------------------
# Live: hourly-only tick, combined with the cached Daily/Weekly/Monthly state
# ---------------------------------------------------------------------------

def run_live_tick(symbols: list[str] | None = None) -> dict:
    """Fetches ONLY hourly candles (one Kite call/symbol, ~30-day window)
    and combines with the cached Daily/Weekly/Monthly flags from
    refresh_tf_cache. This is the fast path meant to run every few minutes
    during market hours."""
    from services.kite_auth import get_kite_client
    from api.iv_analysis import SYMBOLS as _ALL_SYMBOLS

    universe = symbols or list(_ALL_SYMBOLS)
    kite = get_kite_client()
    supabase = get_supabase()
    token_map = _build_token_map(kite, universe)

    today_ist = datetime.now(IST).date()
    hourly_from = (today_ist - timedelta(days=30)).isoformat()
    to_date = today_ist.isoformat()

    tf_cache_rows = supabase.from_("wolves_tf_cache").select("*").in_("symbol", universe).execute().data or []
    tf_cache = {r["symbol"]: r for r in tf_cache_rows}

    rows_to_upsert = []
    fresh_events = []
    scanned, skipped, errored, wolves_found = 0, 0, 0, 0

    for symbol in universe:
        token = token_map.get(symbol)
        cache_row = tf_cache.get(symbol)
        if not token or not cache_row:
            skipped += 1
            continue
        try:
            hourly_candles = kite.historical_data(instrument_token=token, from_date=hourly_from, to_date=to_date, interval="60minute", continuous=False, oi=False)
            time.sleep(0.34)
        except Exception as e:
            print(f"[Wolves] {symbol}: hourly fetch failed -- {e}")
            errored += 1
            continue

        hourly_df = _candles_to_df(hourly_candles)
        if hourly_df.empty:
            skipped += 1
            continue

        hourly_ind = add_indicators(hourly_df)
        h = evaluate_timeframe(hourly_ind, "Hourly")
        scanned += 1
        scan_time = datetime.now(IST).strftime("%H:%M:%S")

        tf_data = {
            "Hourly": {"adx_cross_bull": h["adx_cross_bull"], "adx_cross_bear": h["adx_cross_bear"],
                       "rsi_cross_bull": h["rsi_cross_bull"], "rsi_cross_bear": h["rsi_cross_bear"],
                       "adx": h["adx"], "rsi": h["rsi"], "close": h["close"]},
            "Daily": {"adx_cross_bull": cache_row.get("daily_adx_cross_bull"), "adx_cross_bear": cache_row.get("daily_adx_cross_bear"),
                      "rsi_cross_bull": cache_row.get("daily_rsi_cross_bull"), "rsi_cross_bear": cache_row.get("daily_rsi_cross_bear"),
                      "adx": cache_row.get("daily_adx"), "rsi": cache_row.get("daily_rsi"), "close": cache_row.get("daily_close")},
            "Weekly": {"adx_cross_bull": cache_row.get("weekly_adx_cross_bull"), "adx_cross_bear": cache_row.get("weekly_adx_cross_bear"),
                       "rsi_cross_bull": cache_row.get("weekly_rsi_cross_bull"), "rsi_cross_bear": cache_row.get("weekly_rsi_cross_bear"),
                       "adx": cache_row.get("weekly_adx"), "rsi": cache_row.get("weekly_rsi"), "close": cache_row.get("weekly_close")},
            "Monthly": {"adx_cross_bull": cache_row.get("monthly_adx_cross_bull"), "adx_cross_bear": cache_row.get("monthly_adx_cross_bear"),
                        "rsi_cross_bull": cache_row.get("monthly_rsi_cross_bull"), "rsi_cross_bear": cache_row.get("monthly_rsi_cross_bear"),
                        "adx": cache_row.get("monthly_adx"), "rsi": cache_row.get("monthly_rsi"), "close": cache_row.get("monthly_close")},
        }

        def _event(direction, event_type, tf_name, extra_meaning, rsi_val=None, adx_val=None):
            event_key = "|".join([today_ist.isoformat(), symbol, direction, event_type, tf_name])
            fresh_events.append({
                "event_key": event_key, "scan_date": today_ist.isoformat(), "scan_time": scan_time,
                "symbol": symbol, "direction": direction, "event_type": event_type, "timeframe": tf_name,
                "close": round(h["close"], 2) if not pd.isna(h["close"]) else None,
                "rsi": round(rsi_val, 2) if rsi_val is not None and not pd.isna(rsi_val) else None,
                "adx": round(adx_val, 2) if adx_val is not None and not pd.isna(adx_val) else None,
                "meaning": extra_meaning,
            })

        # Standalone ADX Cross + RSI Cross events, independent of the Wolves gate --
        # Manish prioritizes these alone to mend/amend positions.
        for tf_name, d in tf_data.items():
            if d["adx_cross_bull"]:
                _event("Bullish", "ADX Cross 25", tf_name, f"{tf_name} ADX freshly crossed above 25 with bullish DI.", adx_val=d["adx"])
            if d["adx_cross_bear"]:
                _event("Bearish", "ADX Cross 25", tf_name, f"{tf_name} ADX freshly crossed above 25 with bearish DI.", adx_val=d["adx"])
            if d["rsi_cross_bull"]:
                _event("Bullish", "RSI Cross 60", tf_name, f"{tf_name} RSI freshly crossed above 60.", rsi_val=d["rsi"])
            if d["rsi_cross_bear"]:
                _event("Bearish", "RSI Cross 40", tf_name, f"{tf_name} RSI freshly crossed below 40.", rsi_val=d["rsi"])

        # ADX Full Alignment -- all 4 timeframes cross together, same direction.
        if all(tf_data[tf]["adx_cross_bull"] for tf in ("Hourly", "Daily", "Weekly", "Monthly")):
            _event("Bullish", "ADX Full Alignment", "All", "Hourly, Daily, Weekly and Monthly ADX all freshly crossed above 25 with bullish DI together.")
        if all(tf_data[tf]["adx_cross_bear"] for tf in ("Hourly", "Daily", "Weekly", "Monthly")):
            _event("Bearish", "ADX Full Alignment", "All", "Hourly, Daily, Weekly and Monthly ADX all freshly crossed above 25 with bearish DI together.")

        # Grandfather/Father/Son RSI (Monthly=Grandfather, Weekly=Father, Daily=Son), RSI(14), state-based.
        gf_rsi, father_rsi, son_rsi = cache_row.get("monthly_rsi"), cache_row.get("weekly_rsi"), cache_row.get("daily_rsi")
        if gf_rsi is not None and father_rsi is not None and son_rsi is not None:
            if gf_rsi > 70 and father_rsi > 70 and 40 <= son_rsi <= 45:
                _event("Bullish", "Grandfather-Father-Son Setup", "Daily", f"Monthly RSI {gf_rsi} & Weekly RSI {father_rsi} both >70, Daily RSI {son_rsi} pulled back to 40-45 -- continuation setup.", rsi_val=son_rsi)
            if gf_rsi > 70 and father_rsi > 70 and son_rsi > 70:
                _event("Bullish", "Grandfather-Father-Son Exit", "Daily", f"Daily RSI {son_rsi} back above 70 with Monthly/Weekly RSI >70 -- overbought exit.", rsi_val=son_rsi)
            if gf_rsi < 30 and father_rsi < 30 and 55 <= son_rsi <= 60:
                _event("Bearish", "Grandfather-Father-Son Setup", "Daily", f"Monthly RSI {gf_rsi} & Weekly RSI {father_rsi} both <30, Daily RSI {son_rsi} pulled back to 55-60 -- continuation setup.", rsi_val=son_rsi)
            if gf_rsi < 30 and father_rsi < 30 and son_rsi < 30:
                _event("Bearish", "Grandfather-Father-Son Exit", "Daily", f"Daily RSI {son_rsi} back below 30 with Monthly/Weekly RSI <30 -- oversold exit.", rsi_val=son_rsi)

        # Full Wolves confluence (unchanged) -- hourly RSI+BB gate, then per-timeframe ADX cross.
        bull_gate = h["rsi_bull"] and (h["bb_upper_cross"] or h["bb_compression_breakout"] or h["bb_bull"])
        bear_gate = h["rsi_bear"] and (h["bb_lower_cross"] or h["bb_compression_breakdown"] or h["bb_bear"])
        for direction, gate_ok, key in [("Bullish", bull_gate, "adx_cross_bull"), ("Bearish", bear_gate, "adx_cross_bear")]:
            if not gate_ok:
                continue
            for tf_name, d in tf_data.items():
                if not d[key]:
                    continue
                event_key = "|".join([today_ist.isoformat(), symbol, direction, WOLF_TIER[tf_name], tf_name])
                rows_to_upsert.append({
                    "event_key": event_key,
                    "scan_date": today_ist.isoformat(),
                    "scan_time": scan_time,
                    "symbol": symbol,
                    "direction": direction,
                    "wolf_tier": WOLF_TIER[tf_name],
                    "trigger_timeframe": tf_name,
                    "close": round(h["close"], 2) if not pd.isna(h["close"]) else None,
                    "adx": d["adx"],
                    "hourly_rsi": round(h["rsi"], 2) if not pd.isna(h["rsi"]) else None,
                    "di_gap": round(h["di_gap"], 2) if not pd.isna(h["di_gap"]) else None,
                    "bb_width_pct": round(h["bb_width_pct"] * 100, 1) if not pd.isna(h["bb_width_pct"]) else None,
                    "meaning": f"{WOLF_TIER[tf_name]}: {tf_name} ADX fresh cross above 25 ({direction.lower()}) with hourly RSI {'>60' if direction=='Bullish' else '<40'} and hourly {'upper' if direction=='Bullish' else 'lower'}-BB break.",
                })
                wolves_found += 1

    if rows_to_upsert:
        try:
            supabase.from_("wolves_signal_log").upsert(rows_to_upsert, on_conflict="event_key").execute()
        except Exception as e:
            print(f"[Wolves] signal_log upsert failed: {e}")

    if fresh_events:
        try:
            supabase.from_("wolves_fresh_events").upsert(fresh_events, on_conflict="event_key").execute()
        except Exception as e:
            print(f"[Wolves] fresh_events upsert failed: {e}")

    print(f"[Wolves] Live tick: {scanned} scanned, {skipped} skipped, {errored} errored, {wolves_found} wolf hits, {len(fresh_events)} fresh events")
    return {"scanned": scanned, "skipped": skipped, "errored": errored, "wolves_found": wolves_found, "fresh_events": len(fresh_events)}


def get_todays_wolves() -> dict:
    """Read-only -- today's Wolves log. Cheap Supabase read, no Kite calls."""
    supabase = get_supabase()
    today_ist = datetime.now(IST).date().isoformat()
    rows = supabase.from_("wolves_signal_log") \
        .select("*") \
        .eq("scan_date", today_ist) \
        .order("scan_time", desc=True) \
        .execute().data or []
    return {"date": today_ist, "wolves": rows}


def get_todays_fresh_events() -> dict:
    """Read-only -- today's standalone Fresh Events (ADX Cross, RSI Cross,
    ADX Full Alignment, Grandfather-Father-Son), separate from the full
    Wolves confluence log above. Cheap Supabase read, no Kite calls."""
    supabase = get_supabase()
    today_ist = datetime.now(IST).date().isoformat()
    rows = supabase.from_("wolves_fresh_events") \
        .select("*") \
        .eq("scan_date", today_ist) \
        .order("scan_time", desc=True) \
        .execute().data or []
    return {"date": today_ist, "events": rows}
