"""Manual payment-logging + member overview (added Oct 16 2026).

Stopgap until a real Razorpay webhook exists. There is currently NO automatic
link between a Razorpay payment and GreekNova access -- Manish checks Razorpay
himself and used to just insert a bare email into beta_users with no plan or
expiry recorded anywhere, which made every plan (monthly included) silently
grant lifetime access. This module adds a plan + expiry record for every
member so that's at least visible, and a one-form way to log a payment
instead of hand-writing SQL / using the Supabase table editor each time.

Deliberately NOT wired into services/access_gate.py yet: expires_at is
TRACKED, not ENFORCED. The gate still only checks "is this email in
beta_users" (see access_gate._is_member) -- nobody is cut off automatically.
Enforcing expiry is a separate, later decision (don't want to risk locking
out a paying member over a bad date right before launch).
"""
import os
from datetime import datetime, timedelta, timezone

PLANS = {
    "founding":   {"label": "Founding (lifetime-locked)", "amount": 3999, "months": 12},
    "monthly":    {"label": "Monthly",                    "amount": 499,  "months": 1},
    "quarterly":  {"label": "Quarterly",                  "amount": 1399, "months": 3},
    "semiannual": {"label": "Semiannual",                 "amount": 2699, "months": 6},
    "annual":     {"label": "Annual (regular)",            "amount": 4999, "months": 12},
}


def _admin_email() -> str:
    return os.getenv("ADMIN_EMAIL", "megha.jethmalani@gmail.com").strip().lower()


def verify_admin(authorization_header: str) -> bool:
    """True only if the bearer token belongs to the admin's own Supabase account.
    Independent of access_gate's GATE_MODE / ADMIN_GATE -- this checks a real
    identity, not just a shared secret, since it grants paid access."""
    import httpx
    token = ""
    if authorization_header and authorization_header.lower().startswith("bearer "):
        token = authorization_header[7:].strip()
    if not token:
        return False
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    apikey = os.getenv("SUPABASE_KEY", "")
    try:
        r = httpx.get(f"{url}/auth/v1/user",
                      headers={"apikey": apikey, "Authorization": f"Bearer {token}"},
                      timeout=5.0)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    email = (r.json().get("email") or "").strip().lower()
    return bool(email) and email == _admin_email()


def _months_from_now(start: datetime, months: int) -> datetime:
    # simple month add (day-of-month kept, overflow clamped) -- good enough
    # for 1/3/6/12 month plans, avoids pulling in a date library
    y, m, d = start.year, start.month, start.day
    m0 = m - 1 + months
    y += m0 // 12
    m = m0 % 12 + 1
    for dd in (d, d - 1, d - 2, d - 3):
        try:
            return start.replace(year=y, month=m, day=dd)
        except ValueError:
            continue
    return start.replace(year=y, month=m, day=1)


def log_payment(supabase, email: str, name: str, plan: str):
    email = (email or "").strip().lower()
    name = (name or "").strip()
    plan = (plan or "").strip().lower()
    if not email or "@" not in email:
        return {"error": "Please enter a valid email"}
    if plan not in PLANS:
        return {"error": f"Unknown plan '{plan}'. Must be one of: {', '.join(PLANS)}"}
    cfg = PLANS[plan]
    now = datetime.now(timezone.utc)

    existing = (supabase.from_("beta_users").select("email,expires_at")
                .eq("email", email).limit(1).execute()).data
    is_renewal = bool(existing)
    start = now
    if is_renewal and existing[0].get("expires_at"):
        try:
            prev_expiry = datetime.fromisoformat(existing[0]["expires_at"].replace("Z", "+00:00"))
            if prev_expiry > now:
                start = prev_expiry  # renewed before lapsing -- extend from old expiry, don't waste days
        except Exception:
            pass
    expires_at = _months_from_now(start, cfg["months"])

    row = {
        "email": email,
        "plan": plan,
        "amount_paid": cfg["amount"],
        "paid_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    if name:
        row["name"] = name
    elif not is_renewal:
        row["name"] = email.split("@")[0]

    if is_renewal:
        supabase.from_("beta_users").update(row).eq("email", email).execute()
    else:
        supabase.from_("beta_users").insert(row).execute()

    return {
        "status": "renewed" if is_renewal else "added",
        "email": email,
        "plan": plan,
        "plan_label": cfg["label"],
        "amount_paid": cfg["amount"],
        "expires_at": expires_at.isoformat(),
    }


def members_overview(supabase):
    res = (supabase.from_("beta_users")
           .select("email,name,plan,amount_paid,paid_at,expires_at,invited_at")
           .execute())
    rows = res.data or []
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        days_left = None
        status = "no_expiry_on_record"
        if r.get("expires_at"):
            try:
                exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
                days_left = (exp - now).days
                status = "lapsed" if days_left < 0 else ("due_soon" if days_left <= 7 else "active")
            except Exception:
                pass
        out.append({**r, "days_left": days_left, "status": status})
    # most urgent first: lapsed, then due_soon, then active, then unknown -- each sorted by days_left
    order = {"lapsed": 0, "due_soon": 1, "active": 2, "no_expiry_on_record": 3}
    out.sort(key=lambda r: (order.get(r["status"], 9), r["days_left"] if r["days_left"] is not None else 9999))
    return {"plans": PLANS, "members": out}
