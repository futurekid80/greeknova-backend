from utils.db import get_supabase
from datetime import datetime, timezone

def get_options_jungle(oi_threshold: float = 10.0, vol_threshold: float = 50.0, date: str = None):
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Get timestamps via NIFTY ──────────────────────────────────────────────
    ts_result = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    timestamps = sorted(set(r["timestamp"] for r in ts_result.data))

    if len(timestamps) < 2:
        fallback = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .order("timestamp", desc=True)\
            .limit(100)\
            .execute()
        timestamps = sorted(set(r["timestamp"] for r in fallback.data), reverse=True)
        if len(timestamps) < 2:
            return {"error": "Need at least 2 snapshots", "oi_spikes": [], "vol_spikes": []}
        ts_new = timestamps[0]
        ts_old = timestamps[1]
    else:
        ts_old = timestamps[-2]
        ts_new = timestamps[-1]

    # ── Paginated snapshot fetch ───────────────────────────────────────────────
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

    new_data = fetch_snapshot(ts_new)
    old_data = fetch_snapshot(ts_old)

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
    seen = set()
    for c in cmp_raw:
        if c["symbol"] not in seen:
            cmp_map[c["symbol"]] = c["cmp"]
            seen.add(c["symbol"])

    old_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in old_data}

    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                clean = f"{base}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist = dt.hour * 60 + dt.minute + 330
            return f"{(ist//60)%24:02d}:{ist%60:02d}"
        except:
            return ts[11:16]

    oi_spikes = []
    vol_spikes = []

    for row in new_data:
        key = f"{row['symbol']}_{row['tradingsymbol']}"
        old_row = old_map.get(key)
        if not old_row:
            continue

        sym      = row["symbol"]
        old_oi   = old_row["oi"] or 0
        new_oi   = row["oi"] or 0
        old_vol  = old_row["volume"] or 0
        new_vol  = row["volume"] or 0
        new_ltp  = row["last_price"] or 0
        old_ltp  = old_row["last_price"] or 0
        cmp      = cmp_map.get(sym, 0)

        oi_change  = new_oi - old_oi
        oi_pct     = round((oi_change / old_oi * 100), 2) if old_oi > 1000 else 0
        vol_change = new_vol - old_vol
        vol_pct    = round(((new_vol - old_vol) / old_vol * 100), 2) if old_vol > 10000 else 0
        ltp_chg    = round(((new_ltp - old_ltp) / old_ltp * 100), 2) if old_ltp > 0 else 0

        # OTM calculation
        strike   = row["strike"]
        opt_type = row["option_type"]
        if cmp > 0:
            is_otm  = (opt_type == 'CE' and strike > cmp) or (opt_type == 'PE' and strike < cmp)
            otm_pct = round(abs((strike - cmp) / cmp * 100), 2) if is_otm else 0
        else:
            is_otm  = False
            otm_pct = 0

        base = {
            "symbol":        sym,
            "tradingsymbol": row["tradingsymbol"],
            "strike":        strike,
            "option_type":   opt_type,
            "cmp":           float(cmp),
            "last_price":    float(new_ltp),
            "ltp_chg_pct":   ltp_chg,
            "is_index":      sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "is_otm":        is_otm,
            "otm_pct":       otm_pct,
            "volume":        new_vol,
            "oi":            new_oi,
        }

        # ── OI Spike ──────────────────────────────────────────────────────────
        if old_oi >= 1000 and abs(oi_pct) >= oi_threshold:
            direction = "BUILD" if oi_change > 0 else "UNWIND"
            # Interpret using LTP direction
            if ltp_chg > 0.5 and oi_pct > 0:
                interp = "LONG_BUILDUP" if opt_type == "CE" else "SHORT_BUILDUP"
            elif ltp_chg < -0.5 and oi_pct > 0:
                interp = "CALL_WRITING" if opt_type == "CE" else "PUT_WRITING"
            elif oi_pct < 0 and ltp_chg > 0:
                interp = "SHORT_COVERING" if opt_type == "CE" else "LONG_UNWINDING"
            elif oi_pct < 0 and ltp_chg < 0:
                interp = "LONG_UNWINDING" if opt_type == "CE" else "SHORT_COVERING"
            else:
                interp = direction

            oi_spikes.append({
                **base,
                "old_oi":    old_oi,
                "new_oi":    new_oi,
                "oi_change": oi_change,
                "oi_pct":    oi_pct,
                "vol_change": vol_change,
                "direction": direction,
                "interpretation": interp,
            })

        # ── Volume Spike ──────────────────────────────────────────────────────
        if old_vol >= 10000 and vol_pct >= vol_threshold:
            if oi_pct > 5:
                vol_signal = "FRESH_BUILD"
            elif oi_pct < -5:
                vol_signal = "UNWINDING"
            else:
                vol_signal = "CHURN"

            vol_spikes.append({
                **base,
                "old_volume":  old_vol,
                "new_volume":  new_vol,
                "vol_pct":     vol_pct,
                "oi_pct":      oi_pct,
                "vol_signal":  vol_signal,
            })

    oi_spikes.sort(key=lambda x: abs(x["oi_pct"]), reverse=True)
    vol_spikes.sort(key=lambda x: x["vol_pct"], reverse=True)

    return {
        "date":          today,
        "ts_new":        ts_new,
        "ts_old":        ts_old,
        "open_time":     to_ist(ts_old),
        "close_time":    to_ist(ts_new),
        "snapshots":     len(timestamps),
        "oi_threshold":  oi_threshold,
        "vol_threshold": vol_threshold,
        "oi_spikes":     oi_spikes[:100],
        "vol_spikes":    vol_spikes[:100],
        "oi_total":      len(oi_spikes),
        "vol_total":     len(vol_spikes),
    }
