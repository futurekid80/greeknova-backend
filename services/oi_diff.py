"""
services/oi_diff.py

Shared "latest 5-min OI/volume diff snapshot across the whole F&O universe"
fetch, used by the custom scanner (services/custom_scanner.py).

NOTE -- known duplication: this is a near-duplicate of the fetch block
inside api/options_jungle.py's get_options_jungle() (same pagination,
same ±4min window widening, same expiry-filtering logic -- see that
function's own comments for why each piece is shaped the way it is).
Extracting it into one shared function that Jungle also calls would be
cleaner, but switching Jungle's already-live, already-verified fetch path
over to a new shared module right now carries real regression risk for
no user-facing benefit. Flagging this the same way CLAUDE.md/memory flags
the "Class 2, duplicated hardcoded lists" bug pattern: if a "why does the
custom scanner disagree with Jungle" report ever comes in, check both
this file and options_jungle.py's fetch_snapshot() for drift.
"""
from datetime import datetime, timezone, timedelta, date as date_type


def _prev_close_ts(supabase, ts_new: str):
    """Last NIFTY snapshot timestamp strictly before ts_new's UTC calendar
    day, i.e. the previous trading session's final capture. NSE hours
    (09:15-15:40 IST) sit inside one UTC date, so a UTC-day cut is safe.
    Returns None if nothing earlier exists or the query fails."""
    try:
        day_start = f"{ts_new[:10]}T00:00:00+00:00"
        r = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .lt("timestamp", day_start)\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        return r.data[0]["timestamp"] if r.data else None
    except Exception as e:
        print(f"[oi_diff] prev-close lookup failed: {e}")
        return None


def fetch_oi_diff_window(supabase, date: str = None, lookback_minutes=5):
    """Generalized sibling of fetch_latest_oi_diff (Oct 27 2026, for Option
    Strike Stealth Buildup's hourly/EOD windows) -- same shape, same
    pagination/expiry-filter/cmp logic, just a configurable baseline instead
    of a hardcoded 5-min lookback. Deliberately a SEPARATE function rather
    than a parameterized fetch_latest_oi_diff: that one is already live and
    verified under custom_scanner.py, and touching its hardcoded-5-min path
    for an unrelated caller carries real regression risk for no benefit --
    same reasoning this file's own docstring already gives for not merging
    with options_jungle.py's near-duplicate fetch.

    lookback_minutes: minutes before the latest snapshot to use as the
    baseline (e.g. 60 for an hourly window). Pass None for "since today's
    market open" -- NOW (Oct 31 2026) the previous trading day's last snapshot
    (prev close), live-running at any point after open and becoming the
    day's final number once the market closes.

    Returns the same 6-tuple as fetch_latest_oi_diff: (new_data, old_map,
    cmp_map, ts_new, ts_old, timestamps)."""
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

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
            return [], {}, {}, None, None, []

    ts_new = timestamps[-1]

    if lookback_minutes is None:
        # EOD/day-cumulative. Oct 31 2026 FIX: baseline is now the PREVIOUS
        # trading day's last snapshot (its closing OI + premium), not today's
        # first capture. The first capture (9:19 IST) is already AFTER the
        # opening premium crash/spike and OI gap, so "since open" hid the real
        # day move (COALINDIA 380 CE read OI +209% / premium -0.8% instead of
        # ~+900% / -34% vs yesterday's close). Falls back to the day's first
        # capture only if no earlier day exists in oi_snapshots.
        ts_old = _prev_close_ts(supabase, ts_new) or timestamps[0]
    else:
        ts_new_dt = datetime.fromisoformat(ts_new.replace('+00:00', '')).replace(tzinfo=timezone.utc)
        target_old = ts_new_dt - timedelta(minutes=lookback_minutes)
        ts_old = min(timestamps[:-1], key=lambda t: abs(
            datetime.fromisoformat(t.replace('+00:00', '')).replace(tzinfo=timezone.utc) - target_old
        ))

    def fetch_snapshot(ts):
        ts_dt = datetime.fromisoformat(ts.replace('+00:00', '')).replace(tzinfo=timezone.utc)
        window_start = (ts_dt - timedelta(minutes=4)).isoformat()
        rows = []
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .gte("timestamp", window_start)\
                .lte("timestamp", ts)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        latest_by_key: dict = {}
        for r in rows:
            key = (r.get("symbol"), r.get("option_type"), r.get("strike"), r.get("expiry"))
            if key not in latest_by_key or r["timestamp"] > latest_by_key[key]["timestamp"]:
                latest_by_key[key] = r
        return list(latest_by_key.values())

    new_data_raw = fetch_snapshot(ts_new)
    old_data_raw = fetch_snapshot(ts_old)

    today_str = date_type.today().isoformat()
    nearest_expiry_map: dict = {}
    for r in new_data_raw:
        sym = r["symbol"]
        exp = r.get("expiry")
        if not exp or exp < today_str:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    def filter_to_nearest_expiry(rows):
        filtered = []
        for r in rows:
            sym = r["symbol"]
            exp = r.get("expiry")
            nearest = nearest_expiry_map.get(sym)
            if nearest and exp == nearest:
                filtered.append(r)
            elif sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]:
                filtered.append(r)
        return filtered

    new_data = filter_to_nearest_expiry(new_data_raw)
    old_data = filter_to_nearest_expiry(old_data_raw)

    cmp_date = ts_new[:10]
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .gte("timestamp", f"{cmp_date}T00:00:00+00:00")\
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

    return new_data, old_map, cmp_map, ts_new, ts_old, timestamps


