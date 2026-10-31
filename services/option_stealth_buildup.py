"""
services/option_stealth_buildup.py

"Option Strike Stealth Buildup" -- Oct 25 2026 feature idea, built Oct 27
2026 per Manish's go-ahead. Extends the existing FUT-level Stealth Buildup
concept (api/positional_intelligence.py: top-tier FUT OI growth + a small
price candle, i.e. quiet accumulation) DOWN to individual option strikes:
OI growing hard at a specific strike while that strike's OWN premium barely
moves -- the options-chain equivalent of "big size going on without moving
the tape."

Scope decisions locked with Manish on Oct 25 2026 (see memory notes):
  - Separate scanner from the FUT-level one, not merged -- this needs the
    ~5-min snapshot cadence to catch rapid intraday buildups; the FUT-level
    scanner runs on a slower (EOD-rolled) cadence that would miss them.
  - Scanner-first. No per-strike badge on /strike-history.
  - A "Confluence" flag (this symbol ALSO has a live FUT-level Stealth
    Buildup hit right now) is the actual noise-reducer -- restricted to
    ATM+-5 strikes only; beyond that Manish's call is "tail/hedging noise".
  - Built on top of the existing whole-universe 5-min snapshot machinery
    (services/oi_diff.py's fetch_latest_oi_diff, already used by
    services/custom_scanner.py) rather than new infra. The one missing
    piece custom_scanner.py didn't have was a "premium stayed quiet"
    condition type (it only ever filtered on OI change) -- that's the new
    logic here.
  - Manual-run only for now, same as Custom Scanner (no push alerting) --
    prove it live first.

Oct 27 2026 follow-up -- multiple time windows: Manish wants to use this
the same way he uses Positional Intelligence's Elite Setups -- an EOD/
since-open cumulative read at day end to plan next-day trades -- on top of
the original 5-min "instant pulse" window, plus an hourly window as a
middle ground for intraday monitoring. All three windows share the same
scan logic (services/oi_diff.py's new fetch_oi_diff_window, a generalized
sibling of fetch_latest_oi_diff with a configurable baseline) and the same
zone/Confluence machinery below -- only the baseline snapshot, the min-OI
floor, and the tier cutoffs change per window (see WINDOWS below).

IMPORTANT CALIBRATION CAVEAT: the hourly/EOD tier cutoffs were sanity-
checked against real Supabase data on Oct 27 2026 -- but Oct 27 2026 is
NIFTY's own monthly expiry day, which turned out to make that data useless
for calibration: next-month OI grows mechanically by several hundred to
several thousand percent all day as positions roll from the expiring
contract into the new month (confirmed via direct SQL -- e.g. a liquid
large-cap CE at 20k+ opening OI still closed the day up 600-1100%). That's
structural rollover volume, not a "stealth" signal, and it would swamp any
threshold picked from today's numbers. The cutoffs below are therefore a
genuine first-pass GUESS (same posture the original 5-min thresholds
started from), picked to be directionally sensible rather than fitted to
today's contaminated data -- flagged to Manish to tune once he's watched a
normal (non-expiry) session on each window, same as the 5-min tiers.
"""
from services.oi_diff import fetch_latest_oi_diff, fetch_oi_diff_window
from services.strike_zones import build_strike_index_map, strikes_away, in_zone, ZONE_OPTIONS

CONFLUENCE_MAX_STRIKES_AWAY = 5  # Manish's Oct 25 call: ATM+-5 only for Confluence

# (Oct 27 2026): a real-data sanity check (on NIFTY's own monthly expiry
# day -- so an unusually churny session) showed ~430 raw hits across the
# WHOLE F&O universe with no zone restriction at all, most of it deep-OTM
# strikes where OI is thin enough that small absolute changes register as
# huge %. Manish's own call: give this the SAME selectable ATM/+-1/+-2/
# +-5/+-10/All zone filter already used on Jungle/UOA/OI Profile/Alerts
# (services/strike_zones.py's ZONE_OPTIONS) instead of a hardcoded cap --
# default to "5" so it's quiet out of the box, same spirit as his "ATM+-5
# to avoid noise" ask.
DEFAULT_ZONE = "5"

DEFAULT_WINDOW = "5min"

