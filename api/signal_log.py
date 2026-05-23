from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type


SIGNAL_META = {
    "LONG_BUILDUP":     {"label": "Long Buildup",     "bias": "BULLISH"},
    "SHORT_BUILDUP":    {"label": "Short Buildup",    "bias": "BEARISH"},
    "CALL_WRITING":     {"label": "Call Writing",     "bias": "BEARISH"},
    "PUT_WRITING":      {"label": "Put Writing",      "bias": "BULLISH"},
    "SHORT_COVERING":   {"label": "Short Covering",   "bias": "BULLISH"},
    "LONG_UNWINDING":   {"label": "Long Unwinding",   "bias": "BEARISH"},
    "BUYER_DOMINATED":  {"label": "Buyer Dominated",  "bias": "MIXED"},
    "SELLER_DOMINATED": {"label": "Seller Dominated", "bias": "MIXED"},
    "FAR_OTM_ACTIVITY": {"label": "Far OTM Activity", "bias": "MIXED"},
    "VOLUME_SURGE":     {"label": "Volume Surge",     "bias": "MIXED"},
}


def to_ist(ts: str) -> str:
    try:
        clean = ts.split('+')[0].split('Z')[0]
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        ist = dt.hour * 60 + dt.minute + 330
        return f"{(ist//60)%24:02d}:{ist%60:02d}"
    except:
        return ts[11:16]


