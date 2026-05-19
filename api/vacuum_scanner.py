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

# Hard absolute cap — no strike with either side > 1L can be a vacuum
# regardless of the stock's overall OI scale
HARD_CAP_PER_SIDE = 1_00_000  # 1 Lakh


def get_vacuum_scanner(max_distance_pct: float = 10.0):
    """
    Scan all F&O stocks for vacuum zones near current price.
    Also detects stocks APPROACHING a vacuum zone (within 1% of a vacuum strike).

    Vacuum definition (all three must be true):
    1. Combined CE+PE OI < 5% of peak combined OI at any strike
    2. CE alone < 10% of max CE (soft cap)
    3. PE alone < 10% of max PE (soft cap)
    4. HARD CAP: neither CE nor PE exceeds 1L individually
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
        return {
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "data_date": None, "total": 0,
            "results": [], "approaching": []
        }

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

    results    = []
    approaching = []

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

        # Fetch OI — wider range (15%) to catch approaching vacuums too
        fetch_range = max(max_distance_pct, 15.0)
        lower = cmp * (1 - fetch_range / 100)
        upper = cmp * (1 + fetch_range / 100)
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

        # Liquidity filter
        total_oi_all = sum(ce_oi.values()) + sum(pe_oi.values())
        if total_oi_all < 10_00_000:
            continue

        max_ce = max(ce_oi.values()) if ce_oi else 1
        max_pe = max(pe_oi.values()) if pe_oi else 1
        if max_ce < 50_000 and max_pe < 50_000:
            continue

        # Combined threshold
        max_combined = max(
            (ce_oi.get(s, 0) + pe_oi.get(s, 0)) for s in all_strikes
        )
        vac_combined_thresh = max_combined * 0.05
        vac_ce_soft_thresh  = max_ce * 0.10
        vac_pe_soft_thresh  = max_pe * 0.10

        vacuums    = []
        near_zones = []  # strikes that are close to being vacuums

        for s in all_strikes:
            ce = ce_oi.get(s, 0)
            pe = pe_oi.get(s, 0)
            total = ce + pe

            if total == 0:
                continue

            dist_pct = round((s - cmp) / cmp * 100, 2)

            # ── HARD CAP: either side > 1L = never a vacuum ───────────────────
            if ce > HARD_CAP_PER_SIDE or pe > HARD_CAP_PER_SIDE:
                continue

            # ── Soft thresholds ───────────────────────────────────────────────
            is_combined_low = total < vac_combined_thresh
            is_ce_low       = ce < vac_ce_soft_thresh
            is_pe_low       = pe < vac_pe_soft_thresh
            is_vacuum       = is_combined_low and is_ce_low and is_pe_low

            if not is_vacuum:
                continue

            # Classify into vacuum (within max_distance_pct) or approaching
            if abs(dist_pct) <= max_distance_pct:
                vacuums.append({
                    "strike":    s,
                    "ce_oi":     ce,
                    "pe_oi":     pe,
                    "total_oi":  total,
                    "dist_pct":  dist_pct,
                    "direction": "ABOVE" if dist_pct > 0 else "BELOW",
                })
            elif abs(dist_pct) <= 15.0:
                # Within 15% but outside max_distance — approaching zone
                near_zones.append({
                    "strike":    s,
                    "ce_oi":     ce,
                    "pe_oi":     pe,
                    "total_oi":  total,
                    "dist_pct":  dist_pct,
                    "direction": "ABOVE" if dist_pct > 0 else "BELOW",
                })

        # Add to results if has vacuums within range
        if vacuums:
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
        elif near_zones:
            # No vacuum within range but approaching ones exist
            near_zones.sort(key=lambda x: abs(x["dist_pct"]))
            nearest = near_zones[0]
            approaching.append({
                "symbol":       symbol,
                "cmp":          cmp,
                "is_index":     symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
                "nearest_zone": nearest,
                "all_zones":    near_zones,
                "expiry":       active_expiry,
                "data_date":    data_date,
            })

    # Sort results by nearest vacuum distance
    results.sort(key=lambda x: min(
        abs(x["nearest_above"]["dist_pct"]) if x["nearest_above"] else 999,
        abs(x["nearest_below"]["dist_pct"]) if x["nearest_below"] else 999,
    ))

    # Sort approaching by nearest zone distance
    approaching.sort(key=lambda x: abs(x["nearest_zone"]["dist_pct"]))

    return {
        "scan_time":  datetime.now(timezone.utc).isoformat(),
        "data_date":  data_date,
        "total":      len(results),
        "approaching_total": len(approaching),
        "results":    results,
        "approaching": approaching,
    }