# Per-window config: lookback_minutes (None = since today's market open,
# live-running), min_old_oi (floor before trusting a % change -- raised for
# the longer windows since the baseline has had more time to grow and
# illiquid-contract noise gets worse the further back the baseline goes),
# and tiers (min OI % growth over the window, max |premium % change| over
# the same window). See the module docstring's calibration caveat above --
# hourly/EOD numbers are an unvalidated first pass.
WINDOWS = {
    "5min": {
        "label": "5-min",
        "lookback_minutes": 5,
        "min_old_oi": 1000,
        "tiers": [
            ("ELITE",  25.0, 3.0),
            ("STRONG", 15.0, 5.0),
            ("WATCH",   8.0, 8.0),
        ],
    },
    "hourly": {
        "label": "Hourly",
        "lookback_minutes": 60,
        "min_old_oi": 3000,
        "tiers": [
            ("ELITE",  40.0, 6.0),
            ("STRONG", 25.0, 10.0),
            ("WATCH",  15.0, 15.0),
        ],
    },
    "eod": {
        "label": "EOD / vs prev close",
        "lookback_minutes": None,
        "min_old_oi": 10000,
        "tiers": [
            ("ELITE",  60.0, 10.0),
            ("STRONG", 40.0, 18.0),
            ("WATCH",  25.0, 28.0),
        ],
    },
}
TIER_LABELS = {"ELITE": "Elite", "STRONG": "Strong", "WATCH": "Watch"}


def _activity(opt_type: str, premium_pct: float):
    """Oct 31 2026: Writing vs Buying. OI up + premium down = sellers adding
    (WRITING); OI up + premium up = buyers adding (BUYING); within +-0.5% is
    NEUTRAL (genuinely quiet). A CE writing is bearish-leaning, a PE writing
    bullish-leaning -- the label says WHO is adding, the user reads direction."""
    side = "Call" if opt_type == "CE" else "Put"
    if premium_pct <= -0.5:
        return "WRITING", f"{side} Writing"
    if premium_pct >= 0.5:
        return "BUYING", f"{side} Buying"
    return "NEUTRAL", f"{side} Quiet"


def _classify(oi_pct: float, premium_pct: float, tiers):
    for tier, min_oi, max_prem in tiers:
        if oi_pct >= min_oi and abs(premium_pct) <= max_prem:
            return tier, TIER_LABELS[tier]
    return None, None


def _fut_stealth_symbols():
    """Symbols with a live FUT-level Stealth Buildup hit right now, for the
    Confluence flag. Best-effort -- if positional_intelligence is slow/down
    this scanner should still return strike-level results on their own."""
    try:
        from api.positional_intelligence import get_positional_intelligence
        pi = get_positional_intelligence(min_consec=0)
        return {row["symbol"] for row in (pi.get("stealth_buildup") or [])}
    except Exception as e:
        print(f"[OptionStealth] FUT-level confluence lookup failed: {e}")
        return set()


