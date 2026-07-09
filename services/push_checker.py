"""
push_checker.py
Server-side mirror of the OI spike / fresh build / UOA whale detection that
previously only ran inside the browser's service worker (frontend/public/sw.js).
Running this on the backend scheduler means alerts fire reliably even when
no browser tab is open.

Mirrors sw.js's checkOptionsJungle() and checkUOAWhales() logic and message
formatting exactly, so alerts look identical whether delivered via push or
via the (still-present) in-browser fallback.
"""
from datetime import datetime, timezone

_previous_keys: set = set()
_last_reset_date: str = ""


def _reset_if_new_day():
    global _previous_keys, _last_reset_date
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _last_reset_date:
        _previous_keys = set()
        _last_reset_date = today


def _fmt_oi(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    if abs(n) >= 10000000:
        return f"{n/10000000:.1f}Cr"
    if abs(n) >= 100000:
        return f"{n/100000:.1f}L"
    return f"{n:,.0f}"


def run_push_checks(supabase):
    """Called every 5 minutes by the scheduler during market hours."""
    _reset_if_new_day()
    try:
        _check_options_jungle(supabase)
    except Exception as e:
        print(f"[PushCheck] Options Jungle failed: {e}")
    try:
        _check_uoa_whales(supabase)
    except Exception as e:
        print(f"[PushCheck] UOA failed: {e}")


def _check_options_jungle(supabase):
    from main import options_jungle
    from api.push_notifications import broadcast_alert

    json_data = options_jungle(oi_threshold=2.0, vol_threshold=50.0)
    ts_new = json_data.get("ts_new", "")

    for spike in (json_data.get("oi_spikes") or []):
        key = f"oi_{spike.get('tradingsymbol')}_{ts_new}"
        if key in _previous_keys:
            continue
        _previous_keys.add(key)

        oi_pct = spike.get("oi_pct", 0)
        interp = spike.get("interpretation")
        body = (
            f"OI {'+' if oi_pct > 0 else ''}{oi_pct}% in 5 min | "
            f"OI: {_fmt_oi(spike.get('new_oi'))} | LTP: ₹{spike.get('last_price')}"
            + (f" | {interp.replace('_', ' ')}" if interp else "")
        )

        broadcast_alert(supabase, {
            "signal": "OI_SPIKE",
            "symbol": spike.get("symbol"),
            "strike": spike.get("strike"),
            "optionType": spike.get("option_type"),
            "direction": spike.get("direction"),
            "message": body,
            "url": "/jungle",
            "oiPct": oi_pct,
            "ltp": spike.get("last_price"),
        })

    for spike in (json_data.get("vol_spikes") or []):
        if spike.get("vol_signal") != "FRESH_BUILD":
            continue
        key = f"vol_{spike.get('tradingsymbol')}_{ts_new}"
        if key in _previous_keys:
            continue
        _previous_keys.add(key)

        body = f"Vol +{spike.get('vol_pct')}% | OI +{spike.get('oi_pct')}% | LTP: ₹{spike.get('last_price')}"

        broadcast_alert(supabase, {
            "signal": "FRESH_BUILD",
            "symbol": spike.get("symbol"),
            "strike": spike.get("strike"),
            "optionType": spike.get("option_type"),
            "message": body,
            "url": "/jungle",
            "volPct": spike.get("vol_pct"),
            "oiPct": spike.get("oi_pct"),
            "ltp": spike.get("last_price"),
        })


def _check_uoa_whales(supabase):
    from main import uoa
    from api.push_notifications import broadcast_alert

    json_data = uoa()
    ts = json_data.get("timestamp", "")

    for sig in (json_data.get("signals") or []):
        if (sig.get("score") or 0) < 4:
            continue
        key = f"uoa_{sig.get('tradingsymbol')}_{ts}"
        if key in _previous_keys:
            continue
        _previous_keys.add(key)

        oi_chg = sig.get("oi_chg_30min", 0)
        ltp_chg = sig.get("ltp_chg_from_open", 0)
        body = (
            f"Score {sig.get('score')}/5 | OI 30m: {'+' if oi_chg > 0 else ''}{oi_chg}% | "
            f"LTP from open: {'+' if ltp_chg > 0 else ''}{ltp_chg}% | {sig.get('bias')} bias"
        )

        broadcast_alert(supabase, {
            "signal": sig.get("signal_type"),
            "symbol": sig.get("symbol"),
            "strike": sig.get("strike"),
            "optionType": sig.get("option_type"),
            "message": body,
            "url": "/uoa",
            "score": sig.get("score"),
            "bias": sig.get("bias"),
            "ltp": sig.get("ltp"),
        })
