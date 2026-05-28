from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import time as time_module

# Simple cache — signal log is expensive to compute
_signal_cache: dict = {}
_signal_cache_time: float = 0
SIGNAL_CACHE_TTL = 60  # seconds


def to_ist(ts: str) -> str:
    try:
        clean = ts.split('+')[0].split('Z')[0]
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        ist = dt.hour * 60 + dt.minute + 330
        return f"{(ist//60)%24:02d}:{ist%60:02d}"
    except:
        return ts[11:16]


def is_market_hours() -> bool:
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return False
    total = now_utc.hour * 60 + now_utc.minute
    return (3 * 60 + 45) <= total <= (10 * 60 + 0)


def classify(oi_chg_pct: float, price_chg_pct: float):
    if oi_chg_pct > 0 and price_chg_pct > 0:
        return "LONG_BUILDUP",  "Long Buildup",  "BULLISH"
    if oi_chg_pct > 0 and price_chg_pct < 0:
        return "SHORT_BUILDUP", "Short Buildup", "BEARISH"
    if oi_chg_pct < 0 and price_chg_pct > 0:
        return "SHORT_COVERING","Short Covering","BULLISH"
    if oi_chg_pct < 0 and price_chg_pct < 0:
        return "LONG_UNWINDING","Long Unwinding","BEARISH"
    return None, None, None


# ── Options confirmation logic ────────────────────────────────────────────────
# Maps FUT signal type to confirming UOA signal types
CONFIRMING_SIGNALS = {
    "LONG_BUILDUP":   ["PUT_WRITING", "LONG_BUILDUP", "SHORT_COVERING", "BUYER_DOMINATED"],
    "SHORT_BUILDUP":  ["CALL_WRITING", "SHORT_BUILDUP", "LONG_UNWINDING", "SELLER_DOMINATED"],
    "SHORT_COVERING": ["PUT_WRITING", "SHORT_COVERING", "BUYER_DOMINATED"],
    "LONG_UNWINDING": ["CALL_WRITING", "LONG_UNWINDING", "SELLER_DOMINATED"],
}

SIGNAL_LABELS = {
    "PUT_WRITING":      "Put Writing",
    "CALL_WRITING":     "Call Writing",
    "LONG_BUILDUP":     "Long Buildup",
    "SHORT_BUILDUP":    "Short Buildup",
    "SHORT_COVERING":   "Short Covering",
    "LONG_UNWINDING":   "Long Unwinding",
    "BUYER_DOMINATED":  "Buyer Dominated",
    "SELLER_DOMINATED": "Seller Dominated",
    "FAR_OTM_ACTIVITY": "Far OTM Activity",
    "VOLUME_SURGE":     "Volume Surge",
}


def _get_uoa_confirmation(uoa_signals: list, fut_signal_type: str) -> dict:
    """
    Given a list of UOA signals for a symbol and the FUT signal type,
    find the best confirming or contradicting options signal.
    Returns a dict with confirmation details.
    """
    if not uoa_signals:
        return {"has_confirmation": False, "confirms": None, "best_signal": None}

    confirming = CONFIRMING_SIGNALS.get(fut_signal_type, [])

    # Score each UOA signal: confirming signals scored higher
    best_confirming = None
    best_contradicting = None

    for sig in uoa_signals:
        sig_type = sig.get("signal_type", "")
        score = sig.get("score", 0)
        strike = sig.get("strike", 0)
        opt_type = sig.get("option_type", "")

        enriched = {
            "signal_type":  sig_type,
            "label":        SIGNAL_LABELS.get(sig_type, sig_type),
            "strike":       strike,
            "option_type":  opt_type,
            "score":        score,
            "bias":         sig.get("bias", ""),
        }

        if sig_type in confirming:
            if best_confirming is None or score > best_confirming["score"]:
                best_confirming = enriched
        else:
            if best_contradicting is None or score > best_contradicting["score"]:
                best_contradicting = enriched

    if best_confirming:
        return {
            "has_confirmation": True,
            "confirms": True,
            "best_signal": best_confirming,
            "alignment": "✅ Confirms",
            "alignment_color": "EMERALD",
        }
    elif best_contradicting:
        return {
            "has_confirmation": True,
            "confirms": False,
            "best_signal": best_contradicting,
            "alignment": "⚠️ Contradicts",
            "alignment_color": "AMBER",
        }
    else:
        return {"has_confirmation": False, "confirms": None, "best_signal": None}


