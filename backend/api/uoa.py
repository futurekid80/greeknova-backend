from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type

def get_uoa(date: str = None):
    supabase = get_supabase()

    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get latest two distinct timestamps for the requested date via NIFTY filter
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(200)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data), reverse=True)

    # Fallback: if no data for requested date, use last available day
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

    # Get current and previous snapshots with limit to avoid silent truncation
    new_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_new).limit(5000).execute().data
    old_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_old).limit(5000).execute().data

    # Get CMPs
    cmp_res = supabase.from_("cmp_prices").select("*").order("timestamp", desc=True).limit(100).execute().data
    cmp_map = {}
    seen = set()
    for c in cmp_res:
        if c["symbol"] not in seen:
            cmp_map[c["symbol"]] = c["cmp"]
            seen.add(c["symbol"])

    old_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in old_data}

    # Get today's volume history for baseline avg
    today_data = supabase.from_("oi_snapshots")\
        .select("symbol, tradingsymbol, volume")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .limit(5000)\
        .execute().data

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
        ltp = row["last_price"] or 0

        if new_vol < 50000:
            continue

        avg = avg_vol.get(key, new_vol)
        vol_ratio = new_vol / avg if avg > 0 else 1
        vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0
        oi_change_pct = ((new_oi - old_oi) / old_oi * 100) if old_oi > 0 else 0
        vol_change_pct = ((new_vol - old_vol) / old_vol * 100) if old_vol > 0 else 0

        if cmp > 0:
            dist_pct = ((strike - cmp) / cmp * 100)
            is_otm = (opt_type == 'CE' and strike > cmp) or (opt_type == 'PE' and strike < cmp)
            otm_pct = abs(dist_pct) if is_otm else 0
        else:
            otm_pct = 0
            is_otm = False

        score = 0
        if vol_ratio > 5: score += 2
        elif vol_ratio > 3: score += 1
        if vol_oi_ratio > 3: score += 2
        elif vol_oi_ratio > 1.5: score += 1
        if otm_pct > 3 and new_vol > 100000: score += 1
        if oi_change_pct > 15 and vol_change_pct > 20: score += 1

        if score < 2:
            continue

        if vol_oi_ratio > 2 and oi_change_pct < 5:
            signal_type = "BUYER_DOMINATED"
            signal_desc = "High vol vs OI — buyers absorbing sellers"
        elif oi_change_pct > 15 and vol_change_pct > 20:
            signal_type = "FRESH_CONVICTION"
            signal_desc = "Volume + OI both building — strong directional bet"
        elif otm_pct > 3 and new_vol > 200000:
            signal_type = "FAR_OTM_ACTIVITY"
            signal_desc = f"{otm_pct:.1f}% OTM with heavy volume — hedging or speculative bet"
        elif vol_ratio > 4:
            signal_type = "VOLUME_SURGE"
            signal_desc = f"{vol_ratio:.1f}x normal volume — unusual interest"
        else:
            signal_type = "UNUSUAL_ACTIVITY"
            signal_desc = "Multiple unusual signals detected"

        uoa_signals.append({
            "symbol": sym,
            "tradingsymbol": ts,
            "strike": strike,
            "option_type": opt_type,
            "cmp": float(cmp),
            "ltp": float(ltp),
            "volume": new_vol,
            "oi": new_oi,
            "vol_oi_ratio": round(vol_oi_ratio, 2),
            "vol_ratio": round(vol_ratio, 2),
            "oi_change_pct": round(oi_change_pct, 2),
            "vol_change_pct": round(vol_change_pct, 2),
            "otm_pct": round(otm_pct, 2),
            "is_otm": is_otm,
            "signal_type": signal_type,
            "signal_desc": signal_desc,
            "score": min(score, 5),
            "is_index": sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        })

    uoa_signals.sort(key=lambda x: (x["score"], x["volume"]), reverse=True)

    return {
        "timestamp": ts_new,
        "date": today,
        "total": len(uoa_signals),
        "signals": uoa_signals[:50]
    }
