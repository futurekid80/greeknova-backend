from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

def get_uoa(date: str = None):
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get latest two distinct timestamps for the requested date
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(200)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data), reverse=True)

    # Fallback: use last available day if not enough data
    if len(timestamps) < 2:
        fallback = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .order("timestamp", desc=True)\
            .limit(200)\
            .execute()
        timestamps = sorted(set(r["timestamp"] for r in fallback.data), reverse=True)

    if len(timestamps) < 2:
        return {"signals": [], "total": 0}

    ts_new = timestamps[0]
    ts_old = timestamps[1]

    # ── Paginated fetch for current snapshot ──────────────────────────────────
    new_data = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("*")\
            .eq("timestamp", ts_new)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        new_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # ── Paginated fetch for previous snapshot ─────────────────────────────────
    old_data = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("*")\
            .eq("timestamp", ts_old)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        old_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # ── CMP — paginate to cover all 66 symbols ───────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        cmp_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    cmp_map = {}
    seen_cmp = set()
    for c in cmp_raw:
        if c["symbol"] not in seen_cmp:
            cmp_map[c["symbol"]] = c["cmp"]
            seen_cmp.add(c["symbol"])

    old_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in old_data}

    # ── Today's volume history for baseline average ───────────────────────────
    today_data = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("symbol, tradingsymbol, volume")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        today_data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    vol_history: dict = {}
    for r in today_data:
        k = f"{r['symbol']}_{r['tradingsymbol']}"
        if k not in vol_history:
            vol_history[k] = []
        vol_history[k].append(r["volume"] or 0)

    avg_vol = {k: sum(v)/len(v) for k, v in vol_history.items() if len(v) > 1}

    uoa_signals = []

    for row in new_data:
        sym = row["symbol"]
        ts = row["tradingsymbol"]
        key = f"{sym}_{ts}"
        old_row = old_map.get(key)
        if not old_row:
            continue

        cmp = cmp_map.get(sym, 0)
        strike = row["strike"]
        opt_type = row["option_type"]
        new_vol = row["volume"] or 0
        old_vol = old_row["volume"] or 0
        new_oi = row["oi"] or 0
        old_oi = old_row["oi"] or 0
        new_ltp = row["last_price"] or 0
        old_ltp = old_row["last_price"] or 0

        # ── FIX: Raised minimum volume threshold from 50k to 100k ─────────────
        # 50k was too low — created too many low-quality signals (431 signals)
        # 100k filters to genuinely significant activity only
        if new_vol < 100000:
            continue

        avg = avg_vol.get(key, new_vol)
        vol_ratio = new_vol / avg if avg > 0 else 1
        vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0
        oi_change_pct = ((new_oi - old_oi) / old_oi * 100) if old_oi > 0 else 0
        vol_change_pct = ((new_vol - old_vol) / old_vol * 100) if old_vol > 0 else 0

        # ── Option price direction — key to correct signal classification ──────
        ltp_change_pct = ((new_ltp - old_ltp) / old_ltp * 100) if old_ltp > 0 else 0
        price_rising = ltp_change_pct > 0.5   # small buffer to avoid noise
        price_falling = ltp_change_pct < -0.5
        oi_rising = oi_change_pct > 2
        oi_falling = oi_change_pct < -2

        # OTM calculation
        if cmp > 0:
            dist_pct = ((strike - cmp) / cmp * 100)
            is_otm = (opt_type == 'CE' and strike > cmp) or (opt_type == 'PE' and strike < cmp)
            otm_pct = abs(dist_pct) if is_otm else 0
        else:
            otm_pct = 0
            is_otm = False

        # ── Tightened scoring ─────────────────────────────────────────────────
        score = 0
        if vol_ratio > 6: score += 2      # was 5
        elif vol_ratio > 4: score += 1    # was 3
        if vol_oi_ratio > 4: score += 2   # was 3
        elif vol_oi_ratio > 2: score += 1 # was 1.5
        if otm_pct > 3 and new_vol > 200000: score += 1  # was 100k
        if abs(oi_change_pct) > 15 and vol_change_pct > 20: score += 1

        # ── Backend minimum score filter — reduces noise before sending to FE ──
        # Previously only UI had score filter; now enforce min 3 in backend too
        if score < 3:
            continue

        # ── FIXED signal classification using OI + option price direction ──────
        if oi_rising and price_rising:
            if opt_type == 'CE':
                signal_type = "LONG_BUILDUP"
                signal_desc = "OI ↑ + CE price ↑ — fresh call buyers, bullish directional bet"
                bias = "BULLISH"
            else:
                signal_type = "SHORT_BUILDUP"
                signal_desc = "OI ↑ + PE price ↑ — fresh put buyers, bearish directional bet"
                bias = "BEARISH"

        elif oi_rising and price_falling:
            if opt_type == 'CE':
                signal_type = "CALL_WRITING"
                signal_desc = "OI ↑ + CE price ↓ — call writers shorting, bearish on stock"
                bias = "BEARISH"
            else:
                signal_type = "PUT_WRITING"
                signal_desc = "OI ↑ + PE price ↓ — put writers shorting, bullish on stock"
                bias = "BULLISH"

        elif oi_falling and price_rising:
            if opt_type == 'CE':
                signal_type = "SHORT_COVERING"
                signal_desc = "OI ↓ + CE price ↑ — call shorts covering, bullish squeeze"
                bias = "BULLISH"
            else:
                signal_type = "LONG_UNWINDING"
                signal_desc = "OI ↓ + PE price ↑ — put longs exiting, bullish for stock"
                bias = "BULLISH"

        elif oi_falling and price_falling:
            if opt_type == 'CE':
                signal_type = "LONG_UNWINDING"
                signal_desc = "OI ↓ + CE price ↓ — call longs exiting, bearish for stock"
                bias = "BEARISH"
            else:
                signal_type = "SHORT_COVERING"
                signal_desc = "OI ↓ + PE price ↓ — put shorts covering, bearish squeeze"
                bias = "BEARISH"

        elif vol_oi_ratio > 2:
            # High volume, OI not moving much = buyer/seller battle at current price
            if price_rising:
                signal_type = "BUYER_DOMINATED"
                signal_desc = "High vol + price ↑ + flat OI — buyers absorbing sellers"
                bias = "BULLISH"
            elif price_falling:
                signal_type = "SELLER_DOMINATED"
                signal_desc = "High vol + price ↓ + flat OI — sellers absorbing buyers"
                bias = "BEARISH"
            else:
                # Price flat with high vol — skip, not actionable
                continue

        elif otm_pct > 3 and new_vol > 200000:
            bias = "BULLISH" if opt_type == "PE" else "BEARISH"
            signal_type = "FAR_OTM_ACTIVITY"
            signal_desc = f"{otm_pct:.1f}% OTM with heavy volume — hedging or speculative bet"

        elif vol_ratio > 4:
            if price_rising:
                bias = "BULLISH" if opt_type == "CE" else "BEARISH"
            elif price_falling:
                bias = "BEARISH" if opt_type == "CE" else "BULLISH"
            else:
                continue  # volume surge with no price direction = skip
            signal_type = "VOLUME_SURGE"
            signal_desc = f"{vol_ratio:.1f}x normal volume — strong directional interest"

        else:
            # No clear signal — skip rather than show noise
            continue

        uoa_signals.append({
            "symbol":         sym,
            "tradingsymbol":  ts,
            "strike":         strike,
            "option_type":    opt_type,
            "cmp":            float(cmp),
            "ltp":            float(new_ltp),
            "ltp_change_pct": round(ltp_change_pct, 2),
            "volume":         new_vol,
            "oi":             new_oi,
            "vol_oi_ratio":   round(vol_oi_ratio, 2),
            "vol_ratio":      round(vol_ratio, 2),
            "oi_change_pct":  round(oi_change_pct, 2),
            "vol_change_pct": round(vol_change_pct, 2),
            "otm_pct":        round(otm_pct, 2),
            "is_otm":         is_otm,
            "signal_type":    signal_type,
            "signal_desc":    signal_desc,
            "bias":           bias,
            "score":          min(score, 5),
            "is_index":       sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        })

    uoa_signals.sort(key=lambda x: (x["score"], x["volume"]), reverse=True)

    return {
        "timestamp": ts_new,
        "date":      today,
        "total":     len(uoa_signals),
        "signals":   uoa_signals[:50],
    }
