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

Tier thresholds below are a FIRST PASS, not validated against a live
trading session (built post-market on an expiry day) -- flagged to Manish
to tune after seeing real results, the same way Custom Scanner's MIN_OI_PCT
was an initial guess. Unlike the FUT-level scanner's daily open->close
OI%/price%, these are per-STRIKE changes over a single 5-min snapshot pair,
which is a very different scale -- don't assume the FUT-level numbers
(ELITE >=2% OI / <=0.5% price) transfer directly; they don't.
"""
from services.oi_diff import fetch_latest_oi_diff
from services.strike_zones import build_strike_index_map, strikes_away

MIN_OLD_OI = 1000        # same floor used everywhere else before trusting a % change
CONFLUENCE_MAX_STRIKES_AWAY = 5  # Manish's Oct 25 call: ATM+-5 only for Confluence

# Hygiene guard for the scanner itself (separate from the tighter
# Confluence-only ATM+-5 cap above). Added after a real-data sanity check
# (Oct 27 2026, on NIFTY's own monthly expiry day -- so an unusually
# churny session) showed ~430 raw hits across the WHOLE F&O universe with
# no zone restriction at all, most of it deep-OTM strikes where OI is thin
# enough that small absolute changes register as huge %, i.e. exactly the
# kind of tail noise Manish flagged for Confluence. Scoping the whole
# scanner to ATM+-10 keeps results to strikes someone could plausibly
# trade, while staying wider than Confluence's tighter ATM+-5.
MAX_STRIKES_AWAY = 10

# tier: (min OI % growth over the 5-min window, max |premium % change|)
TIERS = [
    ("ELITE",  25.0, 3.0),
    ("STRONG", 15.0, 5.0),
    ("WATCH",   8.0, 8.0),
]
TIER_LABELS = {"ELITE": "Elite", "STRONG": "Strong", "WATCH": "Watch"}


def _classify(oi_pct: float, premium_pct: float):
    for tier, min_oi, max_prem in TIERS:
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


def run_option_stealth_scan(supabase, date: str = None) -> dict:
    new_data, old_map, cmp_map, ts_new, ts_old, timestamps = fetch_latest_oi_diff(supabase, date)

    if not new_data or ts_new is None:
        return {"error": "Need at least 2 snapshots", "hits": [], "total": 0}

    index_map = build_strike_index_map(new_data, cmp_map)
    fut_stealth_symbols = _fut_stealth_symbols()

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
        if sa is None or abs(sa) > MAX_STRIKES_AWAY:
            continue
        key = f"{symbol}_{row['tradingsymbol']}"
        old_row = old_map.get(key)
        if not old_row:
            continue

        old_oi = old_row.get("oi") or 0
        new_oi = row.get("oi") or 0
        if old_oi < MIN_OLD_OI:
            continue
        oi_pct = (new_oi - old_oi) / old_oi * 100
        if oi_pct <= 0:
            continue  # BUILD only -- an unwind isn't a "buildup" by definition

        old_premium = old_row.get("last_price") or 0
        new_premium = row.get("last_price") or 0
        if old_premium <= 0:
            continue
        premium_pct = (new_premium - old_premium) / old_premium * 100

        tier, tier_label = _classify(oi_pct, premium_pct)
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
        "total": len(hits),
        "confluence_count": sum(1 for h in hits if h["is_confluence"]),
        "hits": hits,
    }