def fetch_latest_oi_diff(supabase, date: str = None):
    """Returns (new_data, old_map, cmp_map, ts_new, ts_old, timestamps) --
    the latest 5-min-apart pair of OI snapshots across every symbol,
    expiry-filtered to each symbol's nearest active expiry (indices keep
    all their active expiries) the same way Jungle/UOA already do.
    new_data: list of row dicts (latest snapshot). old_map: {symbol_
    tradingsymbol: row} for the earlier snapshot. cmp_map: {symbol:
    latest cmp}. Returns empty structures if fewer than 2 snapshots exist
    for the requested day (e.g. before the first capture cycle)."""
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

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
            return [], {}, {}, None, None, []

    ts_new = timestamps[-1]
    ts_new_dt = datetime.fromisoformat(ts_new.replace('+00:00', '')).replace(tzinfo=timezone.utc)
    target_old = ts_new_dt - timedelta(minutes=5)
    ts_old = min(timestamps[:-1], key=lambda t: abs(
        datetime.fromisoformat(t.replace('+00:00', '')).replace(tzinfo=timezone.utc) - target_old
    ))

    def fetch_snapshot(ts):
        ts_dt = datetime.fromisoformat(ts.replace('+00:00', '')).replace(tzinfo=timezone.utc)
        window_start = (ts_dt - timedelta(minutes=4)).isoformat()
        rows = []
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .gte("timestamp", window_start)\
                .lte("timestamp", ts)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        latest_by_key: dict = {}
        for r in rows:
            key = (r.get("symbol"), r.get("option_type"), r.get("strike"), r.get("expiry"))
            if key not in latest_by_key or r["timestamp"] > latest_by_key[key]["timestamp"]:
                latest_by_key[key] = r
        return list(latest_by_key.values())

    new_data_raw = fetch_snapshot(ts_new)
    old_data_raw = fetch_snapshot(ts_old)

    today_str = date_type.today().isoformat()
    nearest_expiry_map: dict = {}
    for r in new_data_raw:
        sym = r["symbol"]
        exp = r.get("expiry")
        if not exp or exp < today_str:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    def filter_to_nearest_expiry(rows):
        filtered = []
        for r in rows:
            sym = r["symbol"]
            exp = r.get("expiry")
            nearest = nearest_expiry_map.get(sym)
            if nearest and exp == nearest:
                filtered.append(r)
            elif sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]:
                filtered.append(r)
        return filtered

    new_data = filter_to_nearest_expiry(new_data_raw)
    old_data = filter_to_nearest_expiry(old_data_raw)

    # BUG FIX (Oct 12 2026): was bound to `today` (wall-clock date, or the
    # `date` param), but ts_new above already has its own fallback to the
    # latest AVAILABLE snapshot day when the current day has no capture yet
    # (e.g. Monday morning before the first cycle runs, weekends, holidays).
    # Binding this query to wall-clock `today` instead of ts_new's actual
    # date meant cmp_map came back completely empty whenever those two
    # diverged -- which silently emptied index_map downstream (every symbol
    # skipped in run_custom_scan's "if symbol not in index_map: continue"
    # gate), producing a confident, error-free "0 symbols matched" even
    # though the underlying OI diff data had real, large unwinding/buildup
    # moves in it. Options Jungle/UOA have this same `today`-bound cmp
    # query (see oi_diff.py's module docstring on duplication) but aren't
    # gated on cmp the way the custom scanner is, so they never surfaced it.
    cmp_date = ts_new[:10]
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .gte("timestamp", f"{cmp_date}T00:00:00+00:00")\
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

    return new_data, old_map, cmp_map, ts_new, ts_old, timestamps
