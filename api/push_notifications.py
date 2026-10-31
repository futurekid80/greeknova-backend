"""
push_notifications.py
Web Push subscription management + sending.
Replaces the unreliable browser-side service-worker polling with real
server-triggered push — works even when the tab is closed or backgrounded.
"""
import os
import json
import time
from pywebpush import webpush, WebPushException
from services.strike_zones import build_strike_index_map, strikes_away as _strikes_away, in_zone as _in_zone

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CONTACT_EMAIL = os.environ.get("VAPID_CONTACT_EMAIL") or "mailto:support@greeknova.app"

# (Oct 12 2026): per-symbol strike-index-map cache for computing each
# alert's distance from ATM at persist time (Manish's ask, via a mentee's
# feedback: "bring the same ATM +/- filter to Alerts"). Short TTL (under
# the 5-min capture cadence) so multiple alerts firing for the same symbol
# within one push_checker.py pass (e.g. several OI_SPIKE strikes on NIFTY)
# reuse one lookup instead of re-querying oi_snapshots/cmp_prices per alert.
_strike_map_cache: dict = {}
_STRIKE_MAP_TTL = 240  # seconds


# Oct 31 2026 (Vinod): every alert carries the underlying's spot at the moment
# it fired. One short-TTL lookup per symbol (latest cmp_prices row) shared by
# all alerts in a pass; None when unknown -- never blocks an alert.
_spot_cache: dict = {}
_SPOT_TTL = 45  # seconds


def _get_spot(supabase, symbol: str):
    if not symbol:
        return None
    cached = _spot_cache.get(symbol)
    if cached and time.time() - cached[1] < _SPOT_TTL:
        return cached[0]
    try:
        r = supabase.from_("cmp_prices").select("cmp") \
            .eq("symbol", symbol).order("timestamp", desc=True).limit(1).execute()
        spot = float(r.data[0]["cmp"]) if r.data and r.data[0].get("cmp") else None
    except Exception as e:
        print(f"[Push] spot lookup failed for {symbol}: {e}")
        return None
    _spot_cache[symbol] = (spot, time.time())
    return spot


def _get_strikes_away(supabase, symbol: str, strike) -> int | None:
    """Looks up how many strikes `strike` sits from `symbol`'s ATM, using
    that symbol's most recent capture cycle. Returns None if symbol/strike
    is missing or no usable cmp/strike data exists yet (e.g. right after a
    fresh deploy before the first capture) -- same "unknown, exclude from
    any specific zone" contract as strike_zones.in_zone() expects."""
    if not symbol or strike is None:
        return None

    cached = _strike_map_cache.get(symbol)
    if cached and time.time() - cached[1] < _STRIKE_MAP_TTL:
        index_map = cached[0]
    else:
        try:
            ts_res = supabase.from_("oi_snapshots").select("timestamp") \
                .eq("symbol", symbol).order("timestamp", desc=True).limit(1).execute()
            if not ts_res.data:
                return None
            latest_ts = ts_res.data[0]["timestamp"]
            rows = supabase.from_("oi_snapshots").select("strike") \
                .eq("symbol", symbol).eq("timestamp", latest_ts).execute().data or []
            cmp_res = supabase.from_("cmp_prices").select("cmp") \
                .eq("symbol", symbol).order("timestamp", desc=True).limit(1).execute()
            cmp = float(cmp_res.data[0]["cmp"]) if cmp_res.data else 0
            index_map = build_strike_index_map(
                [{"symbol": symbol, "strike": r["strike"]} for r in rows if r.get("strike") is not None],
                {symbol: cmp},
            )
        except Exception as e:
            print(f"[Push] strikes_away lookup failed for {symbol}: {e}")
            return None
        _strike_map_cache[symbol] = (index_map, time.time())

    return _strikes_away(symbol, strike, index_map)


def save_subscription(supabase, sub_data: dict, spike_threshold: float = 10,
                       vol_threshold: float = None, enabled_signals: list = None,
                       strike_zone: str = None):
    """Save or update a device's push subscription.

    vol_threshold/enabled_signals are optional -- a true first-ever
    subscribe won't have them, and the column defaults (20 / null-means-
    all-signals) cover that case fine. But this also runs on every
    RE-subscribe (a disable->enable cycle unsubscribes the old push
    endpoint and gets issued a new one, so this upserts a NEW row, not an
    update of the old one) -- if the caller already knows the person's
    customized values, passing them here is what carries that
    customization forward instead of silently resetting it to defaults.
    See push_subscribe() in main.py.
    """
    endpoint = sub_data.get("endpoint")
    keys = sub_data.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return {"error": "Invalid subscription data"}

    row = {
        "endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "spike_threshold": spike_threshold,
        "enabled": True,
        "last_used_at": "now()",
    }
    if vol_threshold is not None:
        row["vol_threshold"] = vol_threshold
    if enabled_signals is not None:
        row["enabled_signals"] = [s for s in enabled_signals if s in VALID_SIGNAL_TYPES]
    if strike_zone is not None:
        row["strike_zone"] = strike_zone if strike_zone in STRIKE_ZONE_VALUES else "all"

    try:
        supabase.from_("push_subscriptions").upsert(row, on_conflict="endpoint").execute()
        return {"status": "subscribed"}
    except Exception as e:
        print(f"[Push] Subscribe failed: {e}")
        return {"error": str(e)}


