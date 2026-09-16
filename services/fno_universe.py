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
    """Return the sorted list of stock symbols that currently have at least
    one live options or futures contract on Kite's NFO segment. Returns an
    empty list (never raises) if Kite isn't reachable -- callers should
    treat an empty result as "couldn't check right now" and keep whatever
    list they already had."""
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        instruments = kite.instruments("NFO")
        names = {
            i["name"] for i in instruments
            if i.get("instrument_type") in ("CE", "PE", "FUT")
            and i.get("name") not in INDICES
        }
        return sorted(names)
    except Exception as e:
        print(f"[fno_universe] could not fetch live F&O symbols from Kite: {e}")
        return []
