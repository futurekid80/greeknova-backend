"""
push_notifications.py
Web Push subscription management + sending.
Replaces the unreliable browser-side service-worker polling with real
server-triggered push — works even when the tab is closed or backgrounded.
"""
import os
import json
from pywebpush import webpush, WebPushException

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CONTACT_EMAIL = os.environ.get("VAPID_CONTACT_EMAIL") or "mailto:support@greeknova.app"


def save_subscription(supabase, sub_data: dict, spike_threshold: float = 10):
    """Save or update a device's push subscription."""
    endpoint = sub_data.get("endpoint")
    keys = sub_data.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return {"error": "Invalid subscription data"}

    try:
        supabase.from_("push_subscriptions").upsert({
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "spike_threshold": spike_threshold,
            "enabled": True,
            "last_used_at": "now()",
        }, on_conflict="endpoint").execute()
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
    "FAR_OTM_ACTIVITY", "VOLUME_SURGE",
]


def get_preferences(supabase, endpoint: str) -> dict:
    """
    Per-device alert type preferences for one subscription (identified by its
    own push endpoint URL, since that's the one thing the browser can supply
    without needing a login system). NULL enabled_signals means the device
    has never customized — everything is on by default.
    """
    try:
        res = supabase.from_("push_subscriptions").select("enabled_signals, spike_threshold, vol_threshold").eq("endpoint", endpoint).limit(1).execute()
        if not res.data:
            return {"error": "Subscription not found"}
        row = res.data[0]
        enabled = row.get("enabled_signals")
        return {
            "enabled_signals": enabled if enabled is not None else ALL_SIGNAL_TYPES,
            "spike_threshold": row.get("spike_threshold"),
            "vol_threshold": row.get("vol_threshold"),
            "customized": enabled is not None,
        }
    except Exception as e:
        print(f"[Push] Get preferences failed: {e}")
        return {"error": str(e)}


def save_preferences(supabase, endpoint: str, enabled_signals: list, spike_threshold: float = None, vol_threshold: float = None) -> dict:
    """Save which alert types this specific device wants to receive, and at what OI%/Vol% thresholds."""
    valid = [s for s in enabled_signals if s in ALL_SIGNAL_TYPES]
    update = {"enabled_signals": valid}
    if spike_threshold is not None:
        update["spike_threshold"] = spike_threshold
    if vol_threshold is not None:
        update["vol_threshold"] = vol_threshold
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

        # NULL enabled_signals = device never customized = everything on.
        # Once a device saves preferences, enabled_signals becomes a real
        # list and only those exact signal types get sent to it.
        enabled_signals = sub.get("enabled_signals")
        if enabled_signals is not None and alert.get("signal") not in enabled_signals:
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