def remove_subscription(supabase, endpoint: str):
    """Remove a device's push subscription (user disabled, or push failed permanently)."""
    try:
        supabase.from_("push_subscriptions").delete().eq("endpoint", endpoint).execute()
        return {"status": "unsubscribed"}
    except Exception as e:
        print(f"[Push] Unsubscribe failed: {e}")
        return {"error": str(e)}


# All signal types the alert system can ever generate. Used both to validate
# incoming preference updates and as the "everything enabled" default when a
# subscription has never customized (enabled_signals is NULL in the DB).
ALL_SIGNAL_TYPES = [
    "OI_SPIKE", "FRESH_BUILD",
    "LONG_BUILDUP", "SHORT_BUILDUP", "CALL_WRITING", "PUT_WRITING",
    "SHORT_COVERING", "LONG_UNWINDING", "BUYER_DOMINATED", "SELLER_DOMINATED",
    "FAR_OTM_ACTIVITY", "VOLUME_SURGE", "NEAR_STRIKE_UNWIND",
]

# Technical-level signals (Oct 2026) -- deliberately NOT folded into
# ALL_SIGNAL_TYPES above. Manish's explicit ask: these must be opt-in only,
# never auto-on just because a device has never customized its signal list.
# ALL_SIGNAL_TYPES staying NULL-means-"all of THIS list" (unchanged) is what
# keeps a never-customized device from getting these without asking --
# see the enabled_signals check in broadcast_alert() below.
OPT_IN_SIGNAL_TYPES = [
    "EMA_BULLISH_CROSS", "EMA_BEARISH_CROSS",
    "PDH_CROSS", "PDL_CROSS", "PWH_CROSS", "PWL_CROSS",
    "PCR_CROSSOVER",   # Oct 31 2026 (Pawan): Vol PCR crossing OI PCR on an index
]

# Every signal name that's legal to store in enabled_signals -- used for
# validating incoming preference saves. (ALL_SIGNAL_TYPES alone stays the
# "default enabled" set; this combined list is only for "is this name real".)
VALID_SIGNAL_TYPES = ALL_SIGNAL_TYPES + OPT_IN_SIGNAL_TYPES

# (Oct 16 2026, Manish's ask: "even 1 rupee option alert i get" -- a far
# OTM/ITM strike's premium is tiny, so OI%/Vol% thresholds alone don't cut
# that noise out; a strike-zone filter does.) Same ATM/±1/±2/±5/±10/All
# vocabulary as services/strike_zones.py ZONE_OPTIONS, just lowercased
# "all" to match the column default -- validated here the same way
# enabled_signals is above.
STRIKE_ZONE_VALUES = ["ATM", "1", "2", "5", "10", "all"]


def get_preferences(supabase, endpoint: str) -> dict:
    """
    Per-device alert type preferences for one subscription (identified by its
    own push endpoint URL, since that's the one thing the browser can supply
    without needing a login system). NULL enabled_signals means the device
    has never customized — everything is on by default.
    """
    try:
        res = supabase.from_("push_subscriptions").select("enabled_signals, spike_threshold, vol_threshold, strike_zone").eq("endpoint", endpoint).limit(1).execute()
        if not res.data:
            return {"error": "Subscription not found"}
        row = res.data[0]
        enabled = row.get("enabled_signals")
        return {
            "enabled_signals": enabled if enabled is not None else ALL_SIGNAL_TYPES,
            "spike_threshold": row.get("spike_threshold"),
            "vol_threshold": row.get("vol_threshold"),
            "strike_zone": row.get("strike_zone") or "all",
            "customized": enabled is not None,
        }
    except Exception as e:
        print(f"[Push] Get preferences failed: {e}")
        return {"error": str(e)}


def save_preferences(supabase, endpoint: str, enabled_signals: list, spike_threshold: float = None, vol_threshold: float = None, strike_zone: str = None) -> dict:
    """Save which alert types this specific device wants to receive, and at what OI%/Vol%/strike-zone thresholds."""
    valid = [s for s in enabled_signals if s in VALID_SIGNAL_TYPES]
    update = {"enabled_signals": valid}
    if spike_threshold is not None:
        update["spike_threshold"] = spike_threshold
    if vol_threshold is not None:
        update["vol_threshold"] = vol_threshold
    if strike_zone is not None:
        update["strike_zone"] = strike_zone if strike_zone in STRIKE_ZONE_VALUES else "all"
    try:
        supabase.from_("push_subscriptions").update(update).eq("endpoint", endpoint).execute()
        return {"status": "saved", "enabled_signals": valid}
    except Exception as e:
        print(f"[Push] Save preferences failed: {e}")
        return {"error": str(e)}


