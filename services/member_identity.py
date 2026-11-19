"""
services/member_identity.py

Nov 19 2026 (Manish's call): record, once, which Zerodha account a paid member logs in with, and raise a single
Telegram alert if the SAME member later turns up with a DIFFERENT Zerodha ID. Nothing is blocked -- members do
switch accounts legitimately (Chandrasekkar, 19 Nov) -- the alert is so Manish can look.

Called from /auth/kite-login for a recognised paid member. Never raises: a failure here must not stop a login.
"""

_ALERTED: set = set()   # (member_email, what changed) already alerted in this process -- keeps one ping per change


def _norm(v):
    return (v or "").strip().upper()


def record_member_identity(admin, member_email: str, kite_email: str, kite_user_id: str, name: str = None, notify=None) -> dict:
    """Returns what happened, e.g. {"recorded_client_id": True} -- handy for logs and tests."""
    did: dict = {}
    try:
        rows = admin.from_("beta_users").select("email,name,kite_client_id,linked_kite_email") \
            .eq("email", member_email).limit(1).execute().data
        if not rows:
            return did
        row = rows[0]
        today_id = _norm(kite_user_id)
        rec_id = _norm(row.get("kite_client_id"))
        k_email = (kite_email or "").strip()
        primary = (row.get("email") or "").strip()
        linked = (row.get("linked_kite_email") or "").strip()
        label = row.get("name") or primary
        updates: dict = {}

        if today_id:
            if not rec_id:
                updates["kite_client_id"] = today_id
                did["recorded_client_id"] = today_id
            elif rec_id != today_id:
                did["different_client_id"] = today_id
                key = (primary, "id", today_id)
                if notify and key not in _ALERTED:
                    _ALERTED.add(key)
                    notify(
                        "⚠️ GreekNova: paid member logged in with a different Zerodha ID\n"
                        f"Member: {label} ({primary})\n"
                        f"Recorded Client ID: {rec_id}\n"
                        f"This login: {today_id} ({k_email or '-'})\n"
                        "Not blocked. Check for a second account of theirs, or sharing."
                    )

        if k_email and k_email.lower() != primary.lower():
            if not linked:
                updates["linked_kite_email"] = k_email
                did["recorded_linked_email"] = k_email
            elif linked.lower() != k_email.lower():
                did["different_email"] = k_email
                key = (primary, "email", k_email.lower())
                if notify and key not in _ALERTED:
                    _ALERTED.add(key)
                    notify(
                        "⚠️ GreekNova: paid member logged in with a different Zerodha email\n"
                        f"Member: {label} ({primary})\n"
                        f"Recorded Zerodha email: {linked}\n"
                        f"This login: {k_email} (Client ID {today_id or '-'})\n"
                        "Not blocked. Check whether they changed their Zerodha email or use another account."
                    )

        if updates:
            admin.from_("beta_users").update(updates).eq("email", primary).execute()
    except Exception as e:
        print(f"[MemberIdentity] skipped: {e}")
    return did
