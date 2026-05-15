from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

def get_uoa(date: str = None):
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Get ALL distinct timestamps for today (NIFTY as proxy) ───────────────
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .limit(500)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data))

    # Fallback: use last available day if not enough data
    if len(timestamps) < 2:
        fallback = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .order("timestamp", desc=True)\
            .limit(200)\
            .execute()
        timestamps = sorted(set(r["timestamp"] for r in fallback.data))

    if len(timestamps) < 2:
        return {"signals": [], "total": 0}

    # ── A+B snapshot selection ────────────────────────────────────────────────
    # ts_open  = first snapshot of day (9:20-9:25 AM) — price direction baseline
    # ts_30min = 30 mins ago (6 snapshots back) — OI momentum window
    # ts_new   = latest snapshot — current state
    ts_open  = timestamps[0]                          # first of day
    ts_new   = timestamps[-1]                         # latest
    ts_30min = timestamps[max(0, len(timestamps) - 7)]  # ~30 mins ago (6 back), fallback to oldest

    # ── Minutes to market close (15:30 IST = 10:00 UTC) ──────────────────────
    now_utc = datetime.now(timezone.utc)
    market_close_utc = now_utc.replace(hour=10, minute=0, second=0, microsecond=0)
    mins_to_close = int((market_close_utc - now_utc).total_seconds() / 60)
    is_near_close = 0 < mins_to_close <= 30
    is_very_near_close = 0 < mins_to_close <= 15

    # ── Paginated fetch for 3 key snapshots ───────────────────────────────────
    def fetch_snapshot(ts):
        rows = []
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .eq("timestamp", ts)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        return rows

    new_data   = fetch_snapshot(ts_new)
    open_data  = fetch_snapshot(ts_open)   # for price direction (A)
    min30_data = fetch_snapshot(ts_30min)  # for OI momentum (B)

    # ── CMP map ───────────────────────────────────────────────────────────────
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

    # Build lookup maps
    open_map  = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in open_data}
    min30_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in min30_data}

    # ── Avg volume from 3 snapshots only (fast — no full day fetch) ───────────
    # open + 30min + new = enough to detect unusual volume vs baseline
    avg_vol: dict = {}
    for key in set(list(open_map.keys()) + list(min30_map.keys())):
        vols = []
        if key in open_map and open_map[key]["volume"]:
            vols.append(open_map[key]["volume"])
        if key in min30_map and min30_map[key]["volume"]:
            vols.append(min30_map[key]["volume"])
        if vols:
            avg_vol[key] = sum(vols) / len(vols)

    uoa_signals = []

    for row in new_data:
        sym      = row["symbol"]
        ts       = row["tradingsymbol"]
        key      = f"{sym}_{ts}"
        open_row = open_map.get(key)
        min30_row = min30_map.get(key)

        if not open_row or not min30_row:
            continue

        cmp      = cmp_map.get(sym, 0)
        strike   = row["strike"]
        opt_type = row["option_type"]
        new_vol  = row["volume"] or 0
        new_oi   = row["oi"] or 0
        new_ltp  = row["last_price"] or 0

        open_ltp  = open_row["last_price"] or 0
        open_oi   = open_row["oi"] or 0
        min30_oi  = min30_row["oi"] or 0
        min30_vol = min30_row["volume"] or 0

        # Minimum volume gate
        if new_vol < 100000:
            continue

        # ── A: Price direction — open of day vs now ───────────────────────────
        ltp_chg_from_open = ((new_ltp - open_ltp) / open_ltp * 100) if open_ltp > 0 else 0
        price_rising  = ltp_chg_from_open > 2.0   # up >2% from open
        price_falling = ltp_chg_from_open < -2.0  # down >2% from open

        # ── B: OI momentum — 30 mins ago vs now ──────────────────────────────
        oi_chg_30min = ((new_oi - min30_oi) / min30_oi * 100) if min30_oi > 0 else 0
        oi_rising  = oi_chg_30min > 2.0
        oi_falling = oi_chg_30min < -2.0

        # Volume metrics
        avg = avg_vol.get(key, new_vol)
        vol_ratio    = new_vol / avg if avg > 0 else 1
        vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0
        vol_chg_30m  = ((new_vol - min30_vol) / min30_vol * 100) if min30_vol > 0 else 0

        # OTM calculation
        if cmp > 0:
            dist_pct = ((strike - cmp) / cmp * 100)
            is_otm   = (opt_type == 'CE' and strike > cmp) or (opt_type == 'PE' and strike < cmp)
            otm_pct  = abs(dist_pct) if is_otm else 0
        else:
            otm_pct = 0
            is_otm  = False

        # ── Scoring ───────────────────────────────────────────────────────────
        score = 0
        if vol_ratio > 6:    score += 2
        elif vol_ratio > 4:  score += 1
        if vol_oi_ratio > 4: score += 2
        elif vol_oi_ratio > 2: score += 1
        if otm_pct > 3 and new_vol > 200000: score += 1
        if abs(oi_chg_30min) > 10 and vol_chg_30m > 20: score += 1

        if score < 3:
            continue

        # ── Signal classification: A (price from open) + B (OI 30-min) ───────
        # SEBI compliant: describe WHAT is happening, not what trader should do

        if oi_rising and price_rising:
            if opt_type == 'CE':
                signal_type = "LONG_BUILDUP"
                signal_desc = "CE OI building last 30 mins · price rising from open · call accumulation observed"
                bias = "BULLISH"
            else:
                signal_type = "SHORT_BUILDUP"
                signal_desc = "PE OI building last 30 mins · price rising from open · put accumulation observed"
                bias = "BEARISH"

        elif oi_rising and price_falling:
            if opt_type == 'CE':
                signal_type = "CALL_WRITING"
                signal_desc = "CE OI rising · price falling from open · call writer activity observed"
                bias = "BEARISH"
            else:
                signal_type = "PUT_WRITING"
                signal_desc = "PE OI rising · price falling from open · put writer activity observed"
                bias = "BULLISH"

        elif oi_falling and price_rising:
            if opt_type == 'CE':
                signal_type = "SHORT_COVERING"
                signal_desc = "CE OI reducing · price rising from open · call short positions unwinding"
                bias = "BULLISH"
            else:
                signal_type = "LONG_UNWINDING"
                signal_desc = "PE OI reducing · price rising from open · put long positions exiting"
                bias = "BULLISH"

        elif oi_falling and price_falling:
            if opt_type == 'CE':
                signal_type = "LONG_UNWINDING"
                signal_desc = "CE OI reducing · price falling from open · call long positions exiting"
                bias = "BEARISH"
            else:
                signal_type = "SHORT_COVERING"
                signal_desc = "PE OI reducing · price falling from open · put short positions unwinding"
                bias = "BEARISH"

        elif vol_oi_ratio > 2 and not oi_rising and not oi_falling:
            # High volume but OI flat = positions changing hands, not building
            if price_rising:
                signal_type = "BUYER_DOMINATED"
                signal_desc = "High volume · flat OI · price above open · buying interest observed"
                bias = "BULLISH" if opt_type == "CE" else "BEARISH"
            elif price_falling:
                signal_type = "SELLER_DOMINATED"
                signal_desc = "High volume · flat OI · price below open · selling pressure observed"
                bias = "BEARISH" if opt_type == "CE" else "BULLISH"
            else:
                continue  # flat price + flat OI = no clear activity

        elif otm_pct > 3 and new_vol > 200000:
            signal_type = "FAR_OTM_ACTIVITY"
            signal_desc = f"{otm_pct:.1f}% OTM · heavy volume · possible hedging or speculative interest"
            bias = "BULLISH" if opt_type == "PE" else "BEARISH"

        elif vol_ratio > 4 and (price_rising or price_falling):
            signal_type = "VOLUME_SURGE"
            signal_desc = f"{vol_ratio:.1f}x average volume · significant activity vs baseline"
            bias = "BULLISH" if (
                (opt_type == "CE" and price_rising) or
                (opt_type == "PE" and price_falling)
            ) else "BEARISH"

        else:
            continue  # no clear signal — skip noise

        # ── Time-based advisory tag (SEBI safe) ───────────────────────────────
        if is_very_near_close:
            time_tag = "market_closing"      # < 15 mins
        elif is_near_close:
            time_tag = "positional_only"     # 15-30 mins
        else:
            time_tag = "normal"

        uoa_signals.append({
            "symbol":           sym,
            "tradingsymbol":    ts,
            "strike":           strike,
            "option_type":      opt_type,
            "cmp":              float(cmp),
            "ltp":              float(new_ltp),
            "open_ltp":         float(open_ltp),
            "ltp_chg_from_open": round(ltp_chg_from_open, 2),
            "volume":           new_vol,
            "oi":               new_oi,
            "oi_chg_30min":     round(oi_chg_30min, 2),
            "vol_oi_ratio":     round(vol_oi_ratio, 2),
            "vol_ratio":        round(vol_ratio, 2),
            "vol_chg_30min":    round(vol_chg_30m, 2),
            "otm_pct":          round(otm_pct, 2),
            "is_otm":           is_otm,
            "signal_type":      signal_type,
            "signal_desc":      signal_desc,
            "bias":             bias,
            "score":            min(score, 5),
            "time_tag":         time_tag,
            "is_index":         sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        })

    uoa_signals.sort(key=lambda x: (x["score"], abs(x["ltp_chg_from_open"])), reverse=True)

    return {
        "timestamp":       ts_new,
        "open_timestamp":  ts_open,
        "date":            today,
        "total":           len(uoa_signals),
        "signals":         uoa_signals[:50],
        "snapshot_count":  len(timestamps),
        "mins_to_close":   mins_to_close if mins_to_close > 0 else 0,
    }
