from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type


def get_eod_analysis(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    supabase = get_supabase()

    # ── Get available dates ───────────────────────────────────────────────────
    dates = set()
    base = datetime.now(timezone.utc).date()
    for i in range(60):
        d = (base - timedelta(days=i)).isoformat()
        r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp", f"{d}T23:59:59+00:00")\
            .limit(1).execute()
        if r.data:
            dates.add(d)
        if len(dates) >= 30:
            break

    if not dates:
        return {"symbol": symbol, "dates": [], "rows": []}

    sorted_dates = sorted(dates)

    # ── Default to last full trading day ──────────────────────────────────────
    if date:
        active_date = date
    else:
        active_date = sorted_dates[-1]
        for d in reversed(sorted_dates):
            ts_check = supabase.from_("oi_snapshots")\
                .select("timestamp")\
                .eq("symbol", symbol)\
                .gte("timestamp", f"{d}T00:00:00+00:00")\
                .lt("timestamp", f"{d}T23:59:59+00:00")\
                .limit(10).execute()
            if len(ts_check.data or []) >= 5:
                active_date = d
                break

    # ── FIX: Paginated timestamp fetch ────────────────────────────────────────
    # Old limit(5000) was cutting off at ~11:00 IST
    all_ts_data = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{active_date}T00:00:00+00:00")\
            .lt("timestamp", f"{active_date}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        all_ts_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not all_ts_data:
        return {"symbol": symbol, "dates": sorted_dates, "date": active_date, "rows": []}

    timestamps = sorted(set(r["timestamp"] for r in all_ts_data))
    first_ts = timestamps[0]
    last_ts  = timestamps[-1]

    # ── Get expiries — default to nearest ────────────────────────────────────
    exp_q = supabase.from_("oi_snapshots")\
        .select("expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", last_ts).execute()

    all_expiries = sorted(set(r["expiry"] for r in (exp_q.data or []) if r["expiry"]))
    future_expiries = [e for e in all_expiries if e >= active_date]
    expiries = future_expiries if future_expiries else all_expiries
    active_expiry = expiry or (expiries[0] if expiries else None)

    # ── Fetch open/close snapshots ────────────────────────────────────────────
    def fetch_snap(ts):
        all_data = []
        for offset in range(0, 10000, 1000):
            q = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts)
            if active_expiry:
                q = q.eq("expiry", active_expiry)
            batch = q.range(offset, offset + 999).execute()
            if not batch.data:
                break
            all_data.extend(batch.data)
            if len(batch.data) < 1000:
                break
        result = {}
        for r in all_data:
            result[(r["strike"], r["option_type"])] = r["oi"] or 0
        return result

    snap_open  = fetch_snap(first_ts)
    snap_close = fetch_snap(last_ts)
    all_strikes = sorted(set(k[0] for k in list(snap_open.keys()) + list(snap_close.keys())))

    # ── Intraday journey ──────────────────────────────────────────────────────
    journey_raw = []
    journey_q = supabase.from_("oi_snapshots")\
        .select("timestamp, option_type, oi")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp", f"{active_date}T23:59:59+00:00")
    if active_expiry:
        journey_q = journey_q.eq("expiry", active_expiry)

    for offset in range(0, 200000, 1000):
        batch = journey_q.range(offset, offset + 999).execute()
        if not batch.data:
            break
        journey_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    ts_groups: dict = {}
    for r in journey_raw:
        ts = r["timestamp"]
        if ts not in ts_groups:
            ts_groups[ts] = {"ce": 0, "pe": 0}
        if r["option_type"] == "CE":
            ts_groups[ts]["ce"] += r["oi"] or 0
        else:
            ts_groups[ts]["pe"] += r["oi"] or 0

    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base_t, frac = clean.split('.')
                clean = f"{base_t}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist_min = dt.hour * 60 + dt.minute + 330
            return f"{(ist_min//60)%24:02d}:{ist_min%60:02d}"
        except:
            return ts[11:16]

    journey_data = []
    for ts in sorted(ts_groups.keys()):
        ce = ts_groups[ts]["ce"]
        pe = ts_groups[ts]["pe"]
        pcr = round(pe / ce, 3) if ce > 0 else 0
        journey_data.append({"time": to_ist(ts), "ce_oi": ce, "pe_oi": pe, "pcr": pcr})

    # ── Build rows ────────────────────────────────────────────────────────────
    rows = []
    for strike in all_strikes:
        ce_open  = snap_open.get((strike, "CE"), 0)
        ce_close = snap_close.get((strike, "CE"), 0)
        pe_open  = snap_open.get((strike, "PE"), 0)
        pe_close = snap_close.get((strike, "PE"), 0)
        ce_chg   = ce_close - ce_open
        pe_chg   = pe_close - pe_open
        rows.append({
            "strike":   strike,
            "ce_open":  ce_open, "ce_close": ce_close, "ce_chg": ce_chg,
            "pe_open":  pe_open, "pe_close": pe_close, "pe_chg": pe_chg,
            "net_chg":  pe_chg - ce_chg,
        })

    # ── Cross-reference: UOA whale strikes ───────────────────────────────────
    # Fetch last 2 UOA snapshots to find whale activity at specific strikes
    uoa_whale_strikes = set()   # strikes with whale/large OI activity
    uoa_details = {}            # strike -> signal details
    try:
        uoa_ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{active_date}T00:00:00+00:00")\
            .lt("timestamp", f"{active_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(4).execute()

        uoa_timestamps = sorted(set(r["timestamp"] for r in (uoa_ts_q.data or [])), reverse=True)
        if len(uoa_timestamps) >= 2:
            ts_new = uoa_timestamps[0]
            ts_old = uoa_timestamps[1]

            new_snap = supabase.from_("oi_snapshots")\
                .select("symbol, tradingsymbol, strike, option_type, oi, volume")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts_new)\
                .limit(5000).execute().data or []

            old_snap = supabase.from_("oi_snapshots")\
                .select("symbol, tradingsymbol, strike, option_type, oi, volume")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts_old)\
                .limit(5000).execute().data or []

            old_map = {f"{r['strike']}_{r['option_type']}": r for r in old_snap}

            for row in new_snap:
                key = f"{row['strike']}_{row['option_type']}"
                old_row = old_map.get(key)
                if not old_row:
                    continue
                new_vol = row["volume"] or 0
                old_vol = old_row["volume"] or 0
                new_oi  = row["oi"] or 0
                old_oi  = old_row["oi"] or 0
                if old_vol < 50000 or new_vol < 100000:
                    continue
                vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0
                oi_chg_pct = ((new_oi - old_oi) / old_oi * 100) if old_oi > 0 else 0
                # Whale criteria: high vol/OI ratio or significant OI change
                if vol_oi_ratio > 2 or abs(oi_chg_pct) > 10:
                    strike = row["strike"]
                    uoa_whale_strikes.add(strike)
                    uoa_details[strike] = {
                        "vol_oi_ratio": round(vol_oi_ratio, 2),
                        "oi_chg_pct":   round(oi_chg_pct, 2),
                        "option_type":  row["option_type"],
                        "volume":       new_vol,
                    }
    except Exception as e:
        print(f"[EOD] UOA cross-ref failed: {e}")

    # ── Cross-reference: OI Jungle spike strikes ──────────────────────────────
    jungle_spike_strikes = set()  # strikes with sudden OI spike
    jungle_details = {}
    try:
        if len(uoa_timestamps) >= 2:
            # Reuse timestamps from above
            for row in new_snap:
                key = f"{row['strike']}_{row['option_type']}"
                old_row = old_map.get(key)
                if not old_row:
                    continue
                old_oi = old_row["oi"] or 0
                new_oi = row["oi"] or 0
                if old_oi < 1000:
                    continue
                oi_pct = ((new_oi - old_oi) / old_oi * 100) if old_oi > 0 else 0
                if abs(oi_pct) >= 10:
                    strike = row["strike"]
                    jungle_spike_strikes.add(strike)
                    jungle_details[strike] = {
                        "oi_pct":      round(oi_pct, 2),
                        "direction":   "BUILD" if oi_pct > 0 else "UNWIND",
                        "option_type": row["option_type"],
                    }
    except Exception as e:
        print(f"[EOD] Jungle cross-ref failed: {e}")

    # ── Add convergence flags to rows ─────────────────────────────────────────
    for row in rows:
        strike = row["strike"]
        has_whale  = strike in uoa_whale_strikes
        has_spike  = strike in jungle_spike_strikes
        row["has_whale"]     = has_whale
        row["has_spike"]     = has_spike
        row["high_conv"]     = has_whale and has_spike
        row["uoa_detail"]    = uoa_details.get(strike)
        row["jungle_detail"] = jungle_details.get(strike)

    # ── Smart Money Summary ───────────────────────────────────────────────────
    total_ce_chg = sum(r["ce_chg"] for r in rows)
    total_pe_chg = sum(r["pe_chg"] for r in rows)
    ce_built     = sum(r["ce_chg"] for r in rows if r["ce_chg"] > 0)
    pe_built     = sum(r["pe_chg"] for r in rows if r["pe_chg"] > 0)
    ce_unwound   = sum(r["ce_chg"] for r in rows if r["ce_chg"] < 0)
    pe_unwound   = sum(r["pe_chg"] for r in rows if r["pe_chg"] < 0)

    ce_rows_close = [(s, snap_close.get((s, "CE"), 0)) for s in all_strikes]
    pe_rows_close = [(s, snap_close.get((s, "PE"), 0)) for s in all_strikes]
    max_ce_strike = max(ce_rows_close, key=lambda x: x[1])[0] if ce_rows_close else 0
    max_pe_strike = max(pe_rows_close, key=lambda x: x[1])[0] if pe_rows_close else 0
    total_ce_oi   = sum(v for _, v in ce_rows_close)
    total_pe_oi   = sum(v for _, v in pe_rows_close)

    open_ce  = sum(snap_open.get((s, "CE"), 0) for s in all_strikes)
    open_pe  = sum(snap_open.get((s, "PE"), 0) for s in all_strikes)
    close_ce = sum(snap_close.get((s, "CE"), 0) for s in all_strikes)
    close_pe = sum(snap_close.get((s, "PE"), 0) for s in all_strikes)
    pcr_open  = round(open_pe / open_ce, 3) if open_ce > 0 else 0
    pcr_close = round(close_pe / close_ce, 3) if close_ce > 0 else 0
    pcr_trend = "RISING" if pcr_close > pcr_open else "FALLING" if pcr_close < pcr_open else "FLAT"

    bullish = (pe_built + abs(ce_unwound)) > (ce_built + abs(pe_unwound))

    if pcr_close > 1.2:
        bias = "BULLISH"; bias_strength = "Strong"
    elif pcr_close > 0.9:
        bias = "BULLISH" if bullish else "NEUTRAL"; bias_strength = "Moderate"
    elif pcr_close < 0.6:
        bias = "BEARISH"; bias_strength = "Strong"
    else:
        bias = "BEARISH" if not bullish else "NEUTRAL"; bias_strength = "Moderate"

    top_ce_builds  = sorted([r for r in rows if r["ce_chg"] > 0],  key=lambda x: x["ce_chg"],  reverse=True)[:5]
    top_pe_builds  = sorted([r for r in rows if r["pe_chg"] > 0],  key=lambda x: x["pe_chg"],  reverse=True)[:5]
    top_ce_unwinds = sorted([r for r in rows if r["ce_chg"] < 0],  key=lambda x: x["ce_chg"])[:5]
    top_pe_unwinds = sorted([r for r in rows if r["pe_chg"] < 0],  key=lambda x: x["pe_chg"])[:5]

    # ── Watchlist notes — all rule-based, no assumptions ─────────────────────
    watchlist_notes = []

    # Bias note
    if bias == "BULLISH":
        watchlist_notes.append(f"{'🐂' if bias_strength == 'Strong' else '↑'} {bias_strength} bullish bias — PE writers defending {max_pe_strike:,.0f} support")
    elif bias == "BEARISH":
        watchlist_notes.append(f"{'🐻' if bias_strength == 'Strong' else '↓'} {bias_strength} bearish bias — CE writers capping at {max_ce_strike:,.0f} resistance")
    else:
        watchlist_notes.append(f"↔ Neutral — range-bound between {max_pe_strike:,.0f} support and {max_ce_strike:,.0f} resistance")

    # PCR trend note
    if pcr_trend == "RISING":
        watchlist_notes.append(f"📈 PCR rose {pcr_open} → {pcr_close} through session — put writers more active as day progressed")
    elif pcr_trend == "FALLING":
        watchlist_notes.append(f"📉 PCR fell {pcr_open} → {pcr_close} through session — call writers more active as day progressed")

    # CE unwinding note
    if abs(ce_unwound) > abs(ce_built) * 0.5 and ce_unwound != 0:
        watchlist_notes.append(f"⚡ Significant CE unwinding ({fmtoi(abs(ce_unwound))}) — resistance at {max_ce_strike:,.0f} easing, watch for breakout")

    # PE unwinding note
    if abs(pe_unwound) > abs(pe_built) * 0.5 and pe_unwound != 0:
        watchlist_notes.append(f"⚠️ Significant PE unwinding ({fmtoi(abs(pe_unwound))}) — support at {max_pe_strike:,.0f} weakening")

    # Whale confirmation notes
    if max_pe_strike in uoa_whale_strikes:
        watchlist_notes.append(f"🐋 Whale activity confirmed at {max_pe_strike:,.0f} PE — institutional interest at support level")
    if max_ce_strike in uoa_whale_strikes:
        watchlist_notes.append(f"🐋 Whale activity confirmed at {max_ce_strike:,.0f} CE — institutional resistance being built")

    # High conviction convergence notes
    high_conv_strikes = [r["strike"] for r in rows if r["high_conv"]]
    if high_conv_strikes:
        watchlist_notes.append(f"🔥 High conviction at {', '.join(str(int(s)) for s in high_conv_strikes[:3])} — UOA whale + OI spike both firing")

    # Key levels for tomorrow
    watchlist_notes.append(f"📌 Key levels tomorrow: Support {max_pe_strike:,.0f} · Resistance {max_ce_strike:,.0f} · Range {max_ce_strike - max_pe_strike:,.0f} pts")

    return {
        "symbol":     symbol,
        "date":       active_date,
        "dates":      sorted_dates,
        "expiry":     active_expiry,
        "expiries":   expiries,
        "open_time":  to_ist(first_ts),
        "close_time": to_ist(last_ts),
        "snapshots":  len(timestamps),
        "journey":    journey_data,
        "rows":       rows,
        "summary": {
            "bias":             bias,
            "bias_strength":    bias_strength,
            "bullish":          bullish,
            "pcr_open":         pcr_open,
            "pcr_close":        pcr_close,
            "pcr_trend":        pcr_trend,
            "total_ce_chg":     total_ce_chg,
            "total_pe_chg":     total_pe_chg,
            "ce_built":         ce_built,
            "pe_built":         pe_built,
            "ce_unwound":       ce_unwound,
            "pe_unwound":       pe_unwound,
            "max_ce_strike":    max_ce_strike,
            "max_pe_strike":    max_pe_strike,
            "total_ce_oi":      total_ce_oi,
            "total_pe_oi":      total_pe_oi,
            "support_level":    max_pe_strike,
            "resistance_level": max_ce_strike,
            "top_ce_builds":    top_ce_builds,
            "top_pe_builds":    top_pe_builds,
            "top_ce_unwinds":   top_ce_unwinds,
            "top_pe_unwinds":   top_pe_unwinds,
            "watchlist_notes":  watchlist_notes,
            "whale_strikes":    list(uoa_whale_strikes),
            "spike_strikes":    list(jungle_spike_strikes),
            "high_conv_strikes": high_conv_strikes,
        }
    }

def fmtoi(n: int) -> str:
    if abs(n) >= 10000000: return f"{n/10000000:.2f}Cr"
    if abs(n) >= 100000:   return f"{n/100000:.1f}L"
    return str(n)
