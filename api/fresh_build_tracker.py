"""
fresh_build_tracker.py - Fresh Build Signal Persistence + Premium Tracking

Fresh Build signals (option volume spike + genuine OI buildup) were
previously computed live, on-demand, and never persisted anywhere --
each detection existed only for the moment someone happened to load the
Alerts page. This meant there was no way to answer "what happened to
that signal afterward" -- e.g. showing that a Fresh Build on
BHARATFORGE 2320 PE later saw its premium double.

This module adds:
  - capture_fresh_build_signals(): called every 5-min cycle from
    run_full_capture(), same as the alert engine. Finds current Fresh
    Build spikes and persists NEW ones (deduped against the last 90
    minutes for the same contract, so an ongoing multi-cycle move
    records one entry point, not a new row every 5 minutes).
  - get_fresh_build_winners(): for signals from the last N days, looks
    up the peak premium reached since entry (from oi_snapshots, which
    already captures every contract's last_price every 5 minutes -- no
    new capture infrastructure needed for this part) and returns the
    genuine multiple reached, for use in the public landing-highlights
    showcase.
"""
from datetime import datetime, timedelta, timezone


def capture_fresh_build_signals(supabase):
    from api.volume_spike import get_volume_spikes
    try:
        result = get_volume_spikes(threshold=50.0)
        spikes = [s for s in (result.get("spikes") or []) if s.get("oi_signal") == "FRESH_BUILD"]
        if not spikes:
            return

        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=90)).isoformat()

        for s in spikes:
            tsym = s.get("tradingsymbol")
            if not tsym or not s.get("last_price"):
                continue
            existing = supabase.from_("fresh_build_signals")\
                .select("id")\
                .eq("tradingsymbol", tsym)\
                .gte("entry_timestamp", cutoff)\
                .limit(1).execute()
            if existing.data:
                continue  # already have a recent entry for this contract -- same ongoing move

            supabase.table("fresh_build_signals").insert({
                "tradingsymbol": tsym,
                "symbol": s.get("symbol"),
                "strike": s.get("strike"),
                "option_type": s.get("option_type"),
                "entry_premium": s["last_price"],
                "entry_timestamp": now.isoformat(),
                "vol_pct": s.get("vol_pct"),
                "oi_pct": s.get("oi_pct"),
            }).execute()
        print(f"[FRESH_BUILD_TRACKER] Captured {len(spikes)} spike(s) this cycle")
    except Exception as e:
        print(f"[FRESH_BUILD_TRACKER] Capture failed: {e}")


def get_fresh_build_winners(supabase, days_back: int = 5, min_multiple: float = 1.5):
    """Returns Fresh Build signals from the last `days_back` days whose
    premium has since reached at least `min_multiple`x its entry value,
    with the genuine peak multiple actually reached (not current price --
    a peak that later decayed is still a real, honest thing that
    happened, same as how traders naturally describe these moves)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    signals_res = supabase.from_("fresh_build_signals")\
        .select("*")\
        .gte("entry_timestamp", since)\
        .order("entry_timestamp", desc=True)\
        .limit(200).execute()

    winners = []
    for sig in (signals_res.data or []):
        tsym = sig["tradingsymbol"]
        entry_premium = float(sig["entry_premium"] or 0)
        if entry_premium <= 0:
            continue
        peak_res = supabase.from_("oi_snapshots")\
            .select("last_price")\
            .eq("tradingsymbol", tsym)\
            .gte("timestamp", sig["entry_timestamp"])\
            .order("last_price", desc=True)\
            .limit(1).execute()
        if not peak_res.data:
            continue
        peak_premium = float(peak_res.data[0]["last_price"] or 0)
        multiple = round(peak_premium / entry_premium, 2) if entry_premium > 0 else 0
        if multiple >= min_multiple:
            winners.append({
                "symbol": sig["symbol"],
                "tradingsymbol": tsym,
                "strike": sig["strike"],
                "option_type": sig["option_type"],
                "entry_premium": entry_premium,
                "peak_premium": peak_premium,
                "multiple": multiple,
                "entry_timestamp": sig["entry_timestamp"],
            })

    winners.sort(key=lambda w: -w["multiple"])
    return winners
