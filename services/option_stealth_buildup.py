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
        "label": "EOD / since open",
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

        hits.append({
            "symbol": symbol,
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
