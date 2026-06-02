def get_oi_walls(symbol: str, supabase, cmp: float = 0) -> dict:
    """
    Get CE wall (highest CE OI strike) and PE wall (highest PE OI strike)
    for a given symbol from today's latest snapshot.
    Returns ce_wall, pe_wall, ce_wall_oi_L, pe_wall_oi_L, trade_range, trade_range_pct
    """
    from datetime import datetime, timezone, timedelta
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date().isoformat()

    try:
        rows = supabase.from_("oi_snapshots")\
            .select("strike, option_type, oi")\
            .eq("symbol", symbol)\
            .in_("option_type", ["CE", "PE"])\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(2000)\
            .execute()

        if not rows.data:
            return {}

        # Get latest timestamp only
        latest_ts = rows.data[0].get("timestamp") if rows.data else None
        latest_rows = [r for r in rows.data if r.get("timestamp") == latest_ts]

        # Aggregate OI per strike per option_type
        ce_oi: dict = {}
        pe_oi: dict = {}
        for r in latest_rows:
            strike = float(r["strike"])
            oi = int(r["oi"] or 0)
            if r["option_type"] == "CE":
                ce_oi[strike] = ce_oi.get(strike, 0) + oi
            elif r["option_type"] == "PE":
                pe_oi[strike] = pe_oi.get(strike, 0) + oi

        if not ce_oi or not pe_oi:
            return {}

       # CE wall = nearest significant CE OI above CMP (intraday resistance)
        # PE wall = nearest significant PE OI below CMP (intraday support)
        ce_above = {s: v for s, v in ce_oi.items() if s > cmp} if cmp > 0 else ce_oi
        pe_below = {s: v for s, v in pe_oi.items() if s < cmp} if cmp > 0 else pe_oi

        if not ce_above: ce_above = ce_oi
        if not pe_below: pe_below = pe_oi

        # Significant threshold = 10% of max OI in that direction
        max_ce = max(ce_above.values(), default=1)
        max_pe = max(pe_below.values(), default=1)
        ce_significant = {s: v for s, v in ce_above.items() if v >= max_ce * 0.10}
        pe_significant = {s: v for s, v in pe_below.items() if v >= max_pe * 0.10}

        if not ce_significant: ce_significant = ce_above
        if not pe_significant: pe_significant = pe_below

        # Nearest significant = smallest distance from CMP
        ce_wall = min(ce_significant.keys())  # lowest strike above CMP
        pe_wall = max(pe_significant.keys())  # highest strike below CMP

        # CE wall = highest CE OI ABOVE CMP (resistance)
        # PE wall = highest PE OI BELOW CMP (support)
        ce_above = {s: v for s, v in ce_oi.items() if s > cmp} if cmp > 0 else ce_oi
        pe_below = {s: v for s, v in pe_oi.items() if s < cmp} if cmp > 0 else pe_oi

        # Fallback to all strikes if no strikes above/below CMP
        if not ce_above:
            ce_above = ce_oi
        if not pe_below:
            pe_below = pe_oi

        ce_wall = max(ce_above, key=lambda s: ce_above[s])
        pe_wall = max(pe_below, key=lambda s: pe_below[s])
        ce_wall_oi_L = round(ce_oi[ce_wall] / 100000, 1)
        pe_wall_oi_L = round(pe_oi[pe_wall] / 100000, 1)

        trade_range = round(abs(ce_wall - pe_wall), 1)
        trade_range_pct = round(trade_range / cmp * 100, 1) if cmp > 0 else 0

        # Range quality label
        if trade_range_pct < 2:
            range_label = "Tight"
        elif trade_range_pct < 5:
            range_label = "Moderate"
        else:
            range_label = "Wide"

        return {
            "ce_wall":        ce_wall,
            "pe_wall":        pe_wall,
            "ce_wall_oi_L":   ce_wall_oi_L,
            "pe_wall_oi_L":   pe_wall_oi_L,
            "trade_range":    trade_range,
            "trade_range_pct": trade_range_pct,
            "range_label":    range_label,
        }
    except Exception as e:
        print(f"[OI_WALLS] {symbol}: {e}")
        return {}
