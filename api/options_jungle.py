from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type

# Persistence tracking — survives across calls within same process
_oi_persistence: dict = {}   # tradingsymbol -> {"first_seen": ts, "count": int, "last_date": str}
_vol_persistence: dict = {}

def get_options_jungle(oi_threshold: float = 10.0, vol_threshold: float = 50.0, date: str = None):
    global _oi_persistence, _vol_persistence
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Get distinct timestamps via paginated fetch ───────────────────────────
    all_ts_rows = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .lt("timestamp",  f"{today}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_ts_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    timestamps = sorted(set(r["timestamp"] for r in all_ts_rows))

    if len(timestamps) < 2:
        fallback_rows = []
        for offset in range(0, 50000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("timestamp")\
                .eq("symbol", "NIFTY")\
                .order("timestamp", desc=True)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            fallback_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        timestamps = sorted(set(r["timestamp"] for r in fallback_rows))
        if len(timestamps) < 2:
            return {"error": "Need at least 2 snapshots", "oi_spikes": [], "vol_spikes": []}

    ts_new = timestamps[-1]
    ts_new_dt = datetime.fromisoformat(ts_new.replace('+00:00', '')).replace(tzinfo=timezone.utc)
    target_old = ts_new_dt - timedelta(minutes=5)
    ts_old = min(timestamps[:-1], key=lambda t: abs(
        datetime.fromisoformat(t.replace('+00:00', '')).replace(tzinfo=timezone.utc) - target_old
    ))

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

    new_data_raw = fetch_snapshot(ts_new)
    old_data_raw = fetch_snapshot(ts_old)

    # ── FIX: Build nearest active expiry map per symbol ───────────────────────
    # For each symbol, find the nearest expiry >= today
    # This prevents June expiry LTPs from appearing when May is still active
    today_str = date_type.today().isoformat()

    nearest_expiry_map: dict = {}
    for r in new_data_raw:
        sym = r["symbol"]
        exp = r.get("expiry")
        if not exp or exp < today_str:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    # Filter snapshots to nearest active expiry only
    def filter_to_nearest_expiry(rows):
        filtered = []
        for r in rows:
            sym = r["symbol"]
            exp = r.get("expiry")
            nearest = nearest_expiry_map.get(sym)
            if nearest and exp == nearest:
                filtered.append(r)
            elif sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
                # For indices allow all expiries (they have multiple active)
                filtered.append(r)
        return filtered

    new_data = filter_to_nearest_expiry(new_data_raw)
    old_data = filter_to_nearest_expiry(old_data_raw)

    # ── CMP map ───────────────────────────────────────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
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
            "expiry":        row.get("expiry"),
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
            deep_itm = not is_otm and cmp > 0 and (
                (opt_type == 'CE' and (cmp - strike) / cmp > 0.03) or
                (opt_type == 'PE' and (strike - cmp) / cmp > 0.03)
            )
            if ltp_chg > 0.5 and oi_pct > 0:
                interp = "LONG_BUILDUP" if opt_type == "CE" else "SHORT_BUILDUP"
            elif ltp_chg < -0.5 and oi_pct > 0 and deep_itm:
                interp = "LONG_BUILDUP" if opt_type == "PE" else "SHORT_BUILDUP"
            elif ltp_chg < -0.5 and oi_pct > 0:
                interp = "CALL_WRITING" if opt_type == "CE" else "PUT_WRITING"
            elif oi_pct < 0 and ltp_chg > 0:
                interp = "SHORT_COVERING" if opt_type == "CE" else "LONG_UNWINDING"
            elif oi_pct < 0 and ltp_chg < 0:
                interp = "LONG_UNWINDING" if opt_type == "CE" else "SHORT_COVERING"
            else:
                interp = direction

            ts_key = row["tradingsymbol"]
            if _oi_persistence.get(ts_key, {}).get("last_date") != today:
                _oi_persistence[ts_key] = {"first_seen": ts_new, "snapshots": set(), "last_date": today}
            _oi_persistence[ts_key]["snapshots"].add(ts_new)
            persist = _oi_persistence[ts_key]

            oi_spikes.append({
                **base,
                "old_oi":          old_oi,
                "new_oi":          new_oi,
                "oi_change":       oi_change,
                "oi_pct":          oi_pct,
                "vol_change":      vol_change,
                "direction":       direction,
                "interpretation":  interp,
                "first_seen":      to_ist(persist["first_seen"]),
                "snapshot_count":  len(persist["snapshots"]),
            })

        # ── Volume Spike ──────────────────────────────────────────────────────
        if old_vol >= 10000 and vol_pct >= vol_threshold:
            if oi_pct > 5:
                vol_signal = "FRESH_BUILD"
            elif oi_pct < -5:
                vol_signal = "UNWINDING"
            else:
                vol_signal = "CHURN"

            ts_key = row["tradingsymbol"]
            if _vol_persistence.get(ts_key, {}).get("last_date") != today:
                _vol_persistence[ts_key] = {"first_seen": ts_new, "snapshots": set(), "last_date": today}
            _vol_persistence[ts_key]["snapshots"].add(ts_new)
            vpersist = _vol_persistence[ts_key]

            vol_spikes.append({
                **base,
                "old_volume":      old_vol,
                "new_volume":      new_vol,
                "vol_pct":         vol_pct,
                "oi_pct":          oi_pct,
                "vol_signal":      vol_signal,
                "first_seen":      to_ist(vpersist["first_seen"]),
                "snapshot_count":  len(vpersist["snapshots"]),
            })

    oi_spikes.sort(key=lambda x: abs(x["oi_pct"]), reverse=True)
    vol_spikes.sort(key=lambda x: x["vol_pct"], reverse=True)

    return {
        "date":          today,
        "ts_new":        ts_new,
        "ts_old":        ts_old,
        "open_time":     to_ist(timestamps[0]),
        "close_time":    to_ist(timestamps[-1]),
        "snapshots":     len(timestamps),
        "oi_threshold":  oi_threshold,
        "vol_threshold": vol_threshold,
        "oi_spikes":     oi_spikes[:100],
        "vol_spikes":    vol_spikes[:100],
        "oi_total":      len(oi_spikes),
        "vol_total":     len(vol_spikes),
    }
