from utils.db import get_supabase
from datetime import datetime, timezone

def get_eod_analysis(symbol: str = "NIFTY", date: str = None, expiry: str = None):
    supabase = get_supabase()

    # Get available dates
    all_ts = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .order("timestamp", desc=False)\
        .execute()

    if not all_ts.data:
        return {"symbol": symbol, "dates": [], "rows": []}

    dates = sorted(set(r["timestamp"][:10] for r in all_ts.data))
    active_date = date or dates[-1]

    # Get all timestamps for this date
    day_ts = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{active_date}T00:00:00+00:00")\
        .lt("timestamp",  f"{active_date}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    if not day_ts.data:
        return {"symbol": symbol, "dates": dates, "date": active_date, "rows": []}

    timestamps = sorted(set(r["timestamp"] for r in day_ts.data))
    first_ts = timestamps[0]
    last_ts  = timestamps[-1]

    def fetch_snap(ts):
        q = supabase.from_("oi_snapshots")\
            .select("strike, option_type, oi, expiry")\
            .eq("symbol", symbol)\
            .eq("timestamp", ts)
        if expiry:
            q = q.eq("expiry", expiry)
        rows = q.execute().data
        result = {}
        for r in rows:
            result[(r["strike"], r["option_type"])] = r["oi"] or 0
        return result

    snap_open  = fetch_snap(first_ts)
    snap_close = fetch_snap(last_ts)

    # Get expiries
    exp_q = supabase.from_("oi_snapshots")\
        .select("expiry")\
        .eq("symbol", symbol)\
        .eq("timestamp", last_ts)\
        .execute()
    expiries = sorted(set(r["expiry"] for r in exp_q.data if r["expiry"]))
    active_expiry = expiry or (expiries[0] if expiries else None)

    # Re-fetch with expiry filter if needed
    if active_expiry and not expiry:
        snap_open  = fetch_snap(first_ts)
        snap_close = fetch_snap(last_ts)

    all_strikes = sorted(set(k[0] for k in list(snap_open.keys()) + list(snap_close.keys())))

    # Build intraday OI journey for chart (all snapshots)
    journey_data = []
    for ts in timestamps:
        q = supabase.from_("oi_snapshots")\
            .select("option_type, oi")\
            .eq("symbol", symbol)\
            .eq("timestamp", ts)
        if active_expiry:
            q = q.eq("expiry", active_expiry)
        snap = q.execute().data
        ce = sum(r["oi"] or 0 for r in snap if r["option_type"] == "CE")
        pe = sum(r["oi"] or 0 for r in snap if r["option_type"] == "PE")
        pcr = round(pe / ce, 3) if ce > 0 else 0
        # Convert UTC to IST
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                clean = f"{base}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist_min = dt.hour * 60 + dt.minute + 330
            time_label = f"{(ist_min//60)%24:02d}:{ist_min%60:02d}"
        except:
            time_label = ts[11:16]
        journey_data.append({"time": time_label, "ce_oi": ce, "pe_oi": pe, "pcr": pcr})

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
            "ce_open":  ce_open,
            "ce_close": ce_close,
            "ce_chg":   ce_chg,
            "pe_open":  pe_open,
            "pe_close": pe_close,
            "pe_chg":   pe_chg,
            "net_chg":  pe_chg - ce_chg,
        })

    # IST time labels
    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                clean = f"{base}.{frac[:6].ljust(6,'0')}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist_min = dt.hour * 60 + dt.minute + 330
            return f"{(ist_min//60)%24:02d}:{ist_min%60:02d}"
        except:
            return ts[11:16]

    return {
        "symbol":       symbol,
        "date":         active_date,
        "dates":        dates,
        "expiry":       active_expiry,
        "expiries":     expiries,
        "open_time":    to_ist(first_ts),
        "close_time":   to_ist(last_ts),
        "snapshots":    len(timestamps),
        "journey":      journey_data,
        "rows":         rows,
    }
