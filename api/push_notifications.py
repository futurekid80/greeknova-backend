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


def _send_one(subscription: dict, payload: dict) -> bool:
    """Send push to a single subscription. Returns False if the subscription is dead."""
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CONTACT_EMAIL},
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return False
        print(f"[Push] Send failed ({status}): {e}")
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

    oi_pct = abs(float(alert.get("oiPct") or 0))
    dead_endpoints = []
    sent = 0

    for sub in subs:
        threshold = float(sub.get("spike_threshold") or 10)
        if alert.get("signal") == "OI_SPIKE" and oi_pct < threshold:
            continue

        payload = {
            "title": f"{alert.get('signal', 'Alert').replace('_', ' ').title()} — {alert.get('symbol', '')}",
            "body": alert.get("message", ""),
            "url": alert.get("url", "/alerts"),
            "alert": alert,
        }

        ok = _send_one(sub, payload)
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
