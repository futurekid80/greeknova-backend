from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type


SYMBOLS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY",
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH","TATACONSUM",
    "TATASTEEL","ADANIENT","POWERGRID","NTPC","ONGC","JSWSTEEL","COALINDIA",
    "BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA",
    "DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE",
    "INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT",
    "ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS",
    "HAL","INDIGO","PFC","RECLTD","SAIL","TATAPOWER","VEDL",
]


def get_vacuum_scanner(max_distance_pct: float = 10.0):
    """
    Scan all F&O stocks for vacuum zones near current price.
    A true vacuum = BOTH CE and PE OI are low at that strike.
    Uses combined total OI threshold — not separate CE/PE thresholds.
    """
    supabase = get_supabase()

    # ── Find latest data date ─────────────────────────────────────────────────
    data_date = None
    for i in range(7):
        check = (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
        r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{check}T00:00:00+00:00")\
            .lt("timestamp",  f"{check}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()
        if r.data:
            data_date = check
            break

    if not data_date:
        return {"scan_time": datetime.now(timezone.utc).isoformat(), "data_date": None, "total": 0, "results": []}

    # ── Get latest CMP for all symbols ───────────────────────────────────────
    cmp_rows = supabase.from_("cmp_prices")\
        .select("symbol, cmp")\
        .gte("timestamp", f"{data_date}T00:00:00+00:00")\
        .lt("timestamp",  f"{data_date}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(500).execute().data or []

    cmp_map: dict = {}
    seen: set = set()
    for r in cmp_rows:
        if r["symbol"] not in seen:
            cmp_map[r["symbol"]] = float(r["cmp"])
            seen.add(r["symbol"])

    results = []

    for symbol in SYMBOLS:
        cmp = cmp_map.get(symbol)
        if not cmp or cmp <= 0:
            continue

        # Get EOD timestamp for this symbol
        ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", symbol)\
            .gte("timestamp", f"{data_date}T00:00:00+00:00")\
            .lt("timestamp",  f"{data_date}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()

        if not ts_q.data:
            continue

        eod_ts = ts_q.data[0]["timestamp"]

        # Get nearest expiry
        exp_q = supabase.from_("oi_snapshots")\
            .select("expiry")\
            .eq("symbol", symbol)\
            .eq("timestamp", eod_ts)\
            .limit(100).execute()

        today_str = date_type.today().isoformat()
        expiries = sorted(set(
            r["expiry"] for r in (exp_q.data or [])
            if r["expiry"] and r["expiry"] >= today_str
        ))
        active_expiry = expiries[0] if expiries else None

        # Fetch OI data — filter to ±max_distance_pct of CMP
        lower = cmp * (1 - max_distance_pct / 100)
        upper = cmp * (1 + max_distance_pct / 100)
        all_rows = []
        for offset in range(0, 10000, 1000):
            q = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi")\
                .eq("symbol", symbol)\
                .eq("timestamp", eod_ts)\
                .gte("strike", lower)\
                .lte("strike", upper)
            if active_expiry:
                q = q.eq("expiry", active_expiry)
            batch = q.range(offset, offset+999).execute()
            if not batch.data:
                break
            all_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break

        if not all_rows:
            continue

        # Build CE/PE OI per strike
        ce_oi: dict = {}
        pe_oi: dict = {}
        for r in all_rows:
            s = float(r["strike"])
            oi = r["oi"] or 0
            if r["option_type"] == "CE":
                ce_oi[s] = ce_oi.get(s, 0) + oi
            else:
                pe_oi[s] = pe_oi.get(s, 0) + oi

        all_strikes = sorted(set(list(ce_oi.keys()) + list(pe_oi.keys())))
        if not all_strikes:
            continue

        # ── Liquidity filter ──────────────────────────────────────────────────
        total_oi_all = sum(ce_oi.values()) + sum(pe_oi.values())
        if total_oi_all < 10_00_000:  # skip illiquid stocks
            continue

        max_ce = max(ce_oi.values()) if ce_oi else 1
        max_pe = max(pe_oi.values()) if pe_oi else 1
        if max_ce < 50_000 and max_pe < 50_000:
            continue

        # ── FIXED: Combined OI threshold ──────────────────────────────────────
        # max combined OI at any single strike in the range
        max_combined = max(
            (ce_oi.get(s, 0) + pe_oi.get(s, 0)) for s in all_strikes
        )

        # Vacuum = total OI at strike is less than 5% of the peak combined OI
        # AND neither CE nor PE alone exceeds 10% of its own maximum
        # This prevents one-sided high OI from passing as vacuum
        vac_combined_thresh = max_combined * 0.05
        vac_ce_abs_thresh   = max_ce * 0.10   # absolute CE cap
        vac_pe_abs_thresh   = max_pe * 0.10   # absolute PE cap

        vacuums = []
        for s in all_strikes:
            ce = ce_oi.get(s, 0)
            pe = pe_oi.get(s, 0)
            total = ce + pe

            if total == 0:
                continue

            # TRUE VACUUM: combined total is tiny AND neither side dominates
            is_combined_low = total < vac_combined_thresh
            is_ce_low       = ce < vac_ce_abs_thresh
            is_pe_low       = pe < vac_pe_abs_thresh
            is_vacuum       = is_combined_low and is_ce_low and is_pe_low

            if not is_vacuum:
                continue

            dist_pct = round((s - cmp) / cmp * 100, 2)
            if abs(dist_pct) > max_distance_pct:
                continue

            vacuums.append({
                "strike":    s,
                "ce_oi":     ce,
                "pe_oi":     pe,
                "total_oi":  total,
                "dist_pct":  dist_pct,
                "direction": "ABOVE" if dist_pct > 0 else "BELOW",
            })

        if not vacuums:
            continue

        vacuums.sort(key=lambda x: abs(x["dist_pct"]))

        above = [v for v in vacuums if v["direction"] == "ABOVE"]
        below = [v for v in vacuums if v["direction"] == "BELOW"]

        results.append({
            "symbol":        symbol,
            "cmp":           cmp,
            "is_index":      symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "vacuums":       vacuums,
            "nearest_above": above[0] if above else None,
            "nearest_below": below[0] if below else None,
            "vacuum_count":  len(vacuums),
            "expiry":        active_expiry,
            "data_date":     data_date,
        })

    results.sort(key=lambda x: min(
        abs(x["nearest_above"]["dist_pct"]) if x["nearest_above"] else 999,
        abs(x["nearest_below"]["dist_pct"]) if x["nearest_below"] else 999,
    ))

    return {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "data_date": data_date,
        "total":     len(results),
        "results":   results,
    }
