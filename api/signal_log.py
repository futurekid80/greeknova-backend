from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import time as time_module

# Simple cache — signal log is expensive to compute
_signal_cache: dict = {}
_signal_cache_time: float = 0
SIGNAL_CACHE_TTL = 60  # seconds

def _save_eod_to_supabase(supabase, result: dict):
    """Save EOD snapshot to Supabase for persistence across restarts."""
    try:
        summary = {k: v for k, v in result.items() if k != "signals"}
        supabase.table("intraday_signal_cache").upsert({
            "id":         1,
            "signals":    result["signals"],
            "summary":    summary,
            "trade_date": result["date"],
        }, on_conflict="id").execute()
        print(f"[SIGNAL_LOG] EOD snapshot saved to Supabase — {len(result['signals'])} signals")
    except Exception as e:
        print(f"[SIGNAL_LOG] Failed to save EOD snapshot: {e}")


def _load_eod_from_supabase(supabase) -> dict | None:
    """Load last EOD snapshot from Supabase."""
    try:
        res = supabase.from_("intraday_signal_cache")\
            .select("*").eq("id", 1).limit(1).execute()
        if not res.data:
            return None
        row = res.data[0]
        result = {**row["summary"], "signals": row["signals"]}
        print(f"[SIGNAL_LOG] Loaded EOD snapshot from Supabase — {len(row['signals'])} signals for {row['trade_date']}")
        return result
    except Exception as e:
        print(f"[SIGNAL_LOG] Failed to load EOD snapshot: {e}")
        return None


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


def _get_conv_stars(sig: dict, fut_signal_type: str, cmp: float = 0) -> str:
    """
    Star rating for HIGH CONV based on how close the writer is to CMP.
    PUT WRITING confirming LONG BUILDUP:
      🌟🌟🌟 = put writing ABOVE CMP (extreme bullish conviction)
      🌟🌟   = within 1 strike of ATM (gold standard)
      🌟     = within 2 strikes of ATM (standard)
      None   = beyond 2 strikes (noise, no HIGH CONV)
    CE WRITING confirming SHORT BUILDUP: mirror logic.

    cmp is passed explicitly from FUT data — never rely on sig["cmp"] which may be 0.
    strikes_from_atm is recomputed here using strike interval detection.
    """
    strike   = sig.get("strike", 0)
    opt_type = sig.get("option_type", "")
    sig_type = sig.get("signal_type", "")

    # Use passed CMP — fallback to sig["cmp"] only if not provided
    if not cmp:
        cmp = sig.get("cmp", 0)
    if not cmp or not strike:
        return ""  # can't compute without CMP

    # Detect strike interval from symbol name (NIFTY=50, BANKNIFTY=100, stocks=5)
    sym = sig.get("symbol", "")
    if "NIFTY" in sym and "BANK" in sym:
        interval = 100
    elif "NIFTY" in sym:
        interval = 50
    elif "FINNIFTY" in sym:
        interval = 50
    else:
        interval = 5  # most F&O stocks use 5pt intervals

    # Recompute strikes_from_atm using actual CMP and strike
    atm = round(cmp / interval) * interval  # snap CMP to nearest strike
    strikes_from_atm = abs(strike - atm) / interval

    # Check if writer is on the "strong" side — above CMP for PE, below CMP for CE
    is_extreme = (
        (opt_type == "PE" and strike > cmp and sig_type == "PUT_WRITING") or
        (opt_type == "CE" and strike < cmp and sig_type == "CALL_WRITING")
    )

    if is_extreme:
        return "🌟🌟🌟"
    elif strikes_from_atm <= 1:
        return "🌟🌟"
    elif strikes_from_atm <= 2:
        return "🌟"
    else:
        return ""  # too far — no HIGH CONV


