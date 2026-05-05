from backend.utils.db import get_supabase
from datetime import datetime, timezone

def get_oi_history(symbol: str):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get all today's snapshots for this symbol
    result = supabase.from_("oi_snapshots")\
        .select("timestamp, strike, option_type, oi")\
        .eq("symbol", symbol)\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .order("timestamp", desc=False)\
        .execute()

    if not result.data:
        return {"symbol": symbol, "timestamps": [], "strikes": []}

    # Get unique timestamps and strikes
    timestamps = sorted(set(r["timestamp"] for r in result.data))
    strikes = sorted(set(r["strike"] for r in result.data))

    # Convert timestamps to IST labels
    def to_ist(ts):
        try:
            clean = ts.split('+')[0]
            if '.' in clean:
                base, frac = clean.split('.')
                frac = frac[:6].ljust(6, '0')
                clean = f"{base}.{frac}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ist_min = dt.hour * 60 + dt.minute + 330
            h, m = (ist_min // 60) % 24, ist_min % 60
            return f"{h:02d}:{m:02d}"
        except:
            return ts[11:16]

    ts_labels = [to_ist(ts) for ts in timestamps]

    # Build strike-level OI history
    strike_history = []
    for strike in strikes:
        ce_series = []
        pe_series = []
        for ts in timestamps:
            ce_row = next((r for r in result.data if r["timestamp"] == ts and r["strike"] == strike and r["option_type"] == "CE"), None)
            pe_row = next((r for r in result.data if r["timestamp"] == ts and r["strike"] == strike and r["option_type"] == "PE"), None)
            ce_series.append(ce_row["oi"] if ce_row else None)
            pe_series.append(pe_row["oi"] if pe_row else None)

        # Only include strikes with meaningful OI
        max_oi = max((v for v in ce_series + pe_series if v), default=0)
        if max_oi > 10000:
            strike_history.append({
                "strike": strike,
                "ce_series": ce_series,
                "pe_series": pe_series,
            })

    return {
        "symbol": symbol,
        "timestamps": ts_labels,
        "total_snapshots": len(timestamps),
        "strikes": strike_history[:15],  # top 15 strikes
    }
