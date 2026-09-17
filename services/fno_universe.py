"""Derives the live F&O-tradable stock universe directly from Kite's own
instrument master, instead of relying on a hand-maintained hardcoded list.

Why this exists: GreekNova used to hardcode its ~180-stock universe in
api/iv_analysis.py (SYMBOLS). Whenever NSE dropped a stock from F&O
eligibility (this happens via a periodic review, and can silently remove
20-30 stocks at once) or a company demerged into a new ticker (e.g. RAYMOND
-> RAYMONDLSL, TATAMOTORS -> TMPV), the hardcoded list had no way of knowing
-- it just kept trying to capture data for a symbol that no longer has any
F&O contracts, got nothing back, and silently produced a gap. This module
asks Kite directly instead, so that gap can't happen again.
"""

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]


def get_live_fno_symbols():
    """Return the sorted list of STOCK symbols that currently have at least
    one live options or futures contract on Kite's NFO segment. Returns an
    empty list (never raises) if Kite isn't reachable -- callers should
    treat an empty result as "couldn't check right now" and keep whatever
    list they already had.

    BUG FIX (Sep 17 2026): originally this just excluded the 3 known index
    names (NIFTY/BANKNIFTY/FINNIFTY). That missed MIDCPNIFTY, NIFTYNXT50,
    and NIFTYFPI ("Nifty India FPI 150", launched Aug 2026) -- all real NSE
    index derivatives, none of them stocks -- which leaked into the tracked
    stock universe as if they were companies. A hardcoded index-name list is
    exactly the fragile pattern this module was built to get away from (NSE
    launches new index derivatives periodically), so instead of adding those
    three names to a list, this now checks each F&O underlying against
    Kite's own NSE equity listing: if it isn't a real listed stock there,
    it's not a stock here either -- whatever index NSE launches next is
    excluded automatically, with nothing to maintain."""
    return get_live_fno_symbols_debug()[0]


def get_live_fno_symbols_debug():
    """Same as get_live_fno_symbols() but also returns the excluded-underlying
    breakdown for auditing. TEMP (Sep 17 2026) -- remove alongside the debug
    endpoints once the symbol count is fully reconciled."""
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        nfo_instruments = kite.instruments("NFO")
        nse_instruments = kite.instruments("NSE")
        # BUG FIX (Sep 17 2026, round 2): was matching by NSE's company
        # "name" field (e.g. "ABB INDIA") against NFO's underlying "name"
        # field (e.g. "ABB") -- these are NOT always the same string, so any
        # stock whose full company name differs from its trading symbol
        # (confirmed live: ABB) silently failed this match and got wrongly
        # excluded from the tracked universe, exactly like the index-leakage
        # bug this same cross-reference was built to fix. Matching against
        # NSE's "tradingsymbol" field instead -- that's what Kite's F&O
        # underlying names actually follow, so it doesn't have this gap.
        equity_tradingsymbols = {
            i["tradingsymbol"] for i in nse_instruments
            if i.get("instrument_type") == "EQ"
        }
        nfo_names = {
            i["name"] for i in nfo_instruments
            if i.get("instrument_type") in ("CE", "PE", "FUT")
        }
        stocks = sorted(nfo_names & equity_tradingsymbols)
        dropped = sorted(nfo_names - equity_tradingsymbols - set(INDICES))

        # SANITY FLOOR (Sep 17 2026): if Kite's NSE equity dump comes back
        # incomplete or rate-limited (seen in production: a boot where the
        # /NSE instruments call raced the startup login + immediate capture
        # and returned too few rows), almost every real stock fails the
        # equity cross-reference and gets wrongly treated as "not a stock" --
        # silently shrinking the tracked universe to ~24-27 symbols with no
        # error anywhere. NSE has consistently had 190+ F&O-eligible stocks
        # for years, so anything drastically below that means this response
        # can't be trusted -- treat it the same as "Kite unreachable" and let
        # the caller fall back to the last-known-good list instead of
        # accepting an implausibly small one.
        MIN_PLAUSIBLE_STOCKS = 150
        if len(stocks) < MIN_PLAUSIBLE_STOCKS:
            print(f"[fno_universe] got only {len(stocks)} stocks (need >={MIN_PLAUSIBLE_STOCKS}) -- "
                  f"Kite's NSE equity response looks incomplete/rate-limited, discarding this "
                  f"result and falling back instead of shrinking the tracked universe")
            return []

        if dropped:
            print(f"[fno_universe] excluded {len(dropped)} non-equity F&O underlyings (indices etc): {dropped}")
        return stocks, dropped
    except Exception as e:
        print(f"[fno_universe] could not fetch live F&O symbols from Kite: {e}")
        return [], []
