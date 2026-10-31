import ast

path = "main.py"
with open(path) as f:
    c = f.read()

old = '''    status = _access_status(email)
    result = {"status": status, "trial_expires_date": None, "days_left": None}
    if status in ("trial_active", "trial_expired"):
        from utils.db import get_supabase
        row = get_supabase().from_("trial_users")\\
            .select("trial_expires_date")\\
            .eq("email", email).limit(1).execute()
        if row.data:
            exp = row.data[0]["trial_expires_date"]
            result["trial_expires_date"] = exp
            if status == "trial_active":
                from utils.market_calendar import today_ist
                from datetime import date
                result["days_left"] = (date.fromisoformat(exp) - today_ist()).days
    return result'''

new = '''    status = _access_status(email)
    result = {"status": status, "trial_expires_date": None, "days_left": None}
    if status in ("trial_active", "trial_expired"):
        from utils.db import get_supabase
        row = get_supabase().from_("trial_users")\\
            .select("trial_expires_date")\\
            .eq("email", email).limit(1).execute()
        if row.data:
            exp = row.data[0]["trial_expires_date"]
            result["trial_expires_date"] = exp
            if status == "trial_active":
                from utils.market_calendar import today_ist
                from datetime import date
                result["days_left"] = (date.fromisoformat(exp) - today_ist()).days
    elif status == "member":
        # FIX (Oct 24 2026): paid members had no visible countdown at all --
        # expires_at is tracked on beta_users (services/admin_members.py)
        # but never surfaced to the member themselves, only in the admin
        # panel. Informational only, same as the trial badge -- this does
        # NOT gate access; access_gate.py still only checks "is this email
        # in beta_users" (see its own module docstring). Reusing
        # trial_expires_date/days_left as the field names so the one
        # frontend badge component can render either case.
        from utils.db import get_supabase
        row = get_supabase().from_("beta_users")\\
            .select("expires_at")\\
            .eq("email", email).limit(1).execute()
        if row.data and row.data[0].get("expires_at"):
            from datetime import datetime, timezone
            exp_dt = datetime.fromisoformat(row.data[0]["expires_at"].replace("Z", "+00:00"))
            result["trial_expires_date"] = exp_dt.date().isoformat()
            result["days_left"] = (exp_dt - datetime.now(timezone.utc)).days
    return result'''

assert old in c, "me_trial_status body not found"
c = c.replace(old, new)
with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(c)
ast.parse(c)
print("OK")
