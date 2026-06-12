from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type


def fmtoi(n: int) -> str:
    if abs(n) >= 10000000: return f"{n/10000000:.2f}Cr"
    if abs(n) >= 100000:   return f"{n/100000:.1f}L"
    return str(n)


def get_eod_analysis(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    supabase = get_supabase()

    # ── SPEED FIX: Get available dates in ONE query (was 60 separate queries) ─
    sixty_days_ago = (datetime.now(timezone.utc).date() - timedelta(days=60)).isoformat()
    dates_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{sixty_days_ago}T00:00:00+00:00")\
        .order("timestamp", desc=True)\
        .limit(500)\
        .execute()

    if not dates_result.data:
        return {"symbol": symbol, "dates": [], "rows": []}

    sorted_dates = sorted(set(r["timestamp"][:10] for r in dates_result.data))

    # ── Default to last full trading day ──────────────────────────────────────
    if date:
        active_date = date
    else:
        active_date = sorted_dates[-1]
        date_counts: dict = {}
        for r in dates_result.data:
            d = r["timestamp"][:10]
            date_counts[d] = date_counts.get(d, 0) + 1
        for d in reversed(sorted_dates):
            if date_counts.get(d, 0) >= 5:
                active_date = d
                break

    # ── Paginated timestamp fetch (fixes close_time showing wrong time) ───────
    all_ts_data = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{active_date}T00:00:00+00:00")\
            .lt("timestamp",  f"{active_date}T23:59:59+00:00")\
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

    # Days to expiry
    days_to_expiry = None
    if active_expiry:
        try:
            exp_date = datetime.strptime(active_expiry, "%Y-%m-%d").date()
            active_date_obj = datetime.strptime(active_date, "%Y-%m-%d").date()
            days_to_expiry = (exp_date - active_date_obj).days
        except:
            pass

    # ── CMP — latest price for the symbol ────────────────────────────────────
    cmp_data = supabase.from_("cmp_prices")\
        .select("cmp, timestamp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=True)\
        .limit(1).execute()
    cmp = float(cmp_data.data[0]["cmp"]) if cmp_data.data else 0

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
        .select("timestamp, option_type, oi, strike")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp",  f"{active_date}T23:59:59+00:00")
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
    pcr_values = []
    for ts in sorted(ts_groups.keys()):
        ce = ts_groups[ts]["ce"]
        pe = ts_groups[ts]["pe"]
        pcr = round(pe / ce, 3) if ce > 0 else 0
        pcr_values.append(pcr)
        journey_data.append({"time": to_ist(ts), "ce_oi": ce, "pe_oi": pe, "pcr": pcr})

    # PCR high/low through the day
    pcr_high = max(pcr_values) if pcr_values else 0
    pcr_low  = min(pcr_values) if pcr_values else 0
    pcr_high_time = journey_data[pcr_values.index(pcr_high)]["time"] if pcr_values else ""
    pcr_low_time  = journey_data[pcr_values.index(pcr_low)]["time"]  if pcr_values else ""

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
            "ce_open":  ce_open,  "ce_close": ce_close, "ce_chg": ce_chg,
            "pe_open":  pe_open,  "pe_close": pe_close, "pe_chg": pe_chg,
            "net_chg":  pe_chg - ce_chg,
        })

    # ── Max Pain ──────────────────────────────────────────────────────────────
    ce_rows_close = [(s, snap_close.get((s, "CE"), 0)) for s in all_strikes]
    pe_rows_close = [(s, snap_close.get((s, "PE"), 0)) for s in all_strikes]
    max_pain = all_strikes[0] if all_strikes else 0
    min_loss = float('inf')
    for s in all_strikes:
        loss = 0
        for strike, oi in ce_rows_close:
            if s > strike: loss += (s - strike) * oi
        for strike, oi in pe_rows_close:
            if s < strike: loss += (strike - s) * oi
        if loss < min_loss:
            min_loss = loss
            max_pain = s

    # ── Top OI concentration at close ────────────────────────────────────────
    top_ce_oi = sorted(ce_rows_close, key=lambda x: x[1], reverse=True)[:5]
    top_pe_oi = sorted(pe_rows_close, key=lambda x: x[1], reverse=True)[:5]

    # ── Cross-reference: UOA whale + OI Jungle spikes ────────────────────────
    uoa_whale_strikes = set()
    uoa_details: dict = {}
    jungle_spike_strikes = set()
    jungle_details: dict = {}
    try:
        uoa_ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{active_date}T00:00:00+00:00")\
            .lt("timestamp",  f"{active_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(4).execute()

        uoa_timestamps = sorted(set(r["timestamp"] for r in (uoa_ts_q.data or [])), reverse=True)
        if len(uoa_timestamps) >= 2:
            ts_new_uoa = uoa_timestamps[0]
            ts_old_uoa = uoa_timestamps[1]

            new_snap_uoa = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi, volume")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts_new_uoa)\
                .limit(5000).execute().data or []

            old_snap_uoa = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi, volume")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts_old_uoa)\
                .limit(5000).execute().data or []

            old_map_uoa = {f"{r['strike']}_{r['option_type']}": r for r in old_snap_uoa}

            for row in new_snap_uoa:
                key = f"{row['strike']}_{row['option_type']}"
                old_row = old_map_uoa.get(key)
                if not old_row:
                    continue
                new_vol = row["volume"] or 0
                old_vol = old_row["volume"] or 0
                new_oi  = row["oi"] or 0
                old_oi  = old_row["oi"] or 0
                if new_vol < 100000:
                    continue
                vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0
                oi_chg_pct   = ((new_oi - old_oi) / old_oi * 100) if old_oi > 0 else 0
                strike = row["strike"]
                if vol_oi_ratio > 2 or abs(oi_chg_pct) > 10:
                    uoa_whale_strikes.add(strike)
                    uoa_details[strike] = {
                        "vol_oi_ratio": round(vol_oi_ratio, 2),
                        "oi_chg_pct":   round(oi_chg_pct, 2),
                        "option_type":  row["option_type"],
                        "volume":       new_vol,
                    }
                if old_oi >= 1000 and abs(oi_chg_pct) >= 10:
                    jungle_spike_strikes.add(strike)
                    jungle_details[strike] = {
                        "oi_pct":      round(oi_chg_pct, 2),
                        "direction":   "BUILD" if oi_chg_pct > 0 else "UNWIND",
                        "option_type": row["option_type"],
                    }
    except Exception as e:
        print(f"[EOD] Cross-ref failed: {e}")

    # Add convergence flags to rows
    for row in rows:
        strike = row["strike"]
        has_whale = strike in uoa_whale_strikes
        has_spike = strike in jungle_spike_strikes
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

    top_ce_builds  = sorted([r for r in rows if r["ce_chg"] > 0], key=lambda x: x["ce_chg"],  reverse=True)[:5]
    top_pe_builds  = sorted([r for r in rows if r["pe_chg"] > 0], key=lambda x: x["pe_chg"],  reverse=True)[:5]
    top_ce_unwinds = sorted([r for r in rows if r["ce_chg"] < 0], key=lambda x: x["ce_chg"])[:5]
    top_pe_unwinds = sorted([r for r in rows if r["pe_chg"] < 0], key=lambda x: x["pe_chg"])[:5]
    high_conv_strikes = [r["strike"] for r in rows if r["high_conv"]]

    # CMP relative to key levels
    dist_to_resistance = round((max_ce_strike - cmp) / cmp * 100, 2) if cmp > 0 and max_ce_strike > 0 else None
    dist_to_support    = round((cmp - max_pe_strike) / cmp * 100, 2) if cmp > 0 and max_pe_strike > 0 else None
    dist_to_max_pain   = round((cmp - max_pain) / cmp * 100, 2)      if cmp > 0 and max_pain > 0 else None

    # Position of CMP in range
    cmp_position = "ABOVE_RESISTANCE" if cmp > max_ce_strike else \
                   "BELOW_SUPPORT"    if cmp < max_pe_strike else \
                   "IN_RANGE"

    # ── Watchlist notes ───────────────────────────────────────────────────────
    watchlist_notes = []

    # CMP context first — most important
    if cmp > 0:
        if cmp_position == "IN_RANGE":
            watchlist_notes.append(
                f"📍 {symbol} at {cmp:,.1f} — inside range · "
                f"{dist_to_support:.1f}% above support {max_pe_strike:,.0f} · "
                f"{dist_to_resistance:.1f}% below resistance {max_ce_strike:,.0f}"
            )
        elif cmp_position == "ABOVE_RESISTANCE":
            watchlist_notes.append(
                f"🚀 {symbol} at {cmp:,.1f} — trading ABOVE resistance {max_ce_strike:,.0f} · "
                f"Breakout territory · Watch for pullback or continuation"
            )
        else:
            watchlist_notes.append(
                f"⚠️ {symbol} at {cmp:,.1f} — trading BELOW support {max_pe_strike:,.0f} · "
                f"Breakdown territory · Bulls need to reclaim {max_pe_strike:,.0f}"
            )

    # Bias
    if bias == "BULLISH":
        watchlist_notes.append(
            f"{'🐂' if bias_strength == 'Strong' else '↑'} {bias_strength} bullish bias — "
            f"PE writers defending {max_pe_strike:,.0f} support"
        )
    elif bias == "BEARISH":
        watchlist_notes.append(
            f"{'🐻' if bias_strength == 'Strong' else '↓'} {bias_strength} bearish bias — "
            f"CE writers capping at {max_ce_strike:,.0f} resistance"
        )
    else:
        watchlist_notes.append(
            f"↔ Neutral — range-bound between {max_pe_strike:,.0f} and {max_ce_strike:,.0f}"
        )

    # PCR trend
    if pcr_trend == "RISING":
        watchlist_notes.append(
            f"📈 PCR rose {pcr_open} → {pcr_close} — put writers more active as day progressed · bullish tilt building"
        )
    elif pcr_trend == "FALLING":
        watchlist_notes.append(
            f"📉 PCR fell {pcr_open} → {pcr_close} — call writers dominated second half · bearish pressure"
        )

    # PCR range
    if pcr_high > 0 and pcr_low > 0 and (pcr_high - pcr_low) > 0.1:
        watchlist_notes.append(
            f"📊 PCR ranged {pcr_low} ({pcr_low_time}) → {pcr_high} ({pcr_high_time}) — "
            f"intraday swing of {round(pcr_high - pcr_low, 3)} shows {'indecision' if (pcr_high - pcr_low) > 0.3 else 'moderate sentiment shift'}"
        )

    # Max Pain
    if dist_to_max_pain is not None:
        direction = "above" if dist_to_max_pain > 0 else "below"
        watchlist_notes.append(
            f"🎯 Max Pain at {max_pain:,.0f} — CMP is {abs(dist_to_max_pain):.1f}% {direction} · "
            f"{'expect drift down toward Max Pain' if dist_to_max_pain > 1 else 'expect drift up toward Max Pain' if dist_to_max_pain < -1 else 'CMP near Max Pain — pin risk high'}"
        )

    # Expiry context
    if days_to_expiry is not None:
        if days_to_expiry <= 2:
            watchlist_notes.append(
                f"⏰ {days_to_expiry} day(s) to expiry — Max Pain gravity very strong · "
                f"expect {symbol} to gravitate toward {max_pain:,.0f}"
            )
        elif days_to_expiry <= 7:
            watchlist_notes.append(
                f"📅 {days_to_expiry} days to expiry — Max Pain becoming relevant · monitor {max_pain:,.0f}"
            )

    # CE unwinding
    if abs(ce_unwound) > abs(ce_built) * 0.5 and ce_unwound != 0:
        watchlist_notes.append(
            f"⚡ CE unwinding ({fmtoi(abs(ce_unwound))}) at {max_ce_strike:,.0f} — "
            f"resistance easing · potential breakout zone if bulls hold"
        )

    # PE unwinding
    if abs(pe_unwound) > abs(pe_built) * 0.5 and pe_unwound != 0:
        watchlist_notes.append(
            f"⚠️ PE unwinding ({fmtoi(abs(pe_unwound))}) at {max_pe_strike:,.0f} — "
            f"support weakening · watch for breakdown below {max_pe_strike:,.0f}"
        )

    # Whale confirmations
    if max_pe_strike in uoa_whale_strikes:
        watchlist_notes.append(
            f"🐋 Whale activity confirmed at {max_pe_strike:,.0f} PE — "
            f"institutional interest at support · high conviction level"
        )
    if max_ce_strike in uoa_whale_strikes:
        watchlist_notes.append(
            f"🐋 Whale activity at {max_ce_strike:,.0f} CE — "
            f"institutional resistance · strong ceiling"
        )

    # High conviction
    if high_conv_strikes:
        watchlist_notes.append(
            f"🔥 High conviction at {', '.join(str(int(s)) for s in high_conv_strikes[:3])} — "
            f"UOA whale + OI spike both firing · key levels to watch"
        )

    # Final key levels line
    watchlist_notes.append(
        f"📌 Key levels: Support {max_pe_strike:,.0f} · Resistance {max_ce_strike:,.0f} · "
        f"Max Pain {max_pain:,.0f} · Range {max_ce_strike - max_pe_strike:,.0f} pts"
    )

    return {
        "symbol":        symbol,
        "date":          active_date,
        "dates":         sorted_dates,
        "expiry":        active_expiry,
        "expiries":      expiries,
        "open_time":     to_ist(first_ts),
        "close_time":    to_ist(last_ts),
        "snapshots":     len(timestamps),
        "cmp":           cmp,
        "journey":       journey_data,
        "rows":          rows,
        "summary": {
            "bias":               bias,
            "bias_strength":      bias_strength,
            "bullish":            bullish,
            "pcr_open":           pcr_open,
            "pcr_close":          pcr_close,
            "pcr_trend":          pcr_trend,
            "pcr_high":           pcr_high,
            "pcr_low":            pcr_low,
            "pcr_high_time":      pcr_high_time,
            "pcr_low_time":       pcr_low_time,
            "total_ce_chg":       total_ce_chg,
            "total_pe_chg":       total_pe_chg,
            "ce_built":           ce_built,
            "pe_built":           pe_built,
            "ce_unwound":         ce_unwound,
            "pe_unwound":         pe_unwound,
            "max_ce_strike":      max_ce_strike,
            "max_pe_strike":      max_pe_strike,
            "total_ce_oi":        total_ce_oi,
            "total_pe_oi":        total_pe_oi,
            "max_pain":           max_pain,
            "days_to_expiry":     days_to_expiry,
            "cmp":                cmp,
            "dist_to_resistance": dist_to_resistance,
            "dist_to_support":    dist_to_support,
            "dist_to_max_pain":   dist_to_max_pain,
            "cmp_position":       cmp_position,
            "support_level":      max_pe_strike,
            "resistance_level":   max_ce_strike,
            "top_ce_builds":      top_ce_builds,
            "top_pe_builds":      top_pe_builds,
            "top_ce_unwinds":     top_ce_unwinds,
            "top_pe_unwinds":     top_pe_unwinds,
            "top_ce_oi":          [{"strike": s, "oi": o} for s, o in top_ce_oi],
            "top_pe_oi":          [{"strike": s, "oi": o} for s, o in top_pe_oi],
            "watchlist_notes":    watchlist_notes,
            "whale_strikes":      list(uoa_whale_strikes),
            "spike_strikes":      list(jungle_spike_strikes),
            "high_conv_strikes":  high_conv_strikes,
        }
    }
