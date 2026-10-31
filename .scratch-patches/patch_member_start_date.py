import ast

path = "services/admin_members.py"
with open(path) as f:
    c = f.read()

old_imports = '''import os
from datetime import datetime, timedelta, timezone'''
new_imports = '''import os
from datetime import datetime, timedelta, timezone, date as _date
import pytz
from utils.market_calendar import is_trading_day, get_next_trading_day

_IST = pytz.timezone("Asia/Kolkata")


def _resolve_start_date(payment_date: str = None) -> datetime:
    """FIX (Oct 24 2026): new members' subscription start -- and what the
    admin/members page shows as their listed date -- used to always be
    `datetime.now()`, i.e. literally whatever moment Manish clicked "Add",
    not the day they actually paid. Logging a payment a day late silently
    shifted their whole year. Policy agreed with Manish: start date is the
    actual payment date, rolled forward to the next NSE trading day if the
    payment fell on a weekend/holiday -- they can't use the tool on a
    non-trading day anyway, so there's no reason to burn it off their year.
    `payment_date` is an optional 'YYYY-MM-DD' from the admin form (defaults
    to today if not given); returned as a tz-aware UTC datetime at midday
    IST, which avoids any date-rollover ambiguity from converting a bare
    IST calendar date to UTC."""
    if payment_date:
        d = _date.fromisoformat(payment_date)
    else:
        d = datetime.now(_IST).date()
    if not is_trading_day(d):
        d = get_next_trading_day(d)
    return _IST.localize(datetime.combine(d, datetime.min.time()).replace(hour=12)).astimezone(timezone.utc)'''

assert old_imports in c, "imports block not found"
c = c.replace(old_imports, new_imports)

old_sig = '''def log_payment(supabase, email: str, name: str, plan: str):
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
    }'''
new_sig = '''def log_payment(supabase, email: str, name: str, plan: str, payment_date: str = None):
    email = (email or "").strip().lower()
    name = (name or "").strip()
    plan = (plan or "").strip().lower()
    if not email or "@" not in email:
        return {"error": "Please enter a valid email"}
    if plan not in PLANS:
        return {"error": f"Unknown plan '{plan}'. Must be one of: {', '.join(PLANS)}"}
    cfg = PLANS[plan]
    now = datetime.now(timezone.utc)
    try:
        start = _resolve_start_date(payment_date)
    except Exception:
        start = now  # bad/unparseable payment_date -- fall back rather than 500

    existing = (supabase.from_("beta_users").select("email,expires_at")
                .eq("email", email).limit(1).execute()).data
    is_renewal = bool(existing)
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
        "paid_at": start.isoformat(),
        "expires_at": expires_at.isoformat(),
    }'''
assert old_sig in c, "log_payment def not found"
c = c.replace(old_sig, new_sig)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(c)
ast.parse(c)
print("OK")