def run_option_stealth_scan(supabase, date: str = None, zone: str = DEFAULT_ZONE, window: str = DEFAULT_WINDOW) -> dict:
    cfg = WINDOWS.get(window) or WINDOWS[DEFAULT_WINDOW]

    if cfg["lookback_minutes"] == 5:
        # Keep using the original, already-live fetch for the default 5-min
        # window -- zero behavior change for existing callers/results.
        new_data, old_map, cmp_map, ts_new, ts_old, timestamps = fetch_latest_oi_diff(supabase, date)
    else:
        new_data, old_map, cmp_map, ts_new, ts_old, timestamps = fetch_oi_diff_window(
            supabase, date, lookback_minutes=cfg["lookback_minutes"]
        )

    if not new_data or ts_new is None:
        return {"error": "Need at least 2 snapshots", "hits": [], "total": 0}

    index_map = build_strike_index_map(new_data, cmp_map)
    fut_stealth_symbols = _fut_stealth_symbols()

    min_old_oi = cfg["min_old_oi"]
    tiers = cfg["tiers"]

    hits = []
    for row in new_data:
        opt_type = row.get("option_type")
        if opt_type not in ("CE", "PE"):
            continue
        symbol = row["symbol"]
        strike = row.get("strike")
        if strike is None or symbol not in index_map:
            continue

        sa = strikes_away(symbol, strike, index_map)
        if not in_zone(sa, zone):
            continue
        key = f"{symbol}_{row['tradingsymbol']}"
        old_row = old_map.get(key)
        if not old_row:
            continue

        old_oi = old_row.get("oi") or 0
        new_oi = row.get("oi") or 0
        if old_oi < min_old_oi:
            continue
        oi_pct = (new_oi - old_oi) / old_oi * 100
        if oi_pct <= 0:
            continue  # BUILD only -- an unwind isn't a "buildup" by definition

        old_premium = old_row.get("last_price") or 0
        new_premium = row.get("last_price") or 0
        if old_premium <= 0:
            continue
        premium_pct = (new_premium - old_premium) / old_premium * 100

        tier, tier_label = _classify(oi_pct, premium_pct, tiers)
        if not tier:
            continue

        activity, activity_label = _activity(opt_type, premium_pct)
        hits.append({
            "symbol": symbol,
            "tradingsymbol": row.get("tradingsymbol"),
            "activity": activity,
            "activity_label": activity_label,
            "is_confluence": (
                symbol in fut_stealth_symbols
                and sa is not None
                and abs(sa) <= CONFLUENCE_MAX_STRIKES_AWAY
            ),
            "strike": strike,
            "expiry": row.get("expiry"),  # YYYY-MM-DD -- lets Manish tell weekly vs monthly apart at a glance
            "option_type": opt_type,
            "strikes_away": sa,
            "cmp": cmp_map.get(symbol),
            "atm_strike": index_map[symbol]["atm_strike"],
            "tier": tier,
            "tier_label": tier_label,
            "oi_pct": round(oi_pct, 2),
            "old_oi": old_oi,
            "new_oi": new_oi,
            "premium_pct": round(premium_pct, 2),
            "old_premium": old_premium,
            "new_premium": new_premium,
        })

    tier_rank = {"ELITE": 0, "STRONG": 1, "WATCH": 2}
    hits.sort(key=lambda h: (not h["is_confluence"], tier_rank[h["tier"]], -h["oi_pct"]))

    return {
        "date": (date or (ts_new[:10] if ts_new else None)),
        "ts_new": ts_new,
        "ts_old": ts_old,
        "zone": zone,
        "window": window,
        "window_label": cfg["label"],
        "tiers": tiers,  # so the frontend can render the correct legend numbers per window without hardcoding them
        "total": len(hits),
        "confluence_count": sum(1 for h in hits if h["is_confluence"]),
        "hits": hits,
    }



# ---------------------------------------------------------------------------
# Oct 31 2026: persistence (mirrors the Wolves fresh-events pattern)
# ---------------------------------------------------------------------------
# A background job calls run_persist_tick() every 5 min in market hours. It
# runs all three windows and upserts every hit into option_stealth_events
# (one row per day+window+contract) tracking first_seen / last_seen /
# times_seen, so a strike that qualifies briefly is no longer lost when
# nobody has the page open. Freshness (Fresh Now / Still Active / Seen
# Earlier Today) is computed at READ time from last_seen, because a hit that
# stops qualifying simply stops being updated.
from datetime import datetime, timezone, timedelta

PERSIST_ZONE = "5"          # same default the page shows; keeps volume sane
FRESH_WINDOW_MIN = 7        # last_seen within this => still on the current tick
TIER_RANK = {"ELITE": 0, "STRONG": 1, "WATCH": 2}


