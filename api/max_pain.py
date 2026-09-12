from utils.db import get_supabase
from datetime import datetime, timezone, date
import math

def get_max_pain_all():
    supabase = get_supabase()
    
    # Get latest timestamp
    latest = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()
    
    if not latest.data:
        return {"symbols": []}
    
    ts = latest.data[0]["timestamp"]
    data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts).execute().data
    
    symbols = list(set(r["symbol"] for r in data))
    results = []

    # PERF FIX (Sep 12 2026): this used to fire one cmp_prices query PER
    # symbol inside the loop below -- ~186 sequential network round trips,
    # which alone accounted for ~28s of this endpoint's ~29s total (this is
    # what made Pre-Market Report, which calls this function, so slow).
    # Fetch the latest CMP for every symbol in one bulk query instead
    # (order by timestamp desc, keep the first/latest row seen per symbol).
    # A handful of symbols can be too stale to fall inside that window --
    # top the map up with one query per symbol that's still missing, same
    # as the old per-symbol fallback, just only for the rare gaps instead
    # of every symbol every time.
    cmp_rows = supabase.from_("cmp_prices")\
        .select("symbol, cmp, timestamp")\
        .order("timestamp", desc=True)\
        .limit(3000)\
        .execute().data or []
    cmp_map: dict = {}
    for r in cmp_rows:
        if r["symbol"] not in cmp_map:
            cmp_map[r["symbol"]] = r["cmp"]

    missing = [s for s in symbols if s not in cmp_map]
    if missing:
        for symbol in missing:
            cmp_data = supabase.from_("cmp_prices")\
                .select("cmp")\
                .eq("symbol", symbol)\
                .order("timestamp", desc=True)\
                .limit(1)\
                .execute()
            if cmp_data.data:
                cmp_map[symbol] = cmp_data.data[0]["cmp"]

    for symbol in symbols:
        rows = [r for r in data if r["symbol"] == symbol]
        ce_rows = [r for r in rows if r["option_type"] == "CE"]
        pe_rows = [r for r in rows if r["option_type"] == "PE"]
        
        strikes = sorted(set(r["strike"] for r in rows))
        if not strikes:
            continue
        
        # Max pain calculation
        min_loss = float('inf')
        max_pain = strikes[0]
        for s in strikes:
            loss = 0
            for r in ce_rows:
                if s > r["strike"]:
                    loss += (s - r["strike"]) * r["oi"]
            for r in pe_rows:
                if s < r["strike"]:
                    loss += (r["strike"] - s) * r["oi"]
            if loss < min_loss:
                min_loss = loss
                max_pain = s
        
        # Get expiry
        expiry_str = rows[0]["expiry"] if rows else None
        days_to_expiry = None
        if expiry_str:
            try:
                from utils.market_calendar import today_ist
                exp_date = date.fromisoformat(expiry_str[:10])
                days_to_expiry = (exp_date - today_ist()).days
            except:
                pass
        
        # Get CMP (from the single upfront fetch above, not a per-symbol query)
        cmp = cmp_map.get(symbol, 0)
        
        dist_from_mp = round(((cmp - max_pain) / max_pain * 100), 2) if max_pain > 0 and cmp > 0 else 0
        
        total_ce = sum(r["oi"] for r in ce_rows)
        total_pe = sum(r["oi"] for r in pe_rows)
        pcr = round(total_pe / total_ce, 2) if total_ce > 0 else 0
        
        results.append({
            "symbol": symbol,
            "cmp": float(cmp),
            "max_pain": float(max_pain),
            "dist_from_mp": dist_from_mp,
            "days_to_expiry": days_to_expiry,
            "expiry": expiry_str[:10] if expiry_str else None,
            "pcr": pcr,
            "is_index": symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "direction": "ABOVE" if cmp > max_pain else "BELOW",
        })
    
    results.sort(key=lambda x: abs(x["dist_from_mp"]), reverse=True)
    return {"timestamp": ts, "symbols": results}
