import ast

# --- alert_engine.py: give send_telegram a markdown on/off switch ---
path1 = "services/alert_engine.py"
with open(path1) as f:
    c1 = f.read()

old1 = '''def send_telegram(text: str) -> bool:
    token = _get_bot_token()
    if not token:
        print("[ALERTS] TELEGRAM_BOT_TOKEN env var is empty/not set -- cannot send")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id":    PERSONAL_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown"
        }, timeout=10)'''
new1 = '''def send_telegram(text: str, markdown: bool = True) -> bool:
    # FIX (Oct 24 2026): Telegram's legacy "Markdown" parse_mode has no
    # backslash-escaping at all (only MarkdownV2 supports \\-escapes) -- the
    # Oct 19 fix for the kite-login pings assumed it did and backslash-
    # escaped _ * ` [ in dynamic name/email values, which doesn't work:
    # Telegram still sees the raw _ / * / [ as a real formatting delimiter,
    # the literal backslash just rides along as ordinary text, and an odd
    # count anywhere in the message still 400s with "can't find end of the
    # entity" -- silently dropping the whole alert. There's no reliable way
    # to escape free-form user-supplied text (a real name or email) for
    # legacy Markdown, so callers with unpredictable dynamic content should
    # pass markdown=False instead of trying to escape it. The existing
    # trading-signal alerts below all only wrap a plain stock symbol in
    # *asterisks* (always balanced, never user-free-text), so they keep
    # markdown=True and are unaffected.
    token = _get_bot_token()
    if not token:
        print("[ALERTS] TELEGRAM_BOT_TOKEN env var is empty/not set -- cannot send")
        return False
    try:
        payload = {
            "chat_id": PERSONAL_CHAT_ID,
            "text":    text,
        }
        if markdown:
            payload["parse_mode"] = "Markdown"
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)'''

assert old1 in c1, "send_telegram def not found"
c1 = c1.replace(old1, new1)
with open(path1.replace(".py", "_new.py"), "w") as f:
    f.write(c1)
ast.parse(c1)
print("alert_engine.py OK")

# --- main.py: both kite-login Telegram pings -> plain text, no more _escape_md ---
path2 = "main.py"
with open(path2) as f:
    c2 = f.read()

old2 = '''            already_seen = admin.from_("kite_login_log").select("email").eq("email", kite_email).limit(1).execute().data
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
                )'''
new2 = '''            already_seen = admin.from_("kite_login_log").select("email").eq("email", kite_email).limit(1).execute().data
            if not already_seen:
                from services.alert_engine import send_telegram
                # FIX (Oct 24 2026): plain text, not Markdown -- see the
                # note on send_telegram() in alert_engine.py. A real name
                # or email is free-form text Claude/Manish don't control,
                # so there's no safe way to Markdown-escape it; this alert
                # doesn't need bold formatting anyway.
                send_telegram(
                    f"\\U0001F4B0 GreekNova: paid member's first login\\n"
                    f"Name: {kite_user_name or '-'}\\n"
                    f"Kite email: {kite_email or '-'}\\n"
                    f"Client ID: {kite_user_id or '-'}\\n"
                    f"Matched beta_users row: {beta_row['email']}",
                    markdown=False,
                )'''
assert old2 in c2, "paid-login telegram block not found"
c2 = c2.replace(old2, new2)

old3 = '''            def _escape_md(s):
                s = str(s or '-')
                for ch in ('_', '*', '`', '['):
                    s = s.replace(ch, '\\\\' + ch)
                return s
            send_telegram(
                f"\\U0001F514 GreekNova: unrecognized Zerodha login\\n"
                f"Name: {_escape_md(kite_user_name)}\\n"
                f"Kite email: {_escape_md(kite_email)}\\n"
                f"Client ID: {_escape_md(kite_user_id)}\\n"
                f"Started on a 7-day trial. If this is a paid customer, "
                f"add their Client ID to their beta_users row to upgrade them."
            )'''
new3 = '''            # FIX (Oct 24 2026): plain text -- see send_telegram() note in
            # alert_engine.py. The Oct 19 backslash-escaping fix didn't
            # actually work (legacy Telegram Markdown has no \\-escapes),
            # so this kept silently 400-ing on names/emails with an odd
            # number of _ * ` [ characters.
            send_telegram(
                f"\\U0001F514 GreekNova: unrecognized Zerodha login\\n"
                f"Name: {kite_user_name or '-'}\\n"
                f"Kite email: {kite_email or '-'}\\n"
                f"Client ID: {kite_user_id or '-'}\\n"
                f"Started on a 7-day trial. If this is a paid customer, "
                f"add their Client ID to their beta_users row to upgrade them.",
                markdown=False,
            )'''
assert old3 in c2, "unrecognized-login telegram block not found"
c2 = c2.replace(old3, new3)

with open(path2.replace(".py", "_new.py"), "w") as f:
    f.write(c2)
ast.parse(c2)
print("main.py OK")