def get_signal_log(date: str = None, symbol: str = None):
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_str = date_type.today().isoformat()

    # ── Step 1: Get all timestamps for today ─────────────────────────────────
    all_ts_rows = []
    for offset in range(0, 50000, 1000):
        q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .lt("timestamp",  f"{today}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999)\
            .execute()
        if not q.data:
            break
        all_ts_rows.extend(q.data)
        if len(q.data) < 1000:
            break

    timestamps = sorted(set(r["timestamp"] for r in all_ts_rows))
    if len(timestamps) < 2:
        return {"signals": [], "total": 0, "date": today, "snapshots": 0}

    ts_open  = timestamps[0]
    ts_latest = timestamps[-1]

    # ── Step 2: Fetch ALL OI data for today in one query ─────────────────────
    all_rows = []
    q = supabase.from_("oi_snapshots")\
        .select("timestamp, symbol, tradingsymbol, strike, option_type, oi, volume, last_price, expiry, is_index")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)

    if symbol:
        q = q.eq("symbol", symbol)

    for offset in range(0, 500000, 1000):
        batch = q.range(offset, offset + 999).execute()
        if not batch.data:
            break
        all_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    if not all_rows:
        return {"signals": [], "total": 0, "date": today, "snapshots": len(timestamps)}

    # ── Step 3: Filter to nearest expiry per symbol ───────────────────────────
    nearest_expiry_map: dict = {}
    for r in all_rows:
        sym = r["symbol"]
        exp = r.get("expiry")
        if not exp or exp < today_str:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    all_rows = [
        r for r in all_rows
        if r.get("expiry") == nearest_expiry_map.get(r["symbol"])
    ]

    # ── Step 4: Get CMP map ───────────────────────────────────────────────────
    cmp_rows = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("symbol, cmp, timestamp")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        cmp_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    cmp_map: dict = {}
    for c in cmp_rows:
        if c["symbol"] not in cmp_map:
            cmp_map[c["symbol"]] = float(c["cmp"])

    # ── Step 5: Group rows by timestamp ──────────────────────────────────────
    from collections import defaultdict
    ts_data: dict = defaultdict(list)
    for r in all_rows:
        ts_data[r["timestamp"]].append(r)

    # ── Step 6: Detect signals per timestamp using UOA logic ─────────────────
    # Key = (symbol, tradingsymbol, signal_type) → track appearances
    signal_appearances: dict = {}  # key → list of timestamps where signal appeared

    prev_ts_data: dict = {}  # previous timestamp data for comparison

    for ts in timestamps:
        rows = ts_data.get(ts, [])
        if not rows:
            continue

        # Build OI/volume maps for this timestamp
        current: dict = {}
        for r in rows:
            key = f"{r['symbol']}_{r['tradingsymbol']}"
            current[key] = r

        # Need at least open snapshot for comparison
        open_rows = ts_data.get(ts_open, [])
        open_map  = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in open_rows}

        # 30-min ago snapshot
        ts_idx = timestamps.index(ts)
        ts_30min = timestamps[max(0, ts_idx - 6)]
        min30_rows = ts_data.get(ts_30min, [])
        min30_map  = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in min30_rows}

        for key, row in current.items():
            sym      = row["symbol"]
            ts_sym   = row["tradingsymbol"]
            opt_type = row["option_type"]
            new_oi   = row["oi"] or 0
            new_vol  = row["volume"] or 0
            new_ltp  = row["last_price"] or 0
            cmp      = cmp_map.get(sym, 0)

            open_row  = open_map.get(key)
            min30_row = min30_map.get(key)

            if not open_row or not min30_row:
                continue

            if new_vol < 100000:
                continue

            open_ltp  = open_row.get("last_price") or 0
            min30_oi  = min30_row.get("oi") or 0
            min30_vol = min30_row.get("volume") or 0

            ltp_chg = ((new_ltp - open_ltp) / open_ltp * 100) if open_ltp > 0 else 0
            oi_chg  = ((new_oi - min30_oi)  / min30_oi  * 100) if min30_oi  > 0 else 0
            vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0

            price_rising  = ltp_chg > 2.0
            price_falling = ltp_chg < -2.0
            oi_rising     = oi_chg > 2.0
            oi_falling    = oi_chg < -2.0

            # Score
            score = 0
            avg_vol = (open_row.get("volume") or new_vol)
            vol_ratio = new_vol / avg_vol if avg_vol > 0 else 1
            if vol_ratio > 6:      score += 2
            elif vol_ratio > 4:    score += 1
            if vol_oi_ratio > 4:   score += 2
            elif vol_oi_ratio > 2: score += 1
            if abs(oi_chg) > 10 and ((new_vol - min30_vol) / min30_vol * 100 if min30_vol > 0 else 0) > 20:
                score += 1

            if score < 3:
                continue

            # Determine signal type
            signal_type = None
            bias = None

            if oi_rising and price_rising:
                signal_type = "LONG_BUILDUP" if opt_type == "CE" else "SHORT_BUILDUP"
                bias = "BULLISH" if opt_type == "CE" else "BEARISH"
            elif oi_rising and price_falling:
                signal_type = "CALL_WRITING" if opt_type == "CE" else "PUT_WRITING"
                bias = "BEARISH" if opt_type == "CE" else "BULLISH"
            elif oi_falling and price_rising:
                signal_type = "SHORT_COVERING" if opt_type == "CE" else "LONG_UNWINDING"
                bias = "BULLISH"
            elif oi_falling and price_falling:
                signal_type = "LONG_UNWINDING" if opt_type == "CE" else "SHORT_COVERING"
                bias = "BEARISH"
            elif vol_oi_ratio > 2:
                signal_type = "VOLUME_SURGE"
                bias = "BULLISH" if ((opt_type == "CE" and price_rising) or (opt_type == "PE" and price_falling)) else "BEARISH"

            if not signal_type:
                continue

            sig_key = f"{sym}_{ts_sym}_{signal_type}"
            if sig_key not in signal_appearances:
                signal_appearances[sig_key] = {
                    "symbol":       sym,
                    "tradingsymbol": ts_sym,
                    "strike":       float(row["strike"]),
                    "option_type":  opt_type,
                    "signal_type":  signal_type,
                    "bias":         bias,
                    "score":        min(score, 5),
                    "cmp":          float(cmp) if cmp else 0,
                    "is_index":     row.get("is_index", False),
                    "first_seen_ts": ts,
                    "first_seen":   to_ist(ts),
                    "last_seen_ts":  ts,
                    "last_seen":    to_ist(ts),
                    "appearances":  1,
                    "ltp_at_first": float(new_ltp),
                    "ltp_latest":   float(new_ltp),
                    "oi_chg":       round(oi_chg, 2),
                    "ltp_chg":      round(ltp_chg, 2),
                    "vol_oi_ratio": round(vol_oi_ratio, 2),
                }
            else:
                signal_appearances[sig_key]["last_seen_ts"] = ts
                signal_appearances[sig_key]["last_seen"]    = to_ist(ts)
                signal_appearances[sig_key]["appearances"]  += 1
                signal_appearances[sig_key]["ltp_latest"]   = float(new_ltp)
                signal_appearances[sig_key]["oi_chg"]       = round(oi_chg, 2)
                signal_appearances[sig_key]["ltp_chg"]      = round(ltp_chg, 2)
                signal_appearances[sig_key]["vol_oi_ratio"] = round(vol_oi_ratio, 2)
                # Update score to max seen
                signal_appearances[sig_key]["score"] = max(signal_appearances[sig_key]["score"], min(score, 5))

    # ── Step 7: Mark active vs gone ───────────────────────────────────────────
    signals = []
    for sig in signal_appearances.values():
        sig["is_active"] = sig["last_seen_ts"] == ts_latest
        sig["persistence"] = sig["appearances"]  # how many snapshots it appeared in
        sig["persistence_pct"] = round(sig["appearances"] / len(timestamps) * 100)
        sig["ltp_move"] = round(sig["ltp_latest"] - sig["ltp_at_first"], 2)
        signals.append(sig)

    # Sort by first_seen desc, then score desc
    signals.sort(key=lambda x: (x["first_seen_ts"], x["score"]), reverse=True)

    return {
        "date":      today,
        "signals":   signals,
        "total":     len(signals),
        "snapshots": len(timestamps),
        "open_time": to_ist(ts_open),
        "latest_time": to_ist(ts_latest),
    }
