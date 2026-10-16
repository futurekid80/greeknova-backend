"""
Wolves Scanner -- Oct 16 2026.

Ported from Manish's personal "FYERS ADX Equity Decision Scanner" (a
~7000-line Streamlit app he ran locally against the Fyers API, coined
"3 Wolves" for its core signal), so he can discard the local tool and run
the same scan inside GreekNova against Kite data instead.

THE WOLVES RULE (ported verbatim from the live logic in his file --
`build_wolves_events_today` / `evaluate_timeframe` v2, the Oct 2026 version
after ~25 rounds of iteration superseded the earlier ones in the same
file):
  1. A timeframe's ADX freshly crosses above 25 with +DI > -DI (trend
     just activated) -- "adx_cross_bull".
  2. Hourly RSI > 60 (momentum confirms).
  3. Hourly price breaks the upper Bollinger Band, OR is already above it
     with a recent compression-breakout -- "bb_upper_cross" /
     "bb_compression_breakout" / "bb_bull".
All three together on a symbol = a Wolf. Which timeframe triggered the ADX
cross sets the tier: Hourly -> Scout Wolf, Daily -> Alpha Wolf,
Weekly/Monthly -> Apex Wolf (matching his own naming).

Only the data-fetch layer changed (Kite historical_data instead of Fyers'
API) -- the indicator math and the Wolves rule itself are unchanged, so
this should fire on the same setups his local scanner would have flagged.
"""
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz

from utils.db import get_supabase

IST = pytz.timezone("Asia/Kolkata")

# Indicator lengths -- same defaults as the ported app (ADX 14, RSI 14, BB 20/2).
ADX_LEN = 14
RSI_LEN = 14
BB_LEN = 20
BB_STD = 2.0
ADX_THRESHOLD = 25.0
RSI_BULL = 60.0