def _get_uoa_confirmation(uoa_signals: list, fut_signal_type: str, cmp_override: float = 0) -> dict:
    if not uoa_signals:
        return {"has_confirmation": False, "confirms": None, "best_signal": None}

    confirming = CONFIRMING_SIGNALS.get(fut_signal_type, [])
    best_confirming = None
    best_contradicting = None

    for sig in uoa_signals:
        sig_type = sig.get("signal_type", "")
        score = sig.get("score", 0)
        strike = sig.get("strike", 0)
        opt_type = sig.get("option_type", "")
        strikes_from_atm = sig.get("strikes_from_atm", 99)

        enriched = {
            "signal_type":      sig_type,
            "label":            SIGNAL_LABELS.get(sig_type, sig_type),
            "strike":           strike,
            "option_type":      opt_type,
            "score":            score,
            "bias":             sig.get("bias", ""),
            "strikes_from_atm": strikes_from_atm,
            "otm_distance_pct": sig.get("otm_distance_pct", 0),
            "cmp":              sig.get("cmp", 0),
        }

        if sig_type in confirming:
            # Only near-ATM signals qualify for confirmation (within 2 strikes)
            # Pass cmp explicitly — UOA signals may not carry cmp field
            _cmp = sig.get("cmp", 0) or cmp_override
            stars = _get_conv_stars(sig, fut_signal_type, cmp=_cmp)
            if stars == "":
                # Too far OTM — treat as contradicting (noise)
                if best_contradicting is None or score > best_contradicting.get("score", 0):
                    best_contradicting = enriched
                continue
            enriched["conv_stars"] = stars
            # Prefer closer strikes: sort by stars desc, then score desc
            star_rank = {"🌟🌟🌟": 3, "🌟🌟": 2, "🌟": 1}.get(stars, 0)
            existing_rank = {"🌟🌟🌟": 3, "🌟🌟": 2, "🌟": 1}.get(
                best_confirming.get("conv_stars", "") if best_confirming else "", 0)
            if best_confirming is None or star_rank > existing_rank or (
                    star_rank == existing_rank and score > best_confirming["score"]):
                best_confirming = enriched
        else:
            if best_contradicting is None or score > best_contradicting.get("score", 0):
                best_contradicting = enriched

    if best_confirming:
        stars = best_confirming.get("conv_stars", "🌟")
        return {
            "has_confirmation": True,
            "confirms": True,
            "best_signal": best_confirming,
            "alignment": f"✅ Confirms",
            "alignment_color": "EMERALD",
            "conv_stars": stars,
        }
    elif best_contradicting:
        return {
            "has_confirmation": True,
            "confirms": False,
            "best_signal": best_contradicting,
            "alignment": "⚠️ Contradicts",
            "alignment_color": "AMBER",
            "conv_stars": "",
        }
    else:
        return {"has_confirmation": False, "confirms": None, "best_signal": None}


def _get_atm_bias(atm_snapshot: dict, cmp: float) -> dict:
    """
    From the latest options snapshot for a symbol, find the ATM strike
    and compare CE vs PE OI to determine institutional bias.
    
    Returns:
      atm_strike: the nearest strike to CMP
      atm_ce_oi: CE OI at ATM
      atm_pe_oi: PE OI at ATM
      atm_bias: 'PE_FLOOR' | 'CE_CAP' | 'NEUTRAL'
      atm_bias_label: human readable
      atm_bias_color: 'EMERALD' | 'RED' | 'GRAY'
    """
    if not atm_snapshot or cmp <= 0:
        return {"atm_bias": None, "atm_bias_label": None, "atm_bias_color": None,
                "atm_strike": None, "atm_ce_oi": 0, "atm_pe_oi": 0}

    # Find ATM strike — nearest to CMP
    strikes = list(atm_snapshot.keys())
    if not strikes:
        return {"atm_bias": None, "atm_bias_label": None, "atm_bias_color": None,
                "atm_strike": None, "atm_ce_oi": 0, "atm_pe_oi": 0}

    atm_strike = min(strikes, key=lambda s: abs(s - cmp))
    ce_oi = atm_snapshot[atm_strike].get("ce_oi", 0)
    pe_oi = atm_snapshot[atm_strike].get("pe_oi", 0)

    if pe_oi > ce_oi * 1.2:
        bias = "PE_FLOOR"
        label = "🟢 PE Floor"
        color = "EMERALD"
    elif ce_oi > pe_oi * 1.2:
        bias = "CE_CAP"
        label = "🔴 CE Cap"
        color = "RED"
    else:
        bias = "NEUTRAL"
        label = "⚪ Neutral"
        color = "GRAY"

    return {
        "atm_bias":       bias,
        "atm_bias_label": label,
        "atm_bias_color": color,
        "atm_strike":     atm_strike,
        "atm_ce_oi":      ce_oi,
        "atm_pe_oi":      pe_oi,
    }