FAILURE_CLEANUP_THRESHOLD = 5  # consecutive non-404/410 failures before auto-removing


def _send_one(supabase, subscription: dict, payload: dict) -> bool:
    """Send push to a single subscription. Returns False if the subscription is dead."""
    from urllib.parse import urlparse
    import time as _time

    endpoint = subscription["endpoint"]
    parsed = urlparse(endpoint)
    aud = f"{parsed.scheme}://{parsed.netloc}"

    # Set every claim explicitly ourselves rather than relying on pywebpush's
    # auto-fill, which has been intermittently unreliable under rapid
    # sequential calls (each send needs its own fresh, correctly-scoped
    # 'aud' matching the push service's own origin, plus a real expiry).
    claims = {
        "sub": VAPID_CONTACT_EMAIL,
        "aud": aud,
        "exp": int(_time.time()) + 12 * 60 * 60,
    }

    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=claims,
        )
        # BUG FIX (Jul 24 2026): reset the failure streak on a genuine
        # success, so a subscription that recovers after a transient blip
        # doesn't carry a stale count toward the auto-cleanup threshold.
        if subscription.get("consecutive_failures"):
            try:
                supabase.from_("push_subscriptions")\
                    .update({"consecutive_failures": 0})\
                    .eq("id", subscription["id"]).execute()
            except Exception:
                pass
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return False
        # BUG FIX (Jul 24 2026): non-404/410 failures (e.g. a permanently
        # malformed subscription returning 400 Bad Request on every single
        # send) used to be logged and otherwise ignored forever -- one dead
        # subscription could fail on every alert indefinitely with no way
        # to notice or clean it up short of manually reading logs. Now
        # tracked per-subscription: after several CONSECUTIVE failures
        # (reset by any success in between, so a one-off blip is harmless)
        # the subscription is treated as dead and removed, same as 404/410.
        endpoint_tail = endpoint[-24:] if endpoint else "?"
        sub_id = subscription.get("id", "?")
        new_count = int(subscription.get("consecutive_failures") or 0) + 1
        try:
            supabase.from_("push_subscriptions")\
                .update({"consecutive_failures": new_count})\
                .eq("id", sub_id).execute()
        except Exception:
            pass
        print(f"[Push] Send failed ({status}): sub_id={sub_id} endpoint=...{endpoint_tail} "
              f"attempt={new_count}/{FAILURE_CLEANUP_THRESHOLD} -- {e}")
        if new_count >= FAILURE_CLEANUP_THRESHOLD:
            print(f"[Push] sub_id={sub_id} hit {FAILURE_CLEANUP_THRESHOLD} consecutive failures -- marking dead")
            return False
        return True
    except Exception as e:
        print(f"[Push] Send error: {e}")
        return True