# Index spot tokens -- same ones GreekNova's CPR job already uses for
# kite.historical_data() on indices (they aren't in kite.instruments("NSE")
# the way stocks are).
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
    """ADX/+DI/-DI/RSI/Bollinger Bands + the BB-width compression layer --
    ported verbatim from the live version of this function in the source
    file (the v2/"_v28" one, which added BB Width Percentile/Compressed on
    top of the original add_indicators)."""
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
    """Just what the Wolves rule needs from one timeframe's latest bar --
    adx_cross_bull, plus the raw ADX/RSI/close for display. Ported from
    the live evaluate_timeframe (the fields `build_wolves_events_today`
    actually reads: adx_cross_bull, rsi_bull, bb_upper_cross,
    bb_compression_breakout, bb_bull)."""
    empty = {
        "timeframe": timeframe, "adx_cross_bull": False, "rsi_bull": False,
        "bb_upper_cross": False, "bb_compression_breakout": False, "bb_bull": False,
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
    upper = safe_num(latest.get("BB Upper"))
    lower = safe_num(latest.get("BB Lower"))
    prev_upper = safe_num(prev.get("BB Upper"))
    bb_width_pct = safe_num(latest.get("BB Width Pct"))
    compressed_recent = bool(df["BB Compressed"].tail(12).fillna(False).astype(bool).any()) if "BB Compressed" in df.columns else False

    adx_cross = bool(prev_adx <= ADX_THRESHOLD and adx > ADX_THRESHOLD)
    adx_cross_bull = bool(adx_cross and plus_di > minus_di)
    bb_upper_cross = bool(prev_close <= prev_upper and close > upper)
    bb_compression_breakout = bool(compressed_recent and bb_upper_cross)
    bb_bull = bool(close > upper)

    return {
        "timeframe": timeframe,
        "adx_cross_bull": adx_cross_bull,
        "rsi_bull": bool(rsi > RSI_BULL),
        "bb_upper_cross": bb_upper_cross,
        "bb_compression_breakout": bb_compression_breakout,
        "bb_bull": bb_bull,
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


def scan_wolves(symbols: list[str] | None = None) -> dict:
    """Runs the full Wolves scan and upserts hits into wolves_signal_log.
    Returns a summary dict. Call this from a scheduled job, not per page
    load -- it's ~2 Kite historical_data calls per symbol (daily + 60min),
    which adds up across the F&O universe."""
    from services.kite_auth import get_kite_client
    from api.iv_analysis import SYMBOLS as _ALL_SYMBOLS

    universe = symbols or list(_ALL_SYMBOLS)
    kite = get_kite_client()
    supabase = get_supabase()
    token_map = _build_token_map(kite, universe)

    today_ist = datetime.now(IST).date()
    daily_from = (today_ist - timedelta(days=1100)).isoformat()  # ~3yr, enough for weekly/monthly BB-width percentile
    hourly_from = (today_ist - timedelta(days=90)).isoformat()
    to_date = today_ist.isoformat()

    rows_to_upsert = []
    scanned, skipped, errored = 0, 0, 0

    for symbol in universe:
        token = token_map.get(symbol)
        if not token:
            skipped += 1
            continue
        try:
            daily_candles = kite.historical_data(instrument_token=token, from_date=daily_from, to_date=to_date, interval="day", continuous=False, oi=False)
            time.sleep(0.34)  # ~3 req/sec, matching Kite Connect's historical-data rate limit
            hourly_candles = kite.historical_data(instrument_token=token, from_date=hourly_from, to_date=to_date, interval="60minute", continuous=False, oi=False)
            time.sleep(0.34)
        except Exception as e:
            print(f"[Wolves] {symbol}: historical_data failed -- {e}")
            errored += 1
            continue

        daily_df = _candles_to_df(daily_candles)
        hourly_df = _candles_to_df(hourly_candles)
        if daily_df.empty or hourly_df.empty:
            skipped += 1
            continue

        daily_ind = add_indicators(daily_df)
        weekly_ind = add_indicators(resample_ohlcv(daily_df, "W-FRI"))
        monthly_ind = add_indicators(resample_ohlcv(daily_df, "ME"))
        hourly_ind = add_indicators(hourly_df)

        hourly_eval = evaluate_timeframe(hourly_ind, "Hourly")
        hourly_gate_ok = hourly_eval["rsi_bull"] and (hourly_eval["bb_upper_cross"] or hourly_eval["bb_compression_breakout"] or hourly_eval["bb_bull"])
        if not hourly_gate_ok:
            scanned += 1
            continue

        for tf_name, tf_df in [("Hourly", hourly_ind), ("Daily", daily_ind), ("Weekly", weekly_ind), ("Monthly", monthly_ind)]:
            tf_eval = hourly_eval if tf_name == "Hourly" else evaluate_timeframe(tf_df, tf_name)
            if not tf_eval["adx_cross_bull"]:
                continue
            event_key = "|".join([today_ist.isoformat(), symbol, "Bullish", WOLF_TIER[tf_name], tf_name])
            rows_to_upsert.append({
                "event_key": event_key,
                "scan_date": today_ist.isoformat(),
                "scan_time": datetime.now(IST).strftime("%H:%M:%S"),
                "symbol": symbol,
                "direction": "Bullish",
                "wolf_tier": WOLF_TIER[tf_name],
                "trigger_timeframe": tf_name,
                "close": round(hourly_eval["close"], 2) if not pd.isna(hourly_eval["close"]) else None,
                "adx": round(tf_eval["adx"], 2) if not pd.isna(tf_eval["adx"]) else None,
                "hourly_rsi": round(hourly_eval["rsi"], 2) if not pd.isna(hourly_eval["rsi"]) else None,
                "di_gap": round(tf_eval["di_gap"], 2) if not pd.isna(tf_eval["di_gap"]) else None,
                "bb_width_pct": round(hourly_eval["bb_width_pct"] * 100, 1) if not pd.isna(hourly_eval["bb_width_pct"]) else None,
                "meaning": f"{WOLF_TIER[tf_name]}: {tf_name} ADX fresh cross above 25 with hourly RSI>60 and hourly upper-BB breakout.",
            })
        scanned += 1

    if rows_to_upsert:
        try:
            supabase.from_("wolves_signal_log").upsert(rows_to_upsert, on_conflict="event_key").execute()
        except Exception as e:
            print(f"[Wolves] upsert failed: {e}")

    return {"scanned": scanned, "skipped": skipped, "errored": errored, "wolves_found": len(rows_to_upsert)}


def get_todays_wolves() -> dict:
    """Read-only -- today's Wolves log, for the frontend to poll. Cheap
    Supabase read, no Kite calls, so the page can hit this on every load."""
    supabase = get_supabase()
    today_ist = datetime.now(IST).date().isoformat()
    rows = supabase.from_("wolves_signal_log") \
        .select("*") \
        .eq("scan_date", today_ist) \
        .order("scan_time", desc=True) \
        .execute().data or []
    return {"date": today_ist, "wolves": rows}