def get_signal_log(date: str = None):
    global _signal_cache, _signal_cache_time

    cache_ttl = 60 if is_market_hours() else 300
    if _signal_cache and (time_module.time() - _signal_cache_time) < cache_ttl:
        return _signal_cache

    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Step 1: Get all timestamps for today ─────────────────────────────────
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("option_type", "FUT")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .limit(500)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in (ts_result.data or [])))
    if len(timestamps) < 2:
        return {"signals": [], "total": 0, "date": today, "snapshots": 0,
                "message": "Need at least 2 snapshots — check back after 9:20 AM"}

    ts_open     = timestamps[0]
    ts_latest   = timestamps[-1]
    total_snaps = len(timestamps)

    # ── Step 2: Fetch ALL futures OI for today ────────────────────────────────
    all_fut_rows = []
    for offset in range(0, 100000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp, symbol, oi, volume, last_price")\
            .eq("option_type", "FUT")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .lt("timestamp",  f"{today}T23:59:59+00:00")\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_fut_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not all_fut_rows:
        return {"signals": [], "total": 0, "date": today, "snapshots": total_snaps,
                "message": "No futures data yet — FUT capture started today, check after next cycle"}

    # ── Step 3: Build per-symbol, per-timestamp maps ──────────────────────────
    from collections import defaultdict
    fut_data: dict = defaultdict(dict)
    for r in all_fut_rows:
        sym = r["symbol"]
        ts  = r["timestamp"]
        if ts not in fut_data[sym]:
            fut_data[sym][ts] = {"oi": 0, "volume": 0, "last_price": 0}
        fut_data[sym][ts]["oi"]         += int(r["oi"] or 0)
        fut_data[sym][ts]["volume"]     += int(r["volume"] or 0)
        fut_data[sym][ts]["last_price"]  = float(r["last_price"] or 0)

    # ── Step 4: Get CMP from cmp_prices ──────────────────────────────────────
    cmp_result = supabase.from_("cmp_prices")\
        .select("symbol, cmp")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .limit(500)\
        .execute()
    cmp_map: dict = {}
    for r in (cmp_result.data or []):
        if r["symbol"] not in cmp_map:
            cmp_map[r["symbol"]] = float(r["cmp"])

    # ── Step 5: Get CPR positions ─────────────────────────────────────────────
    cpr_result = supabase.from_("cpr_levels")\
        .select("symbol, tc, bc, width_pct, width_emoji, is_virgin")\
        .gte("trade_date", today)\
        .limit(200)\
        .execute()
    cpr_map: dict = {}
    for r in (cpr_result.data or []):
        cpr_map[r["symbol"]] = r

    # ── Step 6: Get UOA signals for options confirmation ─────────────────────
    uoa_map: dict = defaultdict(list)  # symbol → list of UOA signals
    uoa_fetch_ok = False
    try:
        from api.uoa import get_uoa
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(get_uoa, date=today)
            uoa_data = future.result(timeout=8)  # 8 second max
        for sig in uoa_data.get("signals", []):
            if sig.get("score", 0) >= 3:
                uoa_map[sig["symbol"]].append(sig)
        uoa_fetch_ok = True
    except concurrent.futures.TimeoutError:
        print(f"[SIGNAL_LOG] UOA fetch timed out — showing FUT signals only")
    except Exception as e:
        print(f"[SIGNAL_LOG] UOA fetch failed: {e}")

    # ── Step 7: Compute signals per symbol ───────────────────────────────────
    signal_log: dict = {}

    for sym, ts_map in fut_data.items():
        open_snap   = ts_map.get(ts_open)
        latest_snap = ts_map.get(ts_latest)
        if not open_snap or not latest_snap:
            continue

        oi_open      = open_snap["oi"]
        oi_latest    = latest_snap["oi"]
        vol_open     = open_snap["volume"]
        vol_latest   = latest_snap["volume"]
        price_open   = open_snap["last_price"]
        price_latest = latest_snap["last_price"]

        if oi_open == 0 or vol_open == 0 or price_open == 0:
            continue

        oi_chg_pct    = round((oi_latest - oi_open) / oi_open * 100, 2)
        price_chg_pct = round((price_latest - price_open) / price_open * 100, 2)
        vol_chg_pct   = round((vol_latest - vol_open) / vol_open * 100, 2) if vol_open > 0 else 0

        # Qualification thresholds
        if abs(oi_chg_pct) < 3.0:      continue
        if abs(price_chg_pct) < 0.3:   continue
        if vol_latest < vol_open * 1.2: continue

        signal_type, label, bias = classify(oi_chg_pct, price_chg_pct)
        if not signal_type:
            continue

        # Persistence
        persistence   = 0
        first_seen_ts = ts_latest
        for ts in timestamps:
            snap = ts_map.get(ts)
            if not snap:
                continue
            snap_oi_chg    = (snap["oi"] - oi_open) / oi_open * 100 if oi_open > 0 else 0
            snap_price_chg = (snap["last_price"] - price_open) / price_open * 100 if price_open > 0 else 0
            s, _, _ = classify(snap_oi_chg, snap_price_chg)
            if s == signal_type:
                persistence += 1
                if ts < first_seen_ts:
                    first_seen_ts = ts

        if persistence < 2:
            continue

        # CPR context
        cpr = cpr_map.get(sym, {})
        cmp = cmp_map.get(sym, price_latest)
        cpr_position = None
        if cpr:
            tc = float(cpr.get("tc", 0))
            bc = float(cpr.get("bc", 0))
            if cmp > tc:
                cpr_position = "Above CPR"
            elif cmp < bc:
                cpr_position = "Below CPR"
            else:
                cpr_position = "Inside CPR"

        # ── Options confirmation ──────────────────────────────────────────────
        uoa_signals    = uoa_map.get(sym, [])
        options_conf   = _get_uoa_confirmation(uoa_signals, signal_type)

        signal_log[sym] = {
            "symbol":          sym,
            "cmp":             round(cmp, 2),
            "fut_oi_now":      oi_latest,
            "fut_oi_open":     oi_open,
            "oi_chg_pct":      oi_chg_pct,
            "price_chg_pct":   price_chg_pct,
            "vol_now":         vol_latest,
            "vol_open":        vol_open,
            "vol_chg_pct":     vol_chg_pct,
            "vol_surge":       vol_chg_pct > 50,
            "signal_type":     signal_type,
            "label":           label,
            "bias":            bias,
            "persistence":     persistence,
            "persistence_pct": round(persistence / total_snaps * 100),
            "first_seen":      to_ist(first_seen_ts),
            "first_seen_ts":   first_seen_ts,
            "is_active":       True,
            "cpr_position":    cpr_position,
            "cpr_width_emoji": cpr.get("width_emoji"),
            "cpr_is_virgin":   cpr.get("is_virgin"),
            # Options confirmation fields
            "options_confirmation": options_conf.get("has_confirmation", False),
            "options_confirms":     options_conf.get("confirms"),
            "options_alignment":    options_conf.get("alignment"),
            "options_alignment_color": options_conf.get("alignment_color"),
            "options_signal":       options_conf.get("best_signal"),
        }

    # ── Step 8: Sort — persistence first, then OI change ─────────────────────
    signals = sorted(
        signal_log.values(),
        key=lambda x: (x["persistence"], abs(x["oi_chg_pct"])),
        reverse=True
    )

    result = {
        "date":          today,
        "signals":       signals,
        "total":         len(signals),
        "snapshots":     total_snaps,
        "open_time":     to_ist(ts_open),
        "latest_time":   to_ist(ts_latest),
        "long_buildup":  sum(1 for s in signals if s["signal_type"] == "LONG_BUILDUP"),
        "short_buildup": sum(1 for s in signals if s["signal_type"] == "SHORT_BUILDUP"),
        "short_covering":sum(1 for s in signals if s["signal_type"] == "SHORT_COVERING"),
        "long_unwinding":sum(1 for s in signals if s["signal_type"] == "LONG_UNWINDING"),
    }

    # Only cache if signals found, OR UOA fetch worked AND market genuinely empty
    if len(signals) > 0 or (uoa_fetch_ok and total_snaps >= 5):
        _signal_cache = result
        _signal_cache_time = time_module.time()
    else:
        print(f"[SIGNAL_LOG] Not caching — {len(signals)} signals, uoa_ok={uoa_fetch_ok}, snaps={total_snaps}")
    return result