def get_signal_log(date: str = None):
    global _signal_cache, _signal_cache_time

    supabase = get_supabase()

    # ── Post-market / weekend: serve EOD snapshot ─────────────────────────────
    if not is_market_hours():
        # Try in-memory cache first
        if _signal_cache and _signal_cache.get("signals"):
            return {**_signal_cache, "is_eod_snapshot": True}
        # Fall back to Supabase persisted snapshot
        saved = _load_eod_from_supabase(supabase)
        if saved and saved.get("signals"):
            _signal_cache = saved
            _signal_cache_time = time_module.time()
            return {**saved, "is_eod_snapshot": True}
        # No snapshot available yet
        return {"signals": [], "total": 0, "snapshots": 0,
                "message": "No EOD snapshot yet — will be available after first market session",
                "is_eod_snapshot": True}

    # ── Market hours: use in-memory cache ─────────────────────────────────────
    cache_ttl = 60
    if _signal_cache and (time_module.time() - _signal_cache_time) < cache_ttl:
        return _signal_cache
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Step 1: Get all timestamps for today ─────────────────────────────────
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("option_type", "FUT")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp")\
        .limit(500)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in (ts_result.data or [])))
    if len(timestamps) < 2:
        return {"signals": [], "total": 0, "date": today, "snapshots": 0,
                "message": "Need at least 2 snapshots — check back after 9:20 AM"}

    # ── Use second snapshot as open baseline ──────────────────────────────────────
    # First snapshot (9:18 AM) has near-zero volume — exchange feeds not fully
    # populated yet. Second snapshot (9:23 AM) gives stable OI + volume baseline.
    # This prevents artificial 10,000%+ volume surge numbers.
    ts_open     = timestamps[1] if len(timestamps) >= 2 else timestamps[0]
    ts_latest   = timestamps[-1]
    total_snaps = len(timestamps)

    # ── Step 2: Fetch ALL futures OI for today ────────────────────────────────
    all_fut_rows = []
    for offset in range(0, 100000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp, symbol, oi, volume, last_price, expiry")\
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
    # Pick nearest valid expiry = smallest expiry date >= today
    # This correctly identifies near-month contract for each symbol
    from datetime import date as _date
    _today = _date.today().isoformat()
    nearest_expiry: dict = {}
    for r in all_fut_rows:
        sym    = r["symbol"]
        expiry = r.get("expiry", "")
        if not expiry:
            continue
        expiry_str = str(expiry)  # convert date object to string for comparison
        if expiry_str < _today:   # skip expired contracts
            continue
        if sym not in nearest_expiry or expiry_str < nearest_expiry[sym]:
            nearest_expiry[sym] = expiry_str

    fut_data: dict = defaultdict(dict)
    for r in all_fut_rows:
        sym    = r["symbol"]
        expiry = str(r.get("expiry", ""))  # convert date to string
        if expiry != nearest_expiry.get(sym):
            continue
        ts = r["timestamp"]
        if ts not in fut_data[sym]:
            fut_data[sym][ts] = {"oi": 0, "volume": 0, "last_price": 0}
        fut_data[sym][ts]["oi"]         += int(r["oi"] or 0)
        fut_data[sym][ts]["volume"]     += int(r["volume"] or 0)
        fut_data[sym][ts]["last_price"]  = float(r["last_price"] or 0)

    # ── Step 4: Get CMP ───────────────────────────────────────────────────────
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

    # ── Step 5b: Fetch historical volume for multi-day comparison ───────────────
    # Single query: last 5 days max FUT volume per symbol
    # vol_rank = how many of last 5 days today's volume exceeds
    hist_vol_map: dict = {}  # sym -> [vol_day1, vol_day2, ...] sorted recent first
    try:
        from datetime import timedelta
        from collections import defaultdict
        # Get last 5 trading dates before today — limit to 5000 rows max
        hist_start = (datetime.now(timezone.utc) - timedelta(days=8)).strftime('%Y-%m-%d')
        # Only fetch last snapshot per day per symbol (max volume proxy)
        hist_rows = supabase.from_("oi_snapshots")            .select("symbol, volume, timestamp")            .eq("option_type", "FUT")            .gte("timestamp", f"{hist_start}T09:00:00+00:00")            .lt("timestamp",  f"{today}T00:00:00+00:00")            .gte("volume", 1000)            .order("timestamp", desc=False)            .limit(5000)            .execute()
        sym_date_vol: dict = defaultdict(lambda: defaultdict(int))
        for r in (hist_rows.data or []):
            sym = r["symbol"]
            date_str = str(r["timestamp"])[:10]
            vol = int(r["volume"] or 0)
            if vol > sym_date_vol[sym][date_str]:
                sym_date_vol[sym][date_str] = vol
        for sym, date_vols in sym_date_vol.items():
            sorted_vols = [v for _, v in sorted(date_vols.items(), reverse=True)][:5]
            hist_vol_map[sym] = sorted_vols
        print(f"[SIGNAL_LOG] Historical volume loaded for {len(hist_vol_map)} symbols")
    except Exception as e:
        hist_vol_map = {}
        print(f"[SIGNAL_LOG] Historical volume fetch failed (non-fatal): {e}")

    # ── Step 5c: Fetch latest options snapshot for ATM bias ───────────────────
    # Build strike → {ce_oi, pe_oi} map per symbol from latest snapshot
    atm_data: dict = defaultdict(dict)  # sym → {strike: {ce_oi, pe_oi}}
    try:
        options_latest = supabase.from_("oi_snapshots")\
            .select("symbol, strike, option_type, oi")\
            .eq("timestamp", ts_latest)\
            .in_("option_type", ["CE", "PE"])\
            .limit(15000)\
            .execute()
        for r in (options_latest.data or []):
            sym    = r["symbol"]
            strike = float(r["strike"])
            oi     = int(r["oi"] or 0)
            opt    = r["option_type"]
            if strike not in atm_data[sym]:
                atm_data[sym][strike] = {"ce_oi": 0, "pe_oi": 0}
            if opt == "CE":
                atm_data[sym][strike]["ce_oi"] += oi
            else:
                atm_data[sym][strike]["pe_oi"] += oi
    except Exception as e:
        print(f"[SIGNAL_LOG] ATM bias fetch failed: {e}")

    # ── Step 6: Get UOA signals ───────────────────────────────────────────────
    uoa_map: dict = defaultdict(list)
    uoa_fetch_ok = False
    try:
        from api.uoa import get_uoa
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(get_uoa, date=today)
            uoa_data = future.result(timeout=20)
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

        if abs(oi_chg_pct) < 3.0:      continue
        if abs(price_chg_pct) < 0.3:   continue
        if vol_latest < vol_open * 1.2: continue

        signal_type, label, bias = classify(oi_chg_pct, price_chg_pct)
        if not signal_type:
            continue

        # ── Persistence — tracks current run start, resets on signal flip ────
        persistence    = 0
        prev_signal    = None
        run_start_ts   = None
        first_seen_ts  = ts_latest

        for ts in timestamps:
            snap = ts_map.get(ts)
            if not snap:
                continue
            snap_oi_chg    = (snap["oi"] - oi_open) / oi_open * 100 if oi_open > 0 else 0
            snap_price_chg = (snap["last_price"] - price_open) / price_open * 100 if price_open > 0 else 0
            s, _, _ = classify(snap_oi_chg, snap_price_chg)

            if s == signal_type:
                persistence += 1
                if prev_signal != signal_type:
                    run_start_ts = ts
                first_seen_ts = run_start_ts

            prev_signal = s

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

        # ── ATM OI Bias ───────────────────────────────────────────────────────
        atm_bias_data = _get_atm_bias(atm_data.get(sym, {}), cmp)


        # ── Multi-day volume rank ─────────────────────────────────────────────
        hist_vols = hist_vol_map.get(sym, [])
        vol_rank = 0
        for prev_vol in hist_vols:
            if prev_vol > 0 and vol_latest > prev_vol:
                vol_rank += 1
            else:
                break  # must be consecutive days
        if vol_rank >= 5:
            vol_rank_label = "📊 Vol > 5D"
            vol_rank_color = "PURPLE"
        elif vol_rank >= 3:
            vol_rank_label = f"📊 Vol > {vol_rank}D"
            vol_rank_color = "EMERALD"
        elif vol_rank >= 1:
            vol_rank_label = f"📊 Vol > {vol_rank}D"
            vol_rank_color = "AMBER"
        else:
            vol_rank_label = ""
            vol_rank_color = ""
        # Options confirmation
        uoa_signals  = uoa_map.get(sym, [])
        options_conf = _get_uoa_confirmation(uoa_signals, signal_type, cmp_override=cmp)

        import math
        conviction_score = round(
            (persistence / total_snaps) * 100 *
            math.log1p(abs(oi_chg_pct)) *
            math.log1p(max(vol_chg_pct, 0) / 100 + 1),
            2
        )

        from utils.oi_walls import get_oi_walls
        walls = get_oi_walls(sym, supabase, cmp)

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
            "conviction_score": conviction_score,
            "first_seen":      to_ist(first_seen_ts),
            "first_seen_ts":   first_seen_ts,
            "is_active":       True,
            "cpr_position":    cpr_position,
            "cpr_width_emoji": cpr.get("width_emoji"),
            "cpr_is_virgin":   cpr.get("is_virgin"),
            "options_confirmation": options_conf.get("has_confirmation", False),
            "options_confirms":     options_conf.get("confirms"),
            "options_alignment":    options_conf.get("alignment"),
            "options_alignment_color": options_conf.get("alignment_color"),
            "options_signal":       options_conf.get("best_signal"),
            "conv_stars":           options_conf.get("conv_stars", ""),
            "vol_rank":             vol_rank,
            "vol_rank_label":       vol_rank_label,
            "vol_rank_color":       vol_rank_color,
            "ce_wall":              walls.get("ce_wall"),
            "pe_wall":              walls.get("pe_wall"),
            "ce_wall_oi_L":         walls.get("ce_wall_oi_L"),
            "pe_wall_oi_L":         walls.get("pe_wall_oi_L"),
            "trade_range":          walls.get("trade_range"),
            "trade_range_pct":      walls.get("trade_range_pct"),
            "range_label":          walls.get("range_label"),
            # ATM OI Bias — new fields
            "atm_bias":        atm_bias_data.get("atm_bias"),
            "atm_bias_label":  atm_bias_data.get("atm_bias_label"),
            "atm_bias_color":  atm_bias_data.get("atm_bias_color"),
            "atm_strike":      atm_bias_data.get("atm_strike"),
            "atm_ce_oi":       atm_bias_data.get("atm_ce_oi"),
            "atm_pe_oi":       atm_bias_data.get("atm_pe_oi"),
        }

    # ── Step 8: Sort by conviction score ─────────────────────────────────────
    signals = sorted(
        signal_log.values(),
        key=lambda x: x["conviction_score"],
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

    if len(signals) > 0 or (uoa_fetch_ok and total_snaps >= 5):
        _signal_cache = result
        _signal_cache_time = time_module.time()
        # Save to Supabase after 3:25 PM IST (10:55 UTC) — near market close
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour > 10 or (now_utc.hour == 10 and now_utc.minute >= 55):
            _save_eod_to_supabase(supabase, result)
    else:
        print(f"[SIGNAL_LOG] Not caching — {len(signals)} signals, uoa_ok={uoa_fetch_ok}, snaps={total_snaps}")
    return result
