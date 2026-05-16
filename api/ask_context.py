from utils.db import get_supabase
from datetime import datetime, timezone, timedelta


def get_ask_context(symbol: str = "NIFTY"):
    """
    Assembles a rich context object for the Claude Ask feature.
    Pulls: latest OI structure, PCR, Max Pain, UOA signals,
    IVR/Expected Move, Options Jungle spikes, 7-day PCR trend.
    Returns clean text context Claude can reason over.
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    sym = symbol.upper()

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

    def fmtoi(n):
        if abs(n) >= 10000000: return f"{n/10000000:.2f}Cr"
        if abs(n) >= 100000:   return f"{n/100000:.1f}L"
        return str(n)

    context_parts = []
    context_parts.append(f"=== GreekNova Market Data for {sym} — {today} ===")
    context_parts.append(f"Data source: NSE F&O options snapshots captured every 5 minutes")
    context_parts.append(f"All analysis is observational — based on publicly available NSE OI data\n")

    # ── 1. CMP ────────────────────────────────────────────────────────────────
    try:
        cmp_q = supabase.from_("cmp_prices")\
            .select("cmp, timestamp")\
            .eq("symbol", sym)\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if cmp_q.data:
            cmp = float(cmp_q.data[0]["cmp"])
            cmp_time = to_ist(cmp_q.data[0]["timestamp"])
            context_parts.append(f"CURRENT MARKET PRICE: ₹{cmp:,.2f} (as of {cmp_time} IST)")
    except:
        cmp = 0

    # ── 2. Latest OI snapshot — top strikes ──────────────────────────────────
    try:
        ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", sym)\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()

        if not ts_q.data:
            # fallback to last available
            ts_q = supabase.from_("oi_snapshots")\
                .select("timestamp")\
                .eq("symbol", sym)\
                .order("timestamp", desc=True)\
                .limit(1).execute()

        if ts_q.data:
            latest_ts = ts_q.data[0]["timestamp"]

            # Get expiries
            exp_q = supabase.from_("oi_snapshots")\
                .select("expiry")\
                .eq("symbol", sym)\
                .eq("timestamp", latest_ts)\
                .execute()
            expiries = sorted(set(
                r["expiry"] for r in (exp_q.data or [])
                if r["expiry"] and r["expiry"] >= today
            ))
            nearest_expiry = expiries[0] if expiries else None

            # Fetch OI for nearest expiry
            oi_q = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi, last_price")\
                .eq("symbol", sym)\
                .eq("timestamp", latest_ts)\
                .limit(2000).execute()

            if nearest_expiry:
                oi_data = [r for r in (oi_q.data or []) if r.get("expiry") == nearest_expiry or True]
            else:
                oi_data = oi_q.data or []

            # Top CE and PE by OI
            ce_rows = [(r["strike"], r["oi"] or 0, r["last_price"] or 0) for r in oi_data if r["option_type"] == "CE"]
            pe_rows = [(r["strike"], r["oi"] or 0, r["last_price"] or 0) for r in oi_data if r["option_type"] == "PE"]

            ce_rows.sort(key=lambda x: x[1], reverse=True)
            pe_rows.sort(key=lambda x: x[1], reverse=True)

            total_ce = sum(x[1] for x in ce_rows)
            total_pe = sum(x[1] for x in pe_rows)
            pcr_now  = round(total_pe / total_ce, 3) if total_ce > 0 else 0

            # Max Pain
            all_strikes = sorted(set(r["strike"] for r in oi_data))
            strike_oi = {}
            for r in oi_data:
                s = r["strike"]
                if s not in strike_oi:
                    strike_oi[s] = {"CE": 0, "PE": 0}
                strike_oi[s][r["option_type"]] = r["oi"] or 0

            max_pain = all_strikes[0] if all_strikes else 0
            min_loss = float('inf')
            for s in all_strikes:
                loss = sum((s - k) * strike_oi.get(k, {}).get("CE", 0) for k in all_strikes if s > k)
                loss += sum((k - s) * strike_oi.get(k, {}).get("PE", 0) for k in all_strikes if s < k)
                if loss < min_loss:
                    min_loss = loss
                    max_pain = s

            max_ce = ce_rows[0] if ce_rows else (0, 0, 0)
            max_pe = pe_rows[0] if pe_rows else (0, 0, 0)

            context_parts.append(f"\nOPTION CHAIN SNAPSHOT (as of {to_ist(latest_ts)} IST):")
            context_parts.append(f"  Expiry: {nearest_expiry}")
            context_parts.append(f"  Total CE OI: {fmtoi(total_ce)} | Total PE OI: {fmtoi(total_pe)}")
            context_parts.append(f"  Current PCR: {pcr_now} ({'Bullish bias' if pcr_now > 1.0 else 'Bearish bias' if pcr_now < 0.8 else 'Neutral'})")
            context_parts.append(f"  Max CE Wall (resistance): {max_ce[0]:,.0f} — OI {fmtoi(max_ce[1])}, LTP ₹{max_ce[2]}")
            context_parts.append(f"  Max PE Wall (support): {max_pe[0]:,.0f} — OI {fmtoi(max_pe[1])}, LTP ₹{max_pe[2]}")
            context_parts.append(f"  Max Pain: {max_pain:,.0f}")
            if cmp > 0:
                context_parts.append(f"  CMP vs Max Pain: {'+' if cmp > max_pain else ''}{((cmp - max_pain)/cmp*100):.2f}%")
                context_parts.append(f"  CMP vs Resistance: {((max_ce[0] - cmp)/cmp*100):.2f}% away")
                context_parts.append(f"  CMP vs Support: {((cmp - max_pe[0])/cmp*100):.2f}% above")

            context_parts.append(f"\n  Top 5 CE strikes by OI (resistance levels):")
            for s, oi, ltp in ce_rows[:5]:
                context_parts.append(f"    {s:,.0f} CE — OI: {fmtoi(oi)}, LTP: ₹{ltp}")

            context_parts.append(f"\n  Top 5 PE strikes by OI (support levels):")
            for s, oi, ltp in pe_rows[:5]:
                context_parts.append(f"    {s:,.0f} PE — OI: {fmtoi(oi)}, LTP: ₹{ltp}")

    except Exception as e:
        context_parts.append(f"\nOI snapshot unavailable: {e}")

    # ── 3. Today's OI journey — PCR open vs close ─────────────────────────────
    try:
        journey_q = supabase.from_("oi_snapshots")\
            .select("timestamp, option_type, oi")\
            .eq("symbol", sym)\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(5000).execute()

        ts_groups: dict = {}
        for r in (journey_q.data or []):
            ts = r["timestamp"]
            if ts not in ts_groups:
                ts_groups[ts] = {"ce": 0, "pe": 0}
            if r["option_type"] == "CE":
                ts_groups[ts]["ce"] += r["oi"] or 0
            else:
                ts_groups[ts]["pe"] += r["oi"] or 0

        if ts_groups:
            sorted_ts = sorted(ts_groups.keys())
            first = ts_groups[sorted_ts[0]]
            last  = ts_groups[sorted_ts[-1]]
            pcr_open  = round(first["pe"] / first["ce"], 3) if first["ce"] > 0 else 0
            pcr_close = round(last["pe"] / last["ce"], 3) if last["ce"] > 0 else 0
            pcr_trend = "RISING" if pcr_close > pcr_open else "FALLING" if pcr_close < pcr_open else "FLAT"

            # PCR high and low
            pcr_values = [round(v["pe"]/v["ce"], 3) for v in ts_groups.values() if v["ce"] > 0]
            pcr_high = max(pcr_values) if pcr_values else 0
            pcr_low  = min(pcr_values) if pcr_values else 0

            context_parts.append(f"\nINTRADAY PCR JOURNEY ({to_ist(sorted_ts[0])} → {to_ist(sorted_ts[-1])} IST):")
            context_parts.append(f"  PCR at open: {pcr_open} | PCR now: {pcr_close} | Trend: {pcr_trend}")
            context_parts.append(f"  PCR range today: Low {pcr_low} → High {pcr_high} (swing: {round(pcr_high-pcr_low,3)})")
            context_parts.append(f"  CE OI change today: {'+' if last['ce'] > first['ce'] else ''}{fmtoi(last['ce'] - first['ce'])}")
            context_parts.append(f"  PE OI change today: {'+' if last['pe'] > first['pe'] else ''}{fmtoi(last['pe'] - first['pe'])}")

    except Exception as e:
        context_parts.append(f"\nPCR journey unavailable: {e}")

    # ── 4. UOA signals — latest ───────────────────────────────────────────────
    try:
        from api.uoa import get_uoa
        uoa_data = get_uoa()
        signals = [s for s in (uoa_data.get("signals") or []) if s["symbol"] == sym]

        if signals:
            context_parts.append(f"\nUOA SIGNALS FOR {sym} (unusual options activity):")
            for sig in signals[:5]:
                context_parts.append(
                    f"  {sig['strike']} {sig['option_type']} — {sig['signal_type'].replace('_',' ')} | "
                    f"Score {sig['score']}/5 | OI 30m: {'+' if sig['oi_chg_30min'] > 0 else ''}{sig['oi_chg_30min']}% | "
                    f"LTP from open: {'+' if sig['ltp_chg_from_open'] > 0 else ''}{sig['ltp_chg_from_open']}% | "
                    f"Bias: {sig['bias']}"
                )
        else:
            # Get top signals across all symbols
            all_sigs = (uoa_data.get("signals") or [])[:5]
            if all_sigs:
                context_parts.append(f"\nTOP UOA SIGNALS TODAY (all symbols):")
                for sig in all_sigs:
                    context_parts.append(
                        f"  {sig['symbol']} {sig['strike']} {sig['option_type']} — "
                        f"{sig['signal_type'].replace('_',' ')} | Score {sig['score']}/5 | {sig['bias']}"
                    )
    except Exception as e:
        context_parts.append(f"\nUOA data unavailable: {e}")

    # ── 5. Options Jungle — latest spikes ─────────────────────────────────────
    try:
        from api.options_jungle import get_options_jungle
        jungle = get_options_jungle(oi_threshold=10.0, vol_threshold=50.0)

        oi_spikes = [s for s in (jungle.get("oi_spikes") or []) if s["symbol"] == sym][:3]
        if oi_spikes:
            context_parts.append(f"\nOPTIONS JUNGLE — OI SPIKES FOR {sym}:")
            for s in oi_spikes:
                context_parts.append(
                    f"  {s['strike']} {s['option_type']} — OI {'+' if s['oi_pct'] > 0 else ''}{s['oi_pct']}% | "
                    f"LTP Δ: {'+' if s['ltp_chg_pct'] > 0 else ''}{s['ltp_chg_pct']}% | "
                    f"{s.get('interpretation','').replace('_',' ')}"
                )
    except Exception as e:
        context_parts.append(f"\nOptions Jungle data unavailable: {e}")

    # ── 6. IV Analysis ────────────────────────────────────────────────────────
    try:
        from api.iv_analysis import get_iv_analysis, _get_cache
        iv_data = get_iv_analysis(symbol=sym)
        if iv_data.get("results"):
            iv = iv_data["results"][0]
            context_parts.append(f"\nIV ANALYSIS FOR {sym}:")
            context_parts.append(f"  Current IV: {iv['current_iv']}% (CE: {iv['iv_ce']}%, PE: {iv['iv_pe']}%)")
            context_parts.append(f"  IVR: {iv['ivr']} — {iv['iv_signal'].replace('_',' ')} ({iv['iv_label']})")
            context_parts.append(f"  IVP: {iv['ivp']}th percentile ({iv['iv_history_days']} days history)")
            context_parts.append(f"  ATM Straddle: ₹{iv['atm_straddle']} ({iv['atm_strike']:,.0f} strike)")
            context_parts.append(f"  Expected Move (1SD, 68%): ±{iv['expected_move_pts']} pts (±{iv['expected_move_pct']}%)")
            context_parts.append(f"  Expected range by expiry: {iv['lower_range']:,.1f} – {iv['upper_range']:,.1f}")
            context_parts.append(f"  DTE: {iv['dte']} days to {iv['expiry']}")
            if iv['strategies']:
                context_parts.append(f"  Strategy context (based on IVR): {', '.join(iv['strategies'])}")
    except Exception as e:
        context_parts.append(f"\nIV data unavailable: {e}")

    # ── 7. 7-day PCR trend ────────────────────────────────────────────────────
    try:
        pcr_7day = []
        base = datetime.now(timezone.utc).date()
        for i in range(1, 8):
            d = (base - timedelta(days=i)).isoformat()
            day_q = supabase.from_("oi_snapshots")\
                .select("timestamp, option_type, oi")\
                .eq("symbol", sym)\
                .gte("timestamp", f"{d}T09:00:00+00:00")\
                .lt("timestamp",  f"{d}T23:59:59+00:00")\
                .order("timestamp", desc=True)\
                .limit(500).execute()

            if day_q.data:
                ts_grp: dict = {}
                for r in day_q.data:
                    ts = r["timestamp"]
                    if ts not in ts_grp:
                        ts_grp[ts] = {"ce": 0, "pe": 0}
                    if r["option_type"] == "CE":
                        ts_grp[ts]["ce"] += r["oi"] or 0
                    else:
                        ts_grp[ts]["pe"] += r["oi"] or 0

                if ts_grp:
                    last_ts_data = ts_grp[sorted(ts_grp.keys())[-1]]
                    day_pcr = round(last_ts_data["pe"] / last_ts_data["ce"], 3) if last_ts_data["ce"] > 0 else 0
                    if day_pcr > 0:
                        pcr_7day.append((d, day_pcr))

        if pcr_7day:
            context_parts.append(f"\n7-DAY PCR TREND (EOD values):")
            for d, pcr in reversed(pcr_7day):
                trend_char = "↑" if pcr > 1.0 else "↓" if pcr < 0.8 else "→"
                context_parts.append(f"  {d}: PCR {pcr} {trend_char}")

    except Exception as e:
        context_parts.append(f"\n7-day PCR unavailable: {e}")

    context_parts.append(f"\n=== END OF DATA ===")
    context_parts.append(f"Note: All figures from NSE publicly available options data.")
    context_parts.append(f"This context is for observational analysis only.")

    return {
        "symbol":    sym,
        "date":      today,
        "context":   "\n".join(context_parts),
        "cmp":       cmp,
        "generated": datetime.now(timezone.utc).isoformat(),
    }
