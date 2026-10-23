"""
Wolves Scanner -- Oct 16 2026, v4 ("maximum fidelity" pass).

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
  _v32_wolves_tier in his file.

ARCHITECTURE (v2 -- rewritten after the v1 "full re-fetch every scan" design
turned out too slow to run live): Daily/Weekly/Monthly only change once a
day, so their ADX/BB state is computed ONCE after close (refresh_tf_cache)
and cached in wolves_tf_cache. Live ticks during market hours
(run_live_tick) only re-fetch Hourly candles -- one Kite call per symbol
instead of two, and a ~30-day window instead of years -- then combine with
the cached Daily/Weekly/Monthly flags.

v3 added the standalone "Fresh Events" layer Manish actually relies on day
to day -- he prioritizes a lone ADX Cross per timeframe to mend/amend
positions, confluence or not: standalone ADX Cross, RSI Cross, ADX Full
Alignment, Grandfather/Father/Son (named RSI system, confirmed against its
public description since it isn't in Manish's source file -- RSI(14),
bullish/bearish mirror per his confirmation).

v4 ("bring things to maximum") adds, all ported from his source file's
`build_watchlist` (line ~3568, "V2.7 decision watchlist with sector and
explicit ADX active/fresh fields"), `_v32_decision_score`,
`_v32_signal_freshness`, `_v32_sector_tailwind`, `_v32_bb_quality_from_eval`
and the base `evaluate_timeframe` (quality_bull/core_bull/adx_bull etc.):

  - Standalone BB events (Compression Breakout/Breakdown, plain Breakout/
    Breakdown), per timeframe, mirroring the ADX/RSI Cross pattern.
  - Decision Watchlist: Rank (A+/A/B/C), Confidence (High/Medium/Low),
    Decision Score (0-100) per symbol+direction, aggregating ADX-active/
    RSI/BB state and fresh triggers across all 4 timeframes -- written to
    a new `wolves_watchlist` table, read via GET /wolves/watchlist.
  - Sector Tailwind: whether other names in the same sector agree with this
    direction this tick (ported sector map, _v32_sector_tailwind logic).
  - Signal Freshness / Times Seen / First Seen / Last Seen on fresh events:
    read-merge-write against today's existing rows instead of a plain
    upsert, so a repeated event accumulates a count instead of just
    refreshing scan_time.
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

TF_ORDER = ["Hourly", "Daily", "Weekly", "Monthly"]
IMPORTANT_TFS = ["Daily", "Weekly", "Monthly"]

# Replaces the first-pass sector map (ported from the old builtin_sector_map
# in his uploaded source file). Manish sent a live CSV export of his local
# tool's Fresh Events on Oct 16 -- its Sector column uses NSE's standard
# sectoral/industry taxonomy (e.g. "Financial Services" as one umbrella for
# banks/NBFCs/insurance, "Fast Moving Consumer Goods" spelled out, "Realty"
# not "Real Estate", "Oil Gas & Consumable Fuels", "Automobile and Auto
# Components"), which is NOT the same taxonomy as the old ad-hoc dict --
# e.g. his CSV has FEDERALBNK/SBIN/INDUSINDBK under "Financial Services",
# not a separate "Banks" bucket, and ADANIENT under "Metals & Mining", not
# "Infrastructure". ~70 symbols below are ground-truthed directly against
# that CSV; the rest of the F&O universe (_FALLBACK_SYMBOLS in
# api/iv_analysis.py) is classified the same way from general knowledge of
# NSE's sectoral classification, so treat those as best-effort, not
# confirmed. Any symbol still missing falls back to "Unclassified", same as
# his tool (his own CSV left a few gaps too -- NBCC, KAYNES, ICICIPRULI,
# UNOMINDA all showed "Unclassified" in his export despite being
# classifiable, so a handful of mismatches on those specific names is
# expected and not a bug in this port).
SECTOR_MAP = {
    "360ONE": "Financial Services", "ABB": "Capital Goods", "ABCAPITAL": "Financial Services",
    "ADANIENSOL": "Power & Utilities", "ADANIENT": "Metals & Mining", "ADANIGREEN": "Power & Utilities",
    "ADANIPORTS": "Services", "ADANIPOWER": "Power & Utilities", "ALKEM": "Pharmaceuticals & Healthcare",
    "AMBER": "Consumer Durables", "AMBUJACEM": "Construction Materials", "ANANDRATHI": "Financial Services",
    "ANGELONE": "Financial Services", "APLAPOLLO": "Capital Goods", "APOLLOHOSP": "Pharmaceuticals & Healthcare",
    "ASHOKLEY": "Automobile and Auto Components", "ASIANPAINT": "Consumer Durables", "ASTRAL": "Capital Goods",
    "ATHERENERG": "Automobile and Auto Components", "AUBANK": "Financial Services", "AUROPHARMA": "Pharmaceuticals & Healthcare",
    "AXISBANK": "Financial Services", "BAJAJ-AUTO": "Automobile and Auto Components", "BAJAJFINSV": "Financial Services",
    "BAJAJHLDNG": "Financial Services", "BAJFINANCE": "Financial Services", "BANDHANBNK": "Financial Services",
    "BANKBARODA": "Financial Services", "BANKINDIA": "Financial Services", "BDL": "Capital Goods",
    "BEL": "Capital Goods", "BHARATFORG": "Automobile and Auto Components", "BHARTIARTL": "Telecommunication",
    "BHEL": "Capital Goods", "BIOCON": "Pharmaceuticals & Healthcare", "BLUESTARCO": "Consumer Durables",
    "BOSCHLTD": "Automobile and Auto Components", "BPCL": "Oil Gas & Consumable Fuels", "BRITANNIA": "Fast Moving Consumer Goods",
    "BSE": "Financial Services", "CAMS": "Financial Services", "CANBK": "Financial Services",
    "CDSL": "Financial Services", "CGPOWER": "Capital Goods", "CHOLAFIN": "Financial Services",
    "CIPLA": "Pharmaceuticals & Healthcare", "COALINDIA": "Metals & Mining", "COCHINSHIP": "Capital Goods",
    "COFORGE": "Information Technology", "COLPAL": "Fast Moving Consumer Goods", "CONCOR": "Services",
    "CROMPTON": "Consumer Durables", "CUMMINSIND": "Capital Goods", "DABUR": "Fast Moving Consumer Goods",
    "DELHIVERY": "Services", "DIVISLAB": "Pharmaceuticals & Healthcare", "DIXON": "Consumer Durables",
    "DLF": "Realty", "DMART": "Consumer Services", "DRREDDY": "Pharmaceuticals & Healthcare",
    "EICHERMOT": "Automobile and Auto Components", "ETERNAL": "Consumer Services", "FEDERALBNK": "Financial Services",
    "FORCEMOT": "Automobile and Auto Components", "FORTIS": "Pharmaceuticals & Healthcare", "GAIL": "Oil Gas & Consumable Fuels",
    "GLENMARK": "Pharmaceuticals & Healthcare", "GMRAIRPORT": "Services", "GODFRYPHLP": "Fast Moving Consumer Goods",
    "GODREJCP": "Fast Moving Consumer Goods", "GODREJPROP": "Realty", "GRASIM": "Diversified",
    "GVT&D": "Capital Goods", "HAL": "Capital Goods", "HAVELLS": "Consumer Durables",
    "HCLTECH": "Information Technology", "HDFCAMC": "Financial Services", "HDFCBANK": "Financial Services",
    "HDFCLIFE": "Financial Services", "HEROMOTOCO": "Automobile and Auto Components", "HINDALCO": "Metals & Mining",
    "HINDPETRO": "Oil Gas & Consumable Fuels", "HINDUNILVR": "Fast Moving Consumer Goods", "HINDZINC": "Metals & Mining",
    "HYUNDAI": "Automobile and Auto Components", "ICICIBANK": "Financial Services", "ICICIGI": "Financial Services",
    "ICICIPRULI": "Financial Services", "IDEA": "Telecommunication", "IDFCFIRSTB": "Financial Services",
    "IEX": "Financial Services", "INDHOTEL": "Consumer Services", "INDIANB": "Financial Services",
    "INDIGO": "Services", "INDUSINDBK": "Financial Services", "INDUSTOWER": "Telecommunication",
    "INFY": "Information Technology", "INOXWIND": "Capital Goods", "IOC": "Oil Gas & Consumable Fuels",
    "IREDA": "Financial Services", "IRFC": "Financial Services", "ITC": "Fast Moving Consumer Goods",
    "JINDALSTEL": "Metals & Mining", "JIOFIN": "Financial Services", "JSWENERGY": "Power & Utilities",
    "JSWSTEEL": "Metals & Mining", "JUBLFOOD": "Consumer Services", "KALYANKJIL": "Consumer Services",
    "KEI": "Capital Goods", "KFINTECH": "Financial Services", "KOTAKBANK": "Financial Services",
    "KPITTECH": "Information Technology", "LAURUSLABS": "Pharmaceuticals & Healthcare", "LICHSGFIN": "Financial Services",
    "LICI": "Financial Services", "LODHA": "Realty", "LT": "Construction",
    "LTF": "Financial Services", "LTM": "Information Technology", "LUPIN": "Pharmaceuticals & Healthcare",
    "M&M": "Automobile and Auto Components", "MAHABANK": "Financial Services", "MANAPPURAM": "Financial Services",
    "MANKIND": "Pharmaceuticals & Healthcare", "MARICO": "Fast Moving Consumer Goods", "MARUTI": "Automobile and Auto Components",
    "MAXHEALTH": "Pharmaceuticals & Healthcare", "MAZDOCK": "Capital Goods", "MCX": "Financial Services",
    "MFSL": "Financial Services", "MOTHERSON": "Automobile and Auto Components", "MOTILALOFS": "Financial Services",
    "MPHASIS": "Information Technology", "MUTHOOTFIN": "Financial Services", "NAM-INDIA": "Financial Services",
    "NATIONALUM": "Metals & Mining", "NAUKRI": "Consumer Services", "NESTLEIND": "Fast Moving Consumer Goods",
    "NHPC": "Power & Utilities", "NMDC": "Metals & Mining", "NTPC": "Power & Utilities",
    "NYKAA": "Consumer Services", "OBEROIRLTY": "Realty", "OFSS": "Information Technology",
    "OIL": "Oil Gas & Consumable Fuels", "ONGC": "Oil Gas & Consumable Fuels", "PAGEIND": "Textiles",
    "PATANJALI": "Fast Moving Consumer Goods", "PAYTM": "Financial Services", "PERSISTENT": "Information Technology",
    "PETRONET": "Oil Gas & Consumable Fuels", "PFC": "Financial Services", "PGEL": "Consumer Durables",
    "PHOENIXLTD": "Realty", "PIDILITIND": "Chemicals", "PIIND": "Chemicals",
    "PNB": "Financial Services", "PNBHOUSING": "Financial Services", "POLICYBZR": "Financial Services",
    "POLYCAB": "Capital Goods", "POWERGRID": "Power & Utilities", "POWERINDIA": "Capital Goods",
    "PREMIERENE": "Power & Utilities", "PRESTIGE": "Realty", "RADICO": "Fast Moving Consumer Goods",
    "RBLBANK": "Financial Services", "RECLTD": "Financial Services", "RELIANCE": "Oil Gas & Consumable Fuels",
    "RVNL": "Construction", "SAGILITY": "Services", "SAIL": "Metals & Mining",
    "SBICARD": "Financial Services", "SBILIFE": "Financial Services", "SBIN": "Financial Services",
    "SHREECEM": "Construction Materials", "SHRIRAMFIN": "Financial Services", "SIEMENS": "Capital Goods",
    "SOLARINDS": "Chemicals", "SONACOMS": "Automobile and Auto Components", "SRF": "Chemicals",
    "SUNPHARMA": "Pharmaceuticals & Healthcare", "SUPREMEIND": "Capital Goods", "SUZLON": "Capital Goods",
    "SWIGGY": "Consumer Services", "TATACONSUM": "Fast Moving Consumer Goods", "TATAELXSI": "Information Technology",
    "TATAPOWER": "Power & Utilities", "TATASTEEL": "Metals & Mining", "TCS": "Information Technology",
    "TECHM": "Information Technology", "TIINDIA": "Automobile and Auto Components", "TITAN": "Consumer Durables",
    "TMPV": "Automobile and Auto Components", "TORNTPHARM": "Pharmaceuticals & Healthcare", "TRENT": "Consumer Services",
    "TVSMOTOR": "Automobile and Auto Components", "UJJIVANSFB": "Financial Services", "ULTRACEMCO": "Construction Materials",
    "UNIONBANK": "Financial Services", "UNITDSPR": "Fast Moving Consumer Goods", "UNOMINDA": "Automobile and Auto Components",
    "UPL": "Chemicals", "VBL": "Fast Moving Consumer Goods", "VEDL": "Metals & Mining",
    "VOLTAS": "Consumer Durables", "WAAREEENER": "Capital Goods", "WIPRO": "Information Technology",
    "YESBANK": "Financial Services", "ZYDUSLIFE": "Pharmaceuticals & Healthcare",
    "IGL": "Oil Gas & Consumable Fuels",
}

# Ported from the Mode/Priority columns visible in his Fresh Events CSV
# export. Mode buckets by timeframe (how far out the signal looks); Priority
# is his tool's own ranking -- confirmed from the CSV: ADX Cross and BB
# Compression events share priority 1 with Wolves, RSI sits at 3, and a
# plain (non-compression) BB band touch is the lowest-priority 4. This
# matches what Manish said he actually prioritizes (ADX breakout first).
MODE_BY_TF = {"Hourly": "Intraday Event", "Daily": "Swing Event", "Weekly": "Swing Event", "Monthly": "Context Event", "All": "Swing Event"}
PRIORITY_MAP = {
    "ADX Cross 25": 1, "ADX Full Alignment": 1,
    "BB Compression Breakout": 1, "BB Compression Breakdown": 1,
    "Grandfather-Father-Son Setup": 2, "Grandfather-Father-Son Exit": 2,
    "RSI 60 Breakout": 3, "RSI 40 Breakdown": 3,
    "BB Upper Breakout": 4, "BB Lower Breakdown": 4,
}


def sector_for(symbol: str) -> str:
    return SECTOR_MAP.get(symbol, "Unclassified")


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
    """Per-timeframe evaluation. Keeps the v2/v3 "cross" fields
    (adx_cross_bull/bear, rsi_cross_bull/bear, bb_upper_cross/bb_lower_cross,
    bb_compression_breakout/breakdown) AND adds the base-evaluate_timeframe
    state/quality fields from his source file (adx_bull/bear, adx_fresh,
    rsi_fresh, bb_fresh, core_bull/bear, quality_bull/bear) needed for the
    Decision Watchlist's Rank/Confidence logic."""
    empty = {
        "timeframe": timeframe,
        "adx_cross_bull": False, "adx_cross_bear": False,
        "rsi_bull": False, "rsi_bear": False,
        "rsi_cross_bull": False, "rsi_cross_bear": False,
        "bb_upper_cross": False, "bb_lower_cross": False,
        "bb_compression_breakout": False, "bb_compression_breakdown": False,
        "bb_bull": False, "bb_bear": False,
        "adx_bull": False, "adx_bear": False, "adx_fresh": False,
        "rsi_fresh": False, "bb_fresh": False,
        "core_bull": False, "core_bear": False,
        "quality_bull": False, "quality_bear": False,
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

    rsi_bull_state = bool(rsi > RSI_BULL)
    rsi_bear_state = bool(rsi < RSI_BEAR)
    rsi_cross_bull = bool(prev_rsi <= RSI_BULL and rsi > RSI_BULL)
    rsi_cross_bear = bool(prev_rsi >= RSI_BEAR and rsi < RSI_BEAR)

    bb_upper_cross = bool(prev_close <= prev_upper and close > upper)
    bb_lower_cross = bool(prev_close >= prev_lower and close < lower)
    bb_compression_breakout = bool(compressed_recent and bb_upper_cross)
    bb_compression_breakdown = bool(compressed_recent and bb_lower_cross)
    bb_bull = bool(close > upper)
    bb_bear = bool(close < lower)

    # State/quality fields -- ported from the base evaluate_timeframe
    # (source line ~571). adx_bull/bear is the ongoing "ADX active" state
    # (threshold + DI direction), distinct from adx_cross_* which only fires
    # on the bar it crosses. This is what Rank/Decision Score need.
    adx_bull = bool(adx > ADX_THRESHOLD and plus_di > minus_di)
    adx_bear = bool(adx > ADX_THRESHOLD and minus_di > plus_di)
    adx_fresh = adx_cross
    rsi_fresh = bool(rsi_cross_bull or rsi_cross_bear)
    bb_fresh = bool(bb_upper_cross or bb_lower_cross)
    core_bull = bool(adx_bull and rsi_bull_state)
    core_bear = bool(adx_bear and rsi_bear_state)
    quality_bull = bool(adx_bull and rsi_bull_state and bb_bull)
    quality_bear = bool(adx_bear and rsi_bear_state and bb_bear)

    return {
        "timeframe": timeframe,
        "adx_cross_bull": adx_cross_bull, "adx_cross_bear": adx_cross_bear,
        "rsi_bull": rsi_bull_state, "rsi_bear": rsi_bear_state,
        "rsi_cross_bull": rsi_cross_bull, "rsi_cross_bear": rsi_cross_bear,
        "bb_upper_cross": bb_upper_cross, "bb_lower_cross": bb_lower_cross,
        "bb_compression_breakout": bb_compression_breakout, "bb_compression_breakdown": bb_compression_breakdown,
        "bb_bull": bb_bull, "bb_bear": bb_bear,
        "adx_bull": adx_bull, "adx_bear": adx_bear, "adx_fresh": adx_fresh,
        "rsi_fresh": rsi_fresh, "bb_fresh": bb_fresh,
        "core_bull": core_bull, "core_bear": core_bear,
        "quality_bull": quality_bull, "quality_bear": quality_bear,
        "close": close, "adx": adx, "rsi": rsi,
        "di_gap": abs(plus_di - minus_di) if not (pd.isna(plus_di) or pd.isna(minus_di)) else np.nan,
        "bb_width_pct": bb_width_pct,
    }


def _join_tfs(tfs: list[str]) -> str:
    order = {tf: i for i, tf in enumerate(TF_ORDER)}
    clean = sorted(set(tf for tf in tfs if tf in TF_ORDER), key=lambda x: order.get(x, 99))
    return " + ".join(clean) if clean else "—"


def _trigger_text(direction: str, fresh_adx: list[str], fresh_rsi: list[str], fresh_bb: list[str]) -> tuple[str, str]:
    """Ported from _v15_trigger_text."""
    parts, primary = [], []
    if fresh_adx:
        parts.append(f"ADX:{_join_tfs(fresh_adx)}")
        primary.append("ADX")
    if fresh_rsi:
        parts.append(f"RSI:{_join_tfs(fresh_rsi)}")
        primary.append("RSI")
    if fresh_bb:
        parts.append(f"BB:{_join_tfs(fresh_bb)}")
        primary.append("BB")
    if not parts:
        return "—", "Continuation"
    side_word = "Breakout" if direction == "Bullish" else "Breakdown"
    if len(primary) == 1:
        return " | ".join(parts), f"Fresh {primary[0]} {side_word}"
    return " | ".join(parts), f"Fresh Mixed {side_word}"


def decision_score(rank: str, event_type: str, tfs: str, confidence: str,
                    adx_active: bool = False, fresh_adx_trigger: bool = False,
                    sector_tailwind: str = "") -> int:
    """Ported verbatim from _v32_decision_score."""
    score = {"A+": 76, "A": 66, "B": 50, "C": 30}.get(rank, 40)
    if "Wolves" in event_type:
        score = 90
        if "Weekly" in event_type or "Monthly" in event_type:
            score += 6
        elif "Daily" in event_type:
            score += 3
    elif "Compression" in event_type:
        score += 18
    elif "ADX Cross" in event_type:
        score += 16
    elif "RSI" in event_type:
        score += 8
    elif "BB" in event_type:
        score += 7
    score += {"High": 8, "Medium": 4, "Low": 0}.get(confidence, 0)
    score += 5 if "Weekly" in tfs else 0
    score += 5 if "Monthly" in tfs else 0
    score += 4 if "Daily" in tfs else 0
    score += 3 if "Hourly" in tfs else 0
    if adx_active:
        score += 6
    if fresh_adx_trigger:
        score += 8
    if sector_tailwind == "Sector Tailwind":
        score += 5
    elif sector_tailwind == "Against Sector":
        score -= 6
    return int(max(0, min(100, score)))


def signal_freshness(times_seen: int, first_seen: str, last_seen: str) -> str:
    """Ported verbatim from _v32_signal_freshness."""
    if times_seen is None or times_seen <= 1:
        return "Fresh Now"
    if first_seen and last_seen and first_seen != last_seen:
        return "Still Active"
    return "Seen Earlier Today"


def sector_tailwind_map(rows: list[dict]) -> dict:
    """Ported from _v32_sector_tailwind, adapted to work over a list of
    this-tick watchlist rows instead of a DataFrame. Returns
    {(sector, direction): "Sector Tailwind" | "Against Sector" | "Solo / Mixed Sector"}
    keyed by each row's own (sector, direction)."""
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["sector"], r["direction"])
        counts[key] = counts.get(key, 0) + 1

    def bull_bear(sector: str) -> tuple[int, int]:
        return counts.get((sector, "Bullish"), 0), counts.get((sector, "Bearish"), 0)

    result = {}
    for r in rows:
        sector, direction = r["sector"], r["direction"]
        bull, bear = bull_bear(sector)
        if direction == "Bullish" and bull >= max(2, bear + 1):
            label = "Sector Tailwind"
        elif direction == "Bearish" and bear >= max(2, bull + 1):
            label = "Sector Tailwind"
        elif direction == "Bullish" and bear >= bull + 2:
            label = "Against Sector"
        elif direction == "Bearish" and bull >= bear + 2:
            label = "Against Sector"
        else:
            label = "Solo / Mixed Sector"
        result[(r["symbol"], direction)] = label
    return result