def broadcast_alert(supabase, alert: dict):
    """
    Send an alert to every subscribed device whose threshold the alert clears.
    """
    # (Oct 16 2026) Computed once per alert, up front, and reused both for
    # alert_log (as before) and for the new per-subscriber strike-zone
    # filter below -- no reason to look it up twice. None for symbol-level
    # alerts with no strike (EMA/PDH/PDL/PWH/PWL crosses) -- those aren't
    # about any one option contract, so the strike-zone filter never
    # applies to them (see the "if alert.get(strike) is not None" check
    # this preserves from the original single call site).
    alert_strikes_away = (
        _get_strikes_away(supabase, alert.get("symbol"), alert.get("strike"))
        if alert.get("strike") is not None else None
    )

    # (Aug 26 2026): persist every alert to alert_log regardless of whether
    # push delivery succeeds -- the alert itself is real even if VAPID isn't
    # configured or every subscription happens to be dead. This is what
    # lets the in-app Alerts panel fetch a true history on load, instead of
    # depending entirely on the SW->open-tab postMessage relay (which only
    # reaches tabs open at the exact moment a push fires -- native OS
    # notifications aren't tab-dependent, but the in-app panel always was).
    spot = alert.get("spot")
    if spot is None:
        spot = _get_spot(supabase, alert.get("symbol"))
        if spot is not None:
            alert["spot"] = spot
    try:
        supabase.from_("alert_log").insert({
            "spot":         spot,
            "symbol":       alert.get("symbol"),
            "signal":       alert.get("signal"),
            "strike":       alert.get("strike"),
            "option_type":  alert.get("optionType"),
            "message":      alert.get("message"),
            "url":          alert.get("url"),
            "oi_pct":       alert.get("oiPct"),
            "vol_pct":      alert.get("volPct"),
            "ltp":          alert.get("ltp"),
            "score":        alert.get("score"),
            "bias":         alert.get("bias"),
            "direction":    alert.get("direction"),
            "strikes_away": alert_strikes_away,
        }).execute()
    except Exception as e:
        print(f"[Push] alert_log persist failed: {e}")

    if not VAPID_PRIVATE_KEY:
        print("[Push] VAPID_PRIVATE_KEY not set — skipping push")
        return

    try:
        subs_res = supabase.from_("push_subscriptions").select("*").eq("enabled", True).execute()
        subs = subs_res.data or []
    except Exception as e:
        print(f"[Push] Failed to load subscriptions: {e}")
        return

    if not subs:
        return

    # oiPct/volPct are only present on alerts that actually carry that data
    # (not every signal type has both) — None means "this alert has no OI/Vol
    # figure to threshold against", so that particular check is skipped.
    oi_pct = alert.get("oiPct")
    vol_pct = alert.get("volPct")
    oi_pct = abs(float(oi_pct)) if oi_pct is not None else None
    vol_pct = abs(float(vol_pct)) if vol_pct is not None else None
    dead_endpoints = []
    sent = 0

    for sub in subs:
        oi_threshold = float(sub.get("spike_threshold") or 10)
        vol_threshold = float(sub.get("vol_threshold") or 20)
        if oi_pct is not None and oi_pct < oi_threshold:
            continue
        if vol_pct is not None and vol_pct < vol_threshold:
            continue

        # NULL enabled_signals = device never customized = everything in
        # ALL_SIGNAL_TYPES is on by default. OPT_IN_SIGNAL_TYPES (EMA/PDH/
        # PDL/PWH/PWL crosses) are the exception -- those must be explicitly
        # present in enabled_signals to ever fire, NULL included, so a
        # device that's never touched its preferences doesn't suddenly
        # start getting a signal type it never asked for.
        enabled_signals = sub.get("enabled_signals")
        signal = alert.get("signal")
        if signal in OPT_IN_SIGNAL_TYPES:
            if not enabled_signals or signal not in enabled_signals:
                continue
        elif enabled_signals is not None and signal not in enabled_signals:
            continue

        # (Oct 16 2026, "even 1 rupee option alert i get"): a far OTM/ITM
        # strike's tiny premium isn't caught by the OI%/Vol% thresholds
        # above at all -- those are about the SIZE of the move, not where
        # the strike sits. Only applies to alerts that are actually about
        # one option contract -- a symbol-level alert (EMA/PDH/PDL/PWH/PWL
        # cross, alert.get("strike") is None) always passes regardless of
        # zone, since in_zone()'s own None handling is for a DIFFERENT case
        # (a real strike whose distance-from-ATM just couldn't be computed
        # that cycle), not "there is no strike to judge".
        if alert.get("strike") is not None and not _in_zone(alert_strikes_away, sub.get("strike_zone") or "all"):
            continue

        # Front-load the strike/OI%/Vol% into the title itself — the OS
        # truncates long notification bodies, so the most important numbers
        # need to survive truncation, not sit buried at the end of a long body.
        signal_label = alert.get("signal", "Alert").replace("_", " ").title()
        strike_part = f" {alert.get('strike')}{alert.get('optionType', '')}" if alert.get("strike") else ""
        stats = []
        if oi_pct is not None:
            stats.append(f"OI {'+' if (alert.get('oiPct') or 0) > 0 else ''}{alert.get('oiPct')}%")
        if vol_pct is not None:
            stats.append(f"Vol +{alert.get('volPct')}%")
        if alert.get("ltp") is not None:
            stats.append(f"LTP ₹{alert.get('ltp')}")
        if alert.get("spot") is not None:
            stats.append(f"Spot {alert.get('spot'):,.1f}")
        compact_body = " | ".join(stats) if stats else alert.get("message", "")

        payload = {
            "title": f"{signal_label} — {alert.get('symbol', '')}{strike_part}",
            "body": compact_body,
            "url": alert.get("url", "/alerts"),
            "alert": alert,
        }

        ok = _send_one(supabase, sub, payload)
        if ok:
            sent += 1
        else:
            dead_endpoints.append(sub["endpoint"])

    if dead_endpoints:
        try:
            supabase.from_("push_subscriptions").delete().in_("endpoint", dead_endpoints).execute()
            print(f"[Push] Cleaned up {len(dead_endpoints)} dead subscription(s)")
        except Exception as e:
            print(f"[Push] Cleanup failed: {e}")

    if sent:
        print(f"[Push] Sent '{alert.get('symbol')}' {alert.get('signal')} to {sent} device(s)")
