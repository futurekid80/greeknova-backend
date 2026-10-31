import ast

path = "main.py"
with open(path) as f:
    content = f.read()

old = '''    is_member = bool(beta_row)

    if is_member:
        try:
            if beta_row["email"] == kite_email:
                admin.from_("beta_users").upsert({"email": kite_email, "name": kite_user_name or kite_email}).execute()
            else:
                # Matched by client ID on a row whose primary-key email is
                # still their original payment email -- link this Kite
                # email to that row instead of touching the primary key.
                admin.from_("beta_users").update({"linked_kite_email": kite_email}).eq("email", beta_row["email"]).execute()
                print(f"[KiteLogin] linked {kite_email} (client {kite_user_id}) to paid row {beta_row['email']}")
        except Exception as e:
            print(f"[KiteLogin] beta_users upsert/link warning: {e}")'''

new = '''    is_member = bool(beta_row)

    if is_member:
        # Oct 23 2026: the existing "unrecognized login" Telegram ping below
        # only ever fires for new TRIAL signups -- a recognized PAID member's
        # first-ever login silently went through with no notification at
        # all. Mirror that alert here, gated on kite_login_log having no row
        # yet for this email (i.e. this really is their first login, not a
        # routine re-login), so Manish gets a signal for both trial and paid
        # without being pinged on every ordinary session.
        try:
            already_seen = admin.from_("kite_login_log").select("email").eq("email", kite_email).limit(1).execute().data
            if not already_seen:
                from services.alert_engine import send_telegram
                def _escape_md(s):
                    s = str(s or '-')
                    for ch in ('_', '*', '`', '['):
                        s = s.replace(ch, '\\\\' + ch)
                    return s
                send_telegram(
                    f"\\U0001F4B0 GreekNova: paid member's first login\\n"
                    f"Name: {_escape_md(kite_user_name)}\\n"
                    f"Kite email: {_escape_md(kite_email)}\\n"
                    f"Client ID: {_escape_md(kite_user_id)}\\n"
                    f"Matched beta_users row: {_escape_md(beta_row['email'])}"
                )
        except Exception as e:
            print(f"[KiteLogin] paid-login telegram notify skipped: {e}")
        try:
            if beta_row["email"] == kite_email:
                admin.from_("beta_users").upsert({"email": kite_email, "name": kite_user_name or kite_email}).execute()
            else:
                # Matched by client ID on a row whose primary-key email is
                # still their original payment email -- link this Kite
                # email to that row instead of touching the primary key.
                admin.from_("beta_users").update({"linked_kite_email": kite_email}).eq("email", beta_row["email"]).execute()
                print(f"[KiteLogin] linked {kite_email} (client {kite_user_id}) to paid row {beta_row['email']}")
        except Exception as e:
            print(f"[KiteLogin] beta_users upsert/link warning: {e}")'''

assert old in content, "is_member block not found"
content = content.replace(old, new)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