def bb_quality_from_eval(e: dict, direction: str) -> str:
    """Ported from _v32_bb_quality_from_eval."""
    if direction == "Bullish":
        if e.get("bb_compression_breakout"):
            return "Clean Compression Breakout"
        if e.get("bb_upper_cross"):
            return "Band Breakout"
        if e.get("bb_bull"):
            return "Band Close Only"
    if direction == "Bearish":
        if e.get("bb_compression_breakdown"):
            return "Clean Compression Breakdown"
        if e.get("bb_lower_cross"):
            return "Band Breakdown"
        if e.get("bb_bear"):
            return "Band Close Only"
    return "—"


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


def _cmp_prices_candles(supabase, symbol: str, from_date: str, to_date: str, freq: str) -> list[dict]:
    """BUG FIX (Oct 23 2026): kite.historical_data() has no real permission
    on this API key (Manish's Kite Connect app is personal/multiuser, not
    the public Connect API with the Historical Data add-on) -- it started
    rejecting every symbol with "Insufficient permission for that call" on
    2026-10-22, silently zeroing out both the daily/weekly/monthly cache
    job and the hourly live tick (217 errored, 0 scanned). Same root cause
    and same fix shape as api/cpr.py's prev-close fix: reconstruct OHLC
    candles from cmp_prices, which is populated independently every ~5 min
    during market hours via kite.quote() (no Historical Data permission
    needed). `freq` is a pandas resample alias -- "D" for daily, "h" for
    hourly. Returns the same shape _candles_to_df()/kite.historical_data()
    produce (list of {date, open, high, low, close, volume} dicts) so every
    caller downstream is unchanged.
    """
    try:
        to_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
        rows = supabase.from_("cmp_prices")\
            .select("cmp, timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{from_date}T00:00:00+00:00")\
            .lt("timestamp", to_dt.strftime("%Y-%m-%dT00:00:00+00:00"))\
            .order("timestamp")\
            .limit(50000)\
            .execute()
    except Exception as e:
        print(f"[Wolves] cmp_prices fetch {symbol}: {e}")
        return []

    data = rows.data or []
    if not data:
        return []

    df = pd.DataFrame(data)
    df["cmp"] = pd.to_numeric(df["cmp"], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.dropna(subset=["cmp"]).set_index("timestamp").sort_index()
    if df.empty:
        return []

    ohlc = df["cmp"].resample(freq).ohlc().dropna(how="all")
    ohlc = ohlc.dropna(subset=["open", "high", "low", "close"])
    if ohlc.empty:
        return []
    ohlc["volume"] = 0
    ohlc = ohlc.reset_index().rename(columns={"timestamp": "date"})
    return ohlc.to_dict("records")


# ---------------------------------------------------------------------------
# EOD: refresh the Daily/Weekly/Monthly ADX/BB state cache (once per day)
# ---------------------------------------------------------------------------

def refresh_tf_cache(symbols: list[str] | None = None) -> dict:
    """Fetches daily candles once per symbol (years of history, native Kite
    'day' interval -- one call), derives Weekly/Monthly via resample, and
    caches each timeframe's full evaluate_timeframe state (ADX cross +
    active state, RSI cross, BB cross/compression/state). Live ticks read
    this instead of re-fetching daily/weekly/monthly every 5 minutes."""
    from services.kite_auth import get_kite_client
    from api.iv_analysis import SYMBOLS as _ALL_SYMBOLS

    universe = symbols or list(_ALL_SYMBOLS)
    kite = get_kite_client()
    supabase = get_supabase()
    token_map = _build_token_map(kite, universe)

    today_ist = datetime.now(IST).date()
    daily_from = (today_ist - timedelta(days=2000)).isoformat()  # ~8yr -- gives the monthly resample a real percentile population
    to_date = today_ist.isoformat()

    # BUG FIX (Oct 23 2026) cont'd: when Manish's dedicated historical-only
    # Kite app ("Trading Bot", separate from the main login) is configured
    # and its Historical Data subscription is active, prefer it for the
    # full years-deep daily fetch -- this is what actually makes
    # Weekly/Monthly (Apex Wolf) trustworthy again, since cmp_prices alone
    # only has ~6 months of history. Falls back to cmp_prices reconstruction
    # per-symbol if the historical client is unavailable or a call fails,
    # so this can never take the whole job down.
    historical_kite = None
    try:
        from services.kite_auth import get_historical_kite_client
        historical_kite = get_historical_kite_client()
        print("[Wolves] Using Trading Bot historical client for full-depth daily candles")
    except Exception as e:
        print(f"[Wolves] Historical client unavailable ({e}) -- using cmp_prices reconstruction instead")

    rows, cached, skipped, errored = [], 0, 0, 0

    for symbol in universe:
        token = token_map.get(symbol)
        if not token:
            skipped += 1
            continue

        daily_candles = None
        if historical_kite:
            try:
                daily_candles = historical_kite.historical_data(instrument_token=token, from_date=daily_from, to_date=to_date, interval="day", continuous=False, oi=False)
                time.sleep(0.34)
            except Exception as e:
                print(f"[Wolves] {symbol}: historical_data (Trading Bot) failed -- {e}")
                daily_candles = None
        if not daily_candles:
            daily_candles = _cmp_prices_candles(supabase, symbol, daily_from, to_date, "D")
        if not daily_candles:
            skipped += 1
            continue

        daily_df = _candles_to_df(daily_candles)
        if daily_df.empty:
            skipped += 1
            continue

        daily_ind = add_indicators(daily_df)
        weekly_ind = add_indicators(resample_ohlcv(daily_df, "W-FRI"))
        monthly_ind = add_indicators(resample_ohlcv(daily_df, "ME"))

        # BUG FIX (Oct 23 2026) cont'd: cmp_prices only goes back to
        # 2026-05 (~6 months), plenty of daily bars for Daily ADX/RSI/BB,
        # but nowhere near enough for a real Weekly/Monthly ADX(14) --
        # that needs ~28+ bars of its own timeframe to stabilize, i.e.
        # ~28 weeks / ~28 months of history. Rather than write a noisy or
        # flat-out invalid Apex Wolf (weekly/monthly) reading over a real
        # one, only overwrite weekly_*/monthly_* once there's genuinely
        # enough resampled history (same >=30-bar bar BB Width Pct already
        # uses) -- omitted keys are left untouched by the upsert, so the
        # last real cached value (from before Historical Data broke) stays
        # until there's enough cmp_prices history to trust a fresh one.
        # Weekly should self-heal in a month or two; Monthly needs a
        # dedicated properly-subscribed Kite app to ever be timely again.
        row = {"symbol": symbol, "computed_date": today_ist.isoformat()}
        tf_sources = [("daily", daily_ind, True)]
        tf_sources.append(("weekly", weekly_ind, len(weekly_ind) >= 30))
        tf_sources.append(("monthly", monthly_ind, len(monthly_ind) >= 30))
        for tf_name, tf_df, trust_it in tf_sources:
            if not trust_it:
                continue
            e = evaluate_timeframe(tf_df, tf_name.capitalize())
            row[f"{tf_name}_adx_cross_bull"] = e["adx_cross_bull"]
            row[f"{tf_name}_adx_cross_bear"] = e["adx_cross_bear"]
            row[f"{tf_name}_close"] = round(e["close"], 2) if not pd.isna(e["close"]) else None
            row[f"{tf_name}_adx"] = round(e["adx"], 2) if not pd.isna(e["adx"]) else None
            row[f"{tf_name}_rsi"] = round(e["rsi"], 2) if not pd.isna(e["rsi"]) else None
            row[f"{tf_name}_rsi_cross_bull"] = e["rsi_cross_bull"]
            row[f"{tf_name}_rsi_cross_bear"] = e["rsi_cross_bear"]
            row[f"{tf_name}_adx_bull"] = e["adx_bull"]
            row[f"{tf_name}_adx_bear"] = e["adx_bear"]
            row[f"{tf_name}_bb_bull"] = e["bb_bull"]
            row[f"{tf_name}_bb_bear"] = e["bb_bear"]
            row[f"{tf_name}_bb_upper_cross"] = e["bb_upper_cross"]
            row[f"{tf_name}_bb_lower_cross"] = e["bb_lower_cross"]
            row[f"{tf_name}_bb_compression_breakout"] = e["bb_compression_breakout"]
            row[f"{tf_name}_bb_compression_breakdown"] = e["bb_compression_breakdown"]
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
    today_iso = today_ist.isoformat()
    hourly_from = (today_ist - timedelta(days=30)).isoformat()
    to_date = today_iso

    tf_cache_rows = supabase.from_("wolves_tf_cache").select("*").in_("symbol", universe).execute().data or []
    tf_cache = {r["symbol"]: r for r in tf_cache_rows}

    historical_kite = None
    try:
        from services.kite_auth import get_historical_kite_client
        historical_kite = get_historical_kite_client()
    except Exception as e:
        print(f"[Wolves] Historical client unavailable for live tick ({e}) -- using cmp_prices reconstruction")

    # Pull today's existing fresh_events once, for the Times-Seen merge below.
    existing_events_rows = supabase.from_("wolves_fresh_events").select(
        "event_key, first_seen, times_seen"
    ).eq("scan_date", today_iso).execute().data or []
    existing_events = {r["event_key"]: r for r in existing_events_rows}

    rows_to_upsert = []
    fresh_events = []
    watchlist_rows = []
    scanned, skipped, errored, wolves_found = 0, 0, 0, 0

    for symbol in universe:
        token = token_map.get(symbol)
        cache_row = tf_cache.get(symbol)
        if not token or not cache_row:
            skipped += 1
            continue
        hourly_candles = None
        if historical_kite:
            try:
                hourly_candles = historical_kite.historical_data(instrument_token=token, from_date=hourly_from, to_date=to_date, interval="60minute", continuous=False, oi=False)
                time.sleep(0.34)
            except Exception as e:
                print(f"[Wolves] {symbol}: hourly historical_data (Trading Bot) failed -- {e}")
                hourly_candles = None
        if not hourly_candles:
            hourly_candles = _cmp_prices_candles(supabase, symbol, hourly_from, to_date, "h")
        if not hourly_candles:
            skipped += 1
            continue

        hourly_df = _candles_to_df(hourly_candles)
        if hourly_df.empty:
            skipped += 1
            continue

        hourly_ind = add_indicators(hourly_df)
        h = evaluate_timeframe(hourly_ind, "Hourly")
        scanned += 1
        scan_time = datetime.now(IST).strftime("%H:%M:%S")
        sector = sector_for(symbol)

        def _cache_bool(prefix: str, field: str) -> bool:
            return bool(cache_row.get(f"{prefix}_{field}"))

        tf_data = {"Hourly": h}
        for tf_name, prefix in [("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly")]:
            daily_adx = cache_row.get(f"{prefix}_adx")
            daily_rsi = cache_row.get(f"{prefix}_rsi")
            adx_bull = _cache_bool(prefix, "adx_bull")
            adx_bear = _cache_bool(prefix, "adx_bear")
            rsi_bull_state = bool(daily_rsi is not None and daily_rsi > RSI_BULL)
            rsi_bear_state = bool(daily_rsi is not None and daily_rsi < RSI_BEAR)
            bb_bull = _cache_bool(prefix, "bb_bull")
            bb_bear = _cache_bool(prefix, "bb_bear")
            adx_cross_bull = _cache_bool(prefix, "adx_cross_bull")
            adx_cross_bear = _cache_bool(prefix, "adx_cross_bear")
            rsi_cross_bull = _cache_bool(prefix, "rsi_cross_bull")
            rsi_cross_bear = _cache_bool(prefix, "rsi_cross_bear")
            bb_upper_cross = _cache_bool(prefix, "bb_upper_cross")
            bb_lower_cross = _cache_bool(prefix, "bb_lower_cross")
            bb_compression_breakout = _cache_bool(prefix, "bb_compression_breakout")
            bb_compression_breakdown = _cache_bool(prefix, "bb_compression_breakdown")
            tf_data[tf_name] = {
                "adx_cross_bull": adx_cross_bull, "adx_cross_bear": adx_cross_bear,
                "rsi_cross_bull": rsi_cross_bull, "rsi_cross_bear": rsi_cross_bear,
                "bb_upper_cross": bb_upper_cross, "bb_lower_cross": bb_lower_cross,
                "bb_compression_breakout": bb_compression_breakout, "bb_compression_breakdown": bb_compression_breakdown,
                "bb_bull": bb_bull, "bb_bear": bb_bear,
                "adx_bull": adx_bull, "adx_bear": adx_bear,
                "adx_fresh": bool(adx_cross_bull or adx_cross_bear),
                "rsi_bull": rsi_bull_state, "rsi_bear": rsi_bear_state,
                "rsi_fresh": bool(rsi_cross_bull or rsi_cross_bear),
                "bb_fresh": bool(bb_upper_cross or bb_lower_cross),
                "core_bull": bool(adx_bull and rsi_bull_state), "core_bear": bool(adx_bear and rsi_bear_state),
                "quality_bull": bool(adx_bull and rsi_bull_state and bb_bull),
                "quality_bear": bool(adx_bear and rsi_bear_state and bb_bear),
                "adx": daily_adx, "rsi": daily_rsi, "close": cache_row.get(f"{prefix}_close"),
            }

        def _event(direction, event_type, tf_name, extra_meaning, rsi_val=None, adx_val=None, bb_quality="—"):
            event_key = "|".join([today_iso, symbol, direction, event_type, tf_name])
            existing = existing_events.get(event_key)
            if existing:
                first_seen = existing.get("first_seen") or scan_time
                times_seen = int(existing.get("times_seen") or 1) + 1
            else:
                first_seen = scan_time
                times_seen = 1
            freshness = signal_freshness(times_seen, first_seen, scan_time)
            confidence = {"Fresh Now": "High", "Still Active": "Medium"}.get(freshness, "Low")
            score = decision_score(rank="", event_type=event_type, tfs=tf_name, confidence=confidence)
            fresh_events.append({
                "event_key": event_key, "scan_date": today_iso, "scan_time": scan_time,
                "symbol": symbol, "direction": direction, "event_type": event_type, "timeframe": tf_name,
                "close": round(h["close"], 2) if not pd.isna(h["close"]) else None,
                "rsi": round(rsi_val, 2) if rsi_val is not None and not pd.isna(rsi_val) else None,
                "adx": round(adx_val, 2) if adx_val is not None and not pd.isna(adx_val) else None,
                "meaning": extra_meaning,
                "first_seen": first_seen, "last_seen": scan_time, "times_seen": times_seen,
                "signal_freshness": freshness, "decision_score": score,
                "bb_quality": bb_quality, "sector": sector,
                "mode": MODE_BY_TF.get(tf_name, "Swing Event"),
                "priority": PRIORITY_MAP.get(event_type, 9),
            })

        # Standalone ADX Cross + RSI Cross + BB events, independent of the
        # Wolves gate -- Manish prioritizes these alone to mend/amend positions.
        for tf_name, d in tf_data.items():
            if d["adx_cross_bull"]:
                _event("Bullish", "ADX Cross 25", tf_name, f"{tf_name} ADX freshly crossed above 25 with bullish DI.", adx_val=d["adx"])
            if d["adx_cross_bear"]:
                _event("Bearish", "ADX Cross 25", tf_name, f"{tf_name} ADX freshly crossed above 25 with bearish DI.", adx_val=d["adx"])
            if d["rsi_cross_bull"]:
                _event("Bullish", "RSI 60 Breakout", tf_name, f"{tf_name} RSI freshly crossed above 60.", rsi_val=d["rsi"])
            if d["rsi_cross_bear"]:
                _event("Bearish", "RSI 40 Breakdown", tf_name, f"{tf_name} RSI freshly crossed below 40.", rsi_val=d["rsi"])
            # Standalone BB events -- compression breakout/breakdown takes
            # precedence over a plain band cross, matching
            # _v32_bb_quality_from_eval's precedence.
            if d["bb_compression_breakout"]:
                _event("Bullish", "BB Compression Breakout", tf_name, f"{tf_name} broke the upper band out of a recent low-volatility squeeze.", bb_quality="Clean Compression Breakout")
            elif d["bb_upper_cross"]:
                _event("Bullish", "BB Upper Breakout", tf_name, f"{tf_name} closed above the upper Bollinger Band.", bb_quality="Band Breakout")
            if d["bb_compression_breakdown"]:
                _event("Bearish", "BB Compression Breakdown", tf_name, f"{tf_name} broke the lower band out of a recent low-volatility squeeze.", bb_quality="Clean Compression Breakdown")
            elif d["bb_lower_cross"]:
                _event("Bearish", "BB Lower Breakdown", tf_name, f"{tf_name} closed below the lower Bollinger Band.", bb_quality="Band Breakdown")

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
        wolf_hit_tfs = {"Bullish": [], "Bearish": []}
        for direction, gate_ok, key in [("Bullish", bull_gate, "adx_cross_bull"), ("Bearish", bear_gate, "adx_cross_bear")]:
            if not gate_ok:
                continue
            for tf_name, d in tf_data.items():
                if not d[key]:
                    continue
                wolf_hit_tfs[direction].append(tf_name)
                event_key = "|".join([today_iso, symbol, direction, WOLF_TIER[tf_name], tf_name])
                rows_to_upsert.append({
                    "event_key": event_key,
                    "scan_date": today_iso,
                    "scan_time": scan_time,
                    "symbol": symbol,
                    "direction": direction,
                    "wolf_tier": WOLF_TIER[tf_name],
                    "trigger_timeframe": tf_name,
                    "close": round(h["close"], 2) if not pd.isna(h["close"]) else None,
                    # Oct 19 2026 fix: every sibling field here is rounded
                    # before storage (close, hourly_rsi, di_gap,
                    # bb_width_pct) but this one wasn't -- the Wolves table
                    # was showing full-precision floats like
                    # 26.174705102824085 for Scout Wolf (Hourly) rows while
                    # Apex/Alpha rows happened to look clean. Match the
                    # others.
                    "adx": round(d["adx"], 2) if d.get("adx") is not None and not pd.isna(d["adx"]) else None,
                    "hourly_rsi": round(h["rsi"], 2) if not pd.isna(h["rsi"]) else None,
                    "di_gap": round(h["di_gap"], 2) if not pd.isna(h["di_gap"]) else None,
                    "bb_width_pct": round(h["bb_width_pct"] * 100, 1) if not pd.isna(h["bb_width_pct"]) else None,
                    "meaning": f"{WOLF_TIER[tf_name]}: {tf_name} ADX fresh cross above 25 ({direction.lower()}) with hourly RSI {'>60' if direction=='Bullish' else '<40'} and hourly {'upper' if direction=='Bullish' else 'lower'}-BB break.",
                })
                wolves_found += 1

        # --- Decision Watchlist: Rank / Confidence / Decision Score ---
        # Ported from build_watchlist (source line ~3568) -- aggregates
        # ADX-active/RSI/BB state and fresh triggers across all 4 timeframes
        # per symbol+direction.
        for direction in ("Bullish", "Bearish"):
            is_bull = direction == "Bullish"
            quality_tfs, core_tfs, adx_tfs, rsi_tfs, bb_tfs = [], [], [], [], []
            fresh_adx_tfs, fresh_rsi_tfs, fresh_bb_tfs = [], [], []

            for tf in TF_ORDER:
                e = tf_data.get(tf, {})
                if not e or pd.isna(e.get("close", np.nan)):
                    continue
                if is_bull:
                    if e.get("quality_bull"):
                        quality_tfs.append(tf)
                    if e.get("core_bull"):
                        core_tfs.append(tf)
                    if e.get("adx_bull"):
                        adx_tfs.append(tf)
                    if e.get("rsi_bull"):
                        rsi_tfs.append(tf)
                    if e.get("bb_bull"):
                        bb_tfs.append(tf)
                    if tf in IMPORTANT_TFS and e.get("adx_fresh") and e.get("adx_bull"):
                        fresh_adx_tfs.append(tf)
                    if tf in IMPORTANT_TFS and e.get("rsi_fresh") and e.get("rsi_bull"):
                        fresh_rsi_tfs.append(tf)
                    if tf in IMPORTANT_TFS and e.get("bb_fresh") and e.get("bb_bull"):
                        fresh_bb_tfs.append(tf)
                else:
                    if e.get("quality_bear"):
                        quality_tfs.append(tf)
                    if e.get("core_bear"):
                        core_tfs.append(tf)
                    if e.get("adx_bear"):
                        adx_tfs.append(tf)
                    if e.get("rsi_bear"):
                        rsi_tfs.append(tf)
                    if e.get("bb_bear"):
                        bb_tfs.append(tf)
                    if tf in IMPORTANT_TFS and e.get("adx_fresh") and e.get("adx_bear"):
                        fresh_adx_tfs.append(tf)
                    if tf in IMPORTANT_TFS and e.get("rsi_fresh") and e.get("rsi_bear"):
                        fresh_rsi_tfs.append(tf)
                    if tf in IMPORTANT_TFS and e.get("bb_fresh") and e.get("bb_bear"):
                        fresh_bb_tfs.append(tf)

            fresh_tfs = sorted(set(fresh_adx_tfs + fresh_rsi_tfs + fresh_bb_tfs), key=lambda x: TF_ORDER.index(x))
            fresh_signal, primary_trigger = _trigger_text(direction, fresh_adx_tfs, fresh_rsi_tfs, fresh_bb_tfs)

            if len(quality_tfs) >= 1 and len(core_tfs) >= 2:
                rank, signal_type = "A+", "ADX + RSI + BB confluence"
            elif len(core_tfs) >= 2:
                rank, signal_type = "A", "Multi-timeframe ADX + RSI confluence"
            elif fresh_tfs:
                rank, signal_type = "B", primary_trigger
            elif len(core_tfs) >= 1 or (len(adx_tfs) + len(rsi_tfs) + len(bb_tfs)) >= 2:
                rank, signal_type = "C", "Single-timeframe continuation"
            else:
                continue  # not watchlist-worthy this tick

            score_raw = len(quality_tfs) * 3 + len(core_tfs) * 2 + len(fresh_tfs)
            confidence = "High" if rank == "A+" or score_raw >= 8 else "Medium" if rank in ("A", "B") else "Low"
            is_wolf_hit = bool(wolf_hit_tfs[direction])
            event_type_for_score = "Wolves" if is_wolf_hit else signal_type
            timeframes_text = _join_tfs(core_tfs + quality_tfs + fresh_tfs)

            why_parts = []
            if adx_tfs:
                why_parts.append(f"ADX active on {_join_tfs(adx_tfs)}")
            if fresh_adx_tfs:
                why_parts.append(f"Fresh ADX trigger on {_join_tfs(fresh_adx_tfs)}")
            if rsi_tfs:
                why_parts.append(f"RSI confirms on {_join_tfs(rsi_tfs)}")
            if bb_tfs:
                why_parts.append(f"BB confirms on {_join_tfs(bb_tfs)}")
            if fresh_signal != "—":
                why_parts.append(f"Fresh trigger: {fresh_signal}")
            if is_wolf_hit:
                why_parts.append(f"Wolves confluence hit on {_join_tfs(wolf_hit_tfs[direction])}")

            latest_daily = tf_data.get("Daily", {})
            watchlist_rows.append({
                "event_key": "|".join([today_iso, symbol, direction]),
                "scan_date": today_iso, "scan_time": scan_time,
                "symbol": symbol, "sector": sector, "direction": direction,
                "rank": rank, "confidence": confidence,
                "signal_type": signal_type, "primary_trigger": primary_trigger,
                "timeframes": timeframes_text,
                "adx_status": _join_tfs(adx_tfs), "rsi_status": _join_tfs(rsi_tfs), "bb_status": _join_tfs(bb_tfs),
                "adx_active": bool(adx_tfs), "fresh_adx_trigger": bool(fresh_adx_tfs),
                "fresh_signal": fresh_signal,
                "confluence_score": score_raw,
                "close": round(safe_num(latest_daily.get("close")), 2) if latest_daily and not pd.isna(safe_num(latest_daily.get("close"))) else None,
                "adx": round(safe_num(latest_daily.get("adx")), 2) if latest_daily and not pd.isna(safe_num(latest_daily.get("adx"))) else None,
                "rsi": round(safe_num(latest_daily.get("rsi")), 2) if latest_daily and not pd.isna(safe_num(latest_daily.get("rsi"))) else None,
                "di_gap": None,
                "why": "; ".join(why_parts),
                "_event_type_for_score": event_type_for_score,
            })

    # Second pass: Sector Tailwind needs all of this tick's rows at once
    # (ported from _v32_sector_tailwind -- counts agreement within a sector).
    tailwind_lookup = sector_tailwind_map(watchlist_rows)
    for row in watchlist_rows:
        tailwind = tailwind_lookup.get((row["symbol"], row["direction"]), "Solo / Mixed Sector")
        row["sector_tailwind"] = tailwind
        row["decision_score"] = decision_score(
            rank=row["rank"], event_type=row.pop("_event_type_for_score"), tfs=row["timeframes"],
            confidence=row["confidence"], adx_active=row["adx_active"], fresh_adx_trigger=row["fresh_adx_trigger"],
            sector_tailwind=tailwind,
        )

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

    if watchlist_rows:
        try:
            # wolves_watchlist is rebuilt fresh every tick (today's rows
            # replaced), since Rank/Confidence/Decision Score are a snapshot
            # of current state, not a cumulative event log.
            supabase.from_("wolves_watchlist").delete().eq("scan_date", today_iso).execute()
            supabase.from_("wolves_watchlist").upsert(watchlist_rows, on_conflict="event_key").execute()
        except Exception as e:
            print(f"[Wolves] watchlist upsert failed: {e}")

    print(f"[Wolves] Live tick: {scanned} scanned, {skipped} skipped, {errored} errored, {wolves_found} wolf hits, {len(fresh_events)} fresh events, {len(watchlist_rows)} watchlist rows")
    return {
        "scanned": scanned, "skipped": skipped, "errored": errored,
        "wolves_found": wolves_found, "fresh_events": len(fresh_events),
        "watchlist_rows": len(watchlist_rows),
    }


def _latest_scan_date(supabase, table: str) -> str | None:
    """Oct 20 2026: shared helper so every Wolves read falls back to the
    last trading day with data instead of showing an empty scanner whenever
    today has no rows yet (pre-market, post-market, weekend/holiday) --
    same pattern already used by CPR (api/cpr.py)."""
    res = supabase.from_(table) \
        .select("scan_date") \
        .order("scan_date", desc=True) \
        .limit(1) \
        .execute()
    return res.data[0]["scan_date"] if res.data else None


def get_todays_wolves() -> dict:
    """Read-only -- today's Wolves log, falling back to the last trading
    day with data if today has none yet. Cheap Supabase read, no Kite
    calls."""
    supabase = get_supabase()
    today_ist = datetime.now(IST).date().isoformat()
    query_date = today_ist
    rows = supabase.from_("wolves_signal_log") \
        .select("*") \
        .eq("scan_date", query_date) \
        .order("scan_time", desc=True) \
        .execute().data or []
    if not rows:
        fallback_date = _latest_scan_date(supabase, "wolves_signal_log")
        if fallback_date and fallback_date != query_date:
            query_date = fallback_date
            rows = supabase.from_("wolves_signal_log") \
                .select("*") \
                .eq("scan_date", query_date) \
                .order("scan_time", desc=True) \
                .execute().data or []
    return {"date": query_date, "is_fallback": query_date != today_ist, "wolves": rows}


def get_todays_fresh_events() -> dict:
    """Read-only -- today's standalone Fresh Events (ADX Cross, RSI Cross,
    BB Breakout/Breakdown/Compression, ADX Full Alignment,
    Grandfather-Father-Son), separate from the full Wolves confluence log
    above, falling back to the last trading day with data if today has
    none yet. Cheap Supabase read, no Kite calls."""
    supabase = get_supabase()
    today_ist = datetime.now(IST).date().isoformat()
    query_date = today_ist
    rows = supabase.from_("wolves_fresh_events") \
        .select("*") \
        .eq("scan_date", query_date) \
        .order("scan_time", desc=True) \
        .execute().data or []
    if not rows:
        fallback_date = _latest_scan_date(supabase, "wolves_fresh_events")
        if fallback_date and fallback_date != query_date:
            query_date = fallback_date
            rows = supabase.from_("wolves_fresh_events") \
                .select("*") \
                .eq("scan_date", query_date) \
                .order("scan_time", desc=True) \
                .execute().data or []
    return {"date": query_date, "is_fallback": query_date != today_ist, "events": rows}


def get_todays_watchlist() -> dict:
    """Read-only -- today's Decision Watchlist (Rank/Confidence/Decision
    Score/Sector Tailwind per symbol+direction), falling back to the last
    trading day with data if today has none yet. Cheap Supabase read, no
    Kite calls."""
    supabase = get_supabase()
    today_ist = datetime.now(IST).date().isoformat()
    query_date = today_ist
    rows = supabase.from_("wolves_watchlist") \
        .select("*") \
        .eq("scan_date", query_date) \
        .order("decision_score", desc=True) \
        .execute().data or []
    if not rows:
        fallback_date = _latest_scan_date(supabase, "wolves_watchlist")
        if fallback_date and fallback_date != query_date:
            query_date = fallback_date
            rows = supabase.from_("wolves_watchlist") \
                .select("*") \
                .eq("scan_date", query_date) \
                .order("decision_score", desc=True) \
                .execute().data or []
    return {"date": query_date, "is_fallback": query_date != today_ist, "watchlist": rows}