def _parse_ts(ts: str):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def persist_scan_result(supabase, window: str, result: dict) -> int:
    """Upsert one window's scan hits. Returns number of rows written."""
    hits = result.get("hits") or []
    ts_new = result.get("ts_new")
    if not hits or not ts_new:
        return 0
    scan_date = ts_new[:10]
    seen_dt = _parse_ts(ts_new)

    existing_rows = supabase.from_("option_stealth_events").select(
        "event_key, first_seen, last_seen, times_seen, best_tier, peak_oi_pct"
    ).eq("scan_date", scan_date).eq("window", window).execute().data or []
    existing = {r["event_key"]: r for r in existing_rows}

    rows = []
    for h in hits:
        if not h.get("tradingsymbol"):
            continue
        key = f"{scan_date}|{window}|{h['tradingsymbol']}"
        prev = existing.get(key)
        if prev:
            prev_last = _parse_ts(prev["last_seen"])
            if prev_last and seen_dt <= prev_last:
                continue  # same snapshot already counted (manual re-run etc.)
            first_seen = prev["first_seen"]
            times_seen = int(prev.get("times_seen") or 1) + 1
            best_tier = h["tier"] if TIER_RANK[h["tier"]] < TIER_RANK.get(prev.get("best_tier") or "WATCH", 2) else prev.get("best_tier")
            peak_oi = max(float(prev.get("peak_oi_pct") or 0), h["oi_pct"])
        else:
            first_seen = ts_new
            times_seen = 1
            best_tier = h["tier"]
            peak_oi = h["oi_pct"]
        rows.append({
            "event_key": key, "scan_date": scan_date, "window": window,
            "symbol": h["symbol"], "tradingsymbol": h["tradingsymbol"],
            "strike": h["strike"], "expiry": h.get("expiry"), "option_type": h["option_type"],
            "strikes_away": h.get("strikes_away"), "atm_strike": h.get("atm_strike"), "cmp": h.get("cmp"),
            "tier": h["tier"], "best_tier": best_tier,
            "activity": h.get("activity"), "activity_label": h.get("activity_label"),
            "oi_pct": h["oi_pct"], "peak_oi_pct": peak_oi,
            "old_oi": h["old_oi"], "new_oi": h["new_oi"],
            "premium_pct": h["premium_pct"], "old_premium": h["old_premium"], "new_premium": h["new_premium"],
            "is_confluence": bool(h.get("is_confluence")),
            "first_seen": first_seen, "last_seen": ts_new, "times_seen": times_seen,
        })
    if rows:
        supabase.from_("option_stealth_events").upsert(rows, on_conflict="event_key").execute()
    return len(rows)


def run_persist_tick(supabase) -> dict:
    """Run all three windows once and persist hits. Safe to call repeatedly."""
    out = {}
    for window in WINDOWS:
        try:
            res = run_option_stealth_scan(supabase, None, PERSIST_ZONE, window)
            out[window] = persist_scan_result(supabase, window, res) if not res.get("error") else res["error"]
        except Exception as e:
            print(f"[OptionStealth] persist tick failed for {window}: {e}")
            out[window] = f"error: {e}"
    return out


def get_saved_events(supabase, date: str = None, window: str = DEFAULT_WINDOW, zone: str = DEFAULT_ZONE) -> dict:
    """Read saved hits for a day (falls back to the latest day that has any),
    with freshness computed from last_seen."""
    window = window if window in WINDOWS else DEFAULT_WINDOW
    q_date = date
    if not q_date:
        latest = supabase.from_("option_stealth_events").select("scan_date").order("scan_date", desc=True).limit(1).execute().data
        q_date = latest[0]["scan_date"] if latest else None
    if not q_date:
        return {"date": None, "window": window, "total": 0, "events": []}

    rows = supabase.from_("option_stealth_events").select("*").eq("scan_date", q_date).eq("window", window)\
        .order("last_seen", desc=True).limit(2000).execute().data or []

    all_rows = supabase.from_("option_stealth_events").select("last_seen").eq("scan_date", q_date)\
        .order("last_seen", desc=True).limit(1).execute().data
    ref = _parse_ts(all_rows[0]["last_seen"]) if all_rows else None
    now = datetime.now(timezone.utc)
    if ref and q_date == now.strftime("%Y-%m-%d") and (now - ref) < timedelta(minutes=FRESH_WINDOW_MIN * 2):
        ref = max(ref, now - timedelta(minutes=1))  # live: measure against the current tick

    events = []
    for r in rows:
        sa = r.get("strikes_away")
        if sa is not None and not in_zone(sa, zone):
            continue
        last = _parse_ts(r["last_seen"])
        age_min = (ref - last).total_seconds() / 60 if ref and last else 9999
        if age_min <= FRESH_WINDOW_MIN:
            r["freshness"] = "Fresh Now" if (r.get("times_seen") or 1) <= 1 else "Still Active"
        else:
            r["freshness"] = "Seen Earlier Today"
        events.append(r)
    events.sort(key=lambda e: ({"Fresh Now": 0, "Still Active": 1, "Seen Earlier Today": 2}[e["freshness"]],
                               TIER_RANK.get(e["best_tier"], 3), -float(e["oi_pct"] or 0)))
    return {"date": q_date, "window": window, "window_label": WINDOWS[window]["label"],
            "zone": zone, "total": len(events), "events": events}
