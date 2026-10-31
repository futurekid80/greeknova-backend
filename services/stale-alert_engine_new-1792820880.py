"""
services/alert_engine.py

GreekNova Alert Engine — runs after every OI capture cycle.

Three alert types:
1. HIGH CONV — FUT signal + Options Confirmation aligned, persistence >= 10 snaps
2. Near-ATM writing — UOA put/call writing within 2% of CMP, score >= 4
3. Intraday signals — fires at persistence milestones: 50%, 75%, 90%

Delivers: Telegram only
Deduplicates: once per symbol per alert type per milestone per day
"""

import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Telegram config ───────────────────────────────────────────────────────────
# BUG FIX (Aug 5 2026): this hardcoded token was the ORIGINAL compromised
# bot's token (numeric ID 8659302604) -- an "Aug 3" fix to move this to
# TELEGRAM_BOT_TOKEN env var apparently never actually deployed (several
# GitHub auth failures that day likely obscured which push actually
# landed), so the real alert engine has been silently calling Telegram
# with a long-dead token this whole time -- 401 on every single alert --
# while /test-telegram (added afterward) correctly read the current valid
# token fresh from the env var and looked completely fine. Re-applying
# properly this time, and reading fresh on every call rather than baking
# it into a module-level constant, so a future env var update can't get
# silently stuck again regardless of container startup/restart timing.
def _get_bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")

PERSONAL_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5513733966")

# ── Persistence milestones to alert at ───────────────────────────────────────
PERSISTENCE_MILESTONES = [50, 75, 90]

# ── Deduplication — reset daily ───────────────────────────────────────────────
_sent_alerts: set = set()
_sent_date:   str = ""

# ── Candidate-alert state (Oct 14 2026) — see block below ────────────────────
_regime_state: dict = {}       # symbol -> last-seen GEX regime
_max_pain_state: dict = {}     # symbol -> last-alerted max_pain value
_elite_seen_today: set = set() # symbols already alerted as newly-Elite today

IST = timezone(timedelta(hours=5, minutes=30))


def _reset_if_new_day():
    global _sent_alerts, _sent_date, _regime_state, _max_pain_state, _elite_seen_today
    today = datetime.now(IST).strftime('%Y-%m-%d')
    if today != _sent_date:
        _sent_alerts = set()
        _sent_date   = today
        _regime_state = {}
        _max_pain_state = {}
        _elite_seen_today = set()


def _get_milestone(persistence_pct: int) -> Optional[int]:
    """Return the highest milestone crossed, or None if none crossed yet."""
    crossed = [m for m in PERSISTENCE_MILESTONES if persistence_pct >= m]
    return max(crossed) if crossed else None


def send_telegram(text: str, markdown: bool = True) -> bool:
    # FIX (Oct 24 2026): Telegram's legacy "Markdown" parse_mode has no
    # backslash-escaping at all (only MarkdownV2 supports \-escapes) -- the
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
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
        # BUG FIX (Aug 3 2026): this only ever logged on network-level
        # exceptions (timeout, connection error) -- a non-200 response from
        # Telegram's own API (e.g. 401 for a bad/revoked token, 400 for a
        # bad chat_id) fell through silently with zero logging, returning
        # False with no way to diagnose why. Discovered while debugging
        # the new bot token right after the old one was compromised.
        if r.status_code != 200:
            print(f"[ALERTS] Telegram API error {r.status_code}: {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"[ALERTS] Telegram send failed: {e}")
        return False


def is_market_hours() -> bool:
    now   = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    total = now.hour * 60 + now.minute
    # BUG FIX (Aug 3 2026): was 15:30 — CAS goes live today, F&O trades
    # until 15:40 now. This gate controls whether alerts get generated
    # at all in that window.
    return (9 * 60 + 15) <= total <= (15 * 60 + 40)


def run_alert_check():
    if not is_market_hours():
        return

    print("[ALERTS] Running alert check...")
    _reset_if_new_day()

    try:
        _check_high_conv_signals()
        _check_near_atm_writing()
        _check_intraday_signals()
    except Exception as e:
        print(f"[ALERTS] Alert check error: {e}")

    # ── Candidate alert types (Oct 14 2026) ───────────────────────────────
    # Manish wants to see these live before deciding which become permanent
    # / public-facing. Each is wrapped in its own try/except so one bad
    # check can't take down the others or the three proven alerts above.
    # All go to the same PERSONAL_CHAT_ID as everything else in this file —
    # nothing here is public yet.
    try:
        _check_near_strike_unwind()
    except Exception as e:
        print(f"[ALERTS] Near-strike unwind (candidate) error: {e}")
    try:
        _check_gex_regime_flip()
    except Exception as e:
        print(f"[ALERTS] GEX regime flip (candidate) error: {e}")
    try:
        _check_max_pain_shift()
    except Exception as e:
        print(f"[ALERTS] Max pain shift (candidate) error: {e}")
    try:
        _check_elite_tier_entry()
    except Exception as e:
        print(f"[ALERTS] Elite tier entry (candidate) error: {e}")


# ── Alert 1: HIGH CONV ────────────────────────────────────────────────────────
def _check_high_conv_signals():
    """
    Fire when FUT signal + Options Confirmation both align,
    persistence >= 10 snapshots. Fires at milestones: 50%, 75%, 90%.
    """
    try:
        from api.signal_log import get_signal_log
        data    = get_signal_log()
        signals = data.get("signals", [])

        for sig in signals:
            sym             = sig["symbol"]
            persistence     = sig.get("persistence", 0)
            persistence_pct = sig.get("persistence_pct", 0)
            confirms        = sig.get("options_confirms")
            signal_type     = sig.get("signal_type", "")
            label           = sig.get("label", "")
            oi_chg          = sig.get("oi_chg_pct", 0)
            price_chg       = sig.get("price_chg_pct", 0)
            cmp             = sig.get("cmp", 0)
            cpr_pos         = sig.get("cpr_position", "")
            opt_sig         = sig.get("options_signal")

            if not confirms or persistence < 10:
                continue

            milestone = _get_milestone(persistence_pct)
            if not milestone:
                continue

            alert_key = f"highconv_{sym}_{signal_type}_{milestone}"
            if alert_key in _sent_alerts:
                continue

            bias_icon = "🟢" if sig.get("bias") == "BULLISH" else "🔴"
            opt_line  = ""
            if opt_sig:
                opt_line = f"Options: {opt_sig.get('label','')} · {opt_sig.get('strike','')} {opt_sig.get('option_type','')} · Score {opt_sig.get('score','')}/5\n"

            milestone_tag = f"🏁 {milestone}% persistence\n"

            text = (
                f"🎯 *HIGH CONV Signal* {bias_icon}\n"
                f"*{sym}* · ₹{cmp}\n"
                f"FUT: {label} · OI {oi_chg:+.1f}% · Price {price_chg:+.2f}%\n"
                f"{opt_line}"
                f"CPR: {cpr_pos.replace('_',' ').title()} · Persistence: {persistence} snaps\n"
                f"{milestone_tag}"
                f"_GreekNova · Informational only_"
            )

            if send_telegram(text):
                _sent_alerts.add(alert_key)
                print(f"[ALERTS] HIGH CONV sent: {sym} @ {milestone}% milestone")

    except Exception as e:
        print(f"[ALERTS] High conv check error: {e}")


# ── Alert 2: Near-ATM Writing ─────────────────────────────────────────────────
def _check_near_atm_writing():
    """
    Fire when put/call writing appears within 2% of CMP, score >= 4.
    Fires at milestones: 50%, 75%, 90% persistence.
    """
    try:
        from api.uoa import get_uoa
        from utils.db import get_supabase

        today    = datetime.now(IST).strftime('%Y-%m-%d')
        uoa_data = get_uoa(date=today)
        signals  = uoa_data.get("signals", [])

        supabase = get_supabase()
        cmp_rows = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .limit(500).execute()

        cmp_map: dict = {}
        seen = set()
        for r in (cmp_rows.data or []):
            if r["symbol"] not in seen:
                cmp_map[r["symbol"]] = float(r["cmp"])
                seen.add(r["symbol"])

        for sig in signals:
            signal_type = sig.get("signal_type", "")
            if signal_type not in ["PUT_WRITING", "CALL_WRITING"]:
                continue
            if sig.get("score", 0) < 4:
                continue

            sym             = sig["symbol"]
            strike          = float(sig.get("strike", 0))
            opt_type        = sig.get("option_type", "")
            score           = sig.get("score", 0)
            ltp             = sig.get("ltp", 0)
            oi_chg          = sig.get("oi_chg_30min", 0)
            persist_pct     = sig.get("persistence_pct", 0)
            cmp             = cmp_map.get(sym, 0)

            if cmp <= 0 or strike <= 0:
                continue

            distance_pct = abs(strike - cmp) / cmp * 100
            if distance_pct > 2.0:
                continue

            milestone = _get_milestone(persist_pct)
            if not milestone:
                continue

            alert_key = f"nearatm_{sym}_{strike}_{opt_type}_{milestone}"
            if alert_key in _sent_alerts:
                continue

            bias_icon    = "🟢" if signal_type == "PUT_WRITING" else "🔴"
            signal_label = "Put Writing" if signal_type == "PUT_WRITING" else "Call Writing"
            direction    = "defending support" if signal_type == "PUT_WRITING" else "capping upside"

            text = (
                f"📍 *Near-ATM {signal_label}* {bias_icon}\n"
                f"*{sym}* · ₹{cmp:.1f} CMP\n"
                f"{strike} {opt_type} · {distance_pct:.1f}% from CMP · Score {score}/5\n"
                f"OI 30m: {oi_chg:+.1f}% · LTP: ₹{ltp} · Persist: {persist_pct}%\n"
                f"Writer {direction} · 🏁 {milestone}% milestone\n"
                f"_GreekNova · Informational only_"
            )

            if send_telegram(text):
                _sent_alerts.add(alert_key)
                print(f"[ALERTS] Near-ATM {signal_label} sent: {sym} {strike} {opt_type} @ {milestone}%")

    except Exception as e:
        print(f"[ALERTS] Near-ATM check error: {e}")


# ── Alert 3: Intraday Signal ──────────────────────────────────────────────────
def _check_intraday_signals():
    """
    Fire when a new intraday FUT signal appears with:
    - vol_surge = True
    - OI change >= 5%
    Fires at persistence milestones: 50%, 75%, 90%
    Each milestone fires once per symbol per signal_type per day.
    """
    try:
        from api.signal_log import get_signal_log
        data    = get_signal_log()
        signals = data.get("signals", [])

        for sig in signals:
            sym             = sig["symbol"]
            persistence     = sig.get("persistence", 0)
            persistence_pct = sig.get("persistence_pct", 0)
            signal_type     = sig.get("signal_type", "")
            label           = sig.get("label", "")
            oi_chg          = sig.get("oi_chg_pct", 0)
            price_chg       = sig.get("price_chg_pct", 0)
            vol_chg         = sig.get("vol_chg_pct", 0)
            vol_surge       = sig.get("vol_surge", False)
            cmp             = sig.get("cmp", 0)
            cpr_pos         = sig.get("cpr_position", "")
            first_seen      = sig.get("first_seen", "")
            confirms        = sig.get("options_confirms")
            ce_wall         = sig.get("ce_wall")
            pe_wall         = sig.get("pe_wall")
            trade_range_pct = sig.get("trade_range_pct")
            range_label     = sig.get("range_label", "")

            if not vol_surge or abs(oi_chg) < 5.0:
                continue

            milestone = _get_milestone(persistence_pct)
            if not milestone:
                continue

            alert_key = f"intraday_{sym}_{signal_type}_{milestone}"
            if alert_key in _sent_alerts:
                continue

            bias_icon = "🟢" if sig.get("bias") == "BULLISH" else "🔴"
            signal_icons = {
                "LONG_BUILDUP":   "🐂",
                "SHORT_BUILDUP":  "🐻",
                "SHORT_COVERING": "🔄",
                "LONG_UNWINDING": "⚠️",
            }
            sig_icon = signal_icons.get(signal_type, "📊")

            cpr_line = ""
            if cpr_pos:
                virgin = "🔵 Virgin · " if sig.get("cpr_is_virgin") else ""
                cpr_line = f"CPR: {virgin}{cpr_pos}\n"

            walls_line = ""
            if ce_wall and pe_wall:
                walls_line = f"📈 CE ₹{ce_wall:,.0f} · 📉 PE ₹{pe_wall:,.0f}"
                if trade_range_pct:
                    walls_line += f" · {trade_range_pct}% {range_label}"
                walls_line += "\n"

            conf_line = ""
            if confirms is True:
                conf_line = "✅ Options Confirms\n"
            elif confirms is False:
                conf_line = "⚠️ Options Contradicts\n"

            text = (
                f"{sig_icon} *GreekNova Signal* {bias_icon} 🏁 {milestone}%\n"
                f"*{sym}* · ₹{cmp:,.1f}\n"
                f"{label} · OI {oi_chg:+.1f}% · Price {price_chg:+.2f}%\n"
                f"Vol: {vol_chg:+.0f}% ⚡ · Since: {first_seen}\n"
                f"{cpr_line}"
                f"{walls_line}"
                f"{conf_line}"
                f"_GreekNova · Informational only · Not investment advice_"
            )

            if send_telegram(text):
                _sent_alerts.add(alert_key)
                print(f"[ALERTS] Intraday signal sent: {sym} {signal_type} @ {milestone}% milestone")

    except Exception as e:
        print(f"[ALERTS] Intraday signal check error: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Candidate alert types (Oct 14 2026) — personal-only, for live evaluation.
# Every message here states what changed (a number, a tier, a rank), never
# what to do about it — same posture as the three alerts above, mirroring
# the OI terminology already used across the app rather than
# bullish/bearish-as-opinion language.
# ═══════════════════════════════════════════════════════════════════════════

NSU_NEAR_STRIKE_PCT = 4.0
NSU_UNWIND_DROP_PCT = 40.0
NSU_BASELINE_LOOKBACK_MIN = 20
NSU_MIN_OI = 5000


def _check_near_strike_unwind():
    """A strike close to spot (CE = resistance, PE = support) whose OI has
    collapsed over the last ~20 minutes — the same condition already
    detected server-side for web push (services/push_checker.py), computed
    independently here so a bug in this candidate check can never affect
    the live push-notification path real users already receive."""
    from utils.db import get_supabase
    supabase = get_supabase()

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    latest_raw = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots") \
            .select("symbol,strike,option_type,expiry,oi,last_price,timestamp") \
            .gte("timestamp", (now - timedelta(minutes=7)).isoformat()) \
            .lte("timestamp", now.isoformat()) \
            .in_("option_type", ["CE", "PE"]) \
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        latest_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break
    if not latest_raw:
        return

    latest_by_key = {}
    ts_new = None
    for r in latest_raw:
        key = (r["symbol"], r["strike"], r["option_type"], r["expiry"])
        if key not in latest_by_key or r["timestamp"] > latest_by_key[key]["timestamp"]:
            latest_by_key[key] = r
        if ts_new is None or r["timestamp"] > ts_new:
            ts_new = r["timestamp"]

    ts_new_dt = datetime.fromisoformat(ts_new.replace("+00:00", "")).replace(tzinfo=timezone.utc)
    baseline_center = ts_new_dt - timedelta(minutes=NSU_BASELINE_LOOKBACK_MIN)
    baseline_raw = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots") \
            .select("symbol,strike,option_type,expiry,oi,timestamp") \
            .gte("timestamp", (baseline_center - timedelta(minutes=4)).isoformat()) \
            .lte("timestamp", (baseline_center + timedelta(minutes=4)).isoformat()) \
            .in_("option_type", ["CE", "PE"]) \
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        baseline_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    def _ts(row):
        return datetime.fromisoformat(row["timestamp"].replace("+00:00", "")).replace(tzinfo=timezone.utc)

    baseline_by_key = {}
    for r in baseline_raw:
        key = (r["symbol"], r["strike"], r["option_type"], r["expiry"])
        cur = baseline_by_key.get(key)
        if cur is None or abs((_ts(r) - baseline_center).total_seconds()) < abs((_ts(cur) - baseline_center).total_seconds()):
            baseline_by_key[key] = r
    if not baseline_by_key:
        return

    cmp_res = supabase.from_("cmp_prices").select("symbol,cmp,timestamp") \
        .gte("timestamp", (now - timedelta(minutes=10)).isoformat()) \
        .order("timestamp", desc=True).limit(2000).execute()
    cmp_map, seen_cmp = {}, set()
    for c in (cmp_res.data or []):
        if c["symbol"] not in seen_cmp:
            try:
                cmp_map[c["symbol"]] = float(c["cmp"])
            except (TypeError, ValueError):
                continue
            seen_cmp.add(c["symbol"])

    nearest_expiry_map = {}
    for r in latest_raw:
        sym, exp = r["symbol"], r.get("expiry")
        if not exp or exp < today:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    for key, latest in latest_by_key.items():
        sym, strike, opt, expiry = key
        if nearest_expiry_map.get(sym) != expiry:
            continue
        spot = cmp_map.get(sym)
        if not spot or spot <= 0:
            continue
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        if abs(strike_f - spot) / spot > NSU_NEAR_STRIKE_PCT / 100:
            continue

        base = baseline_by_key.get(key)
        if not base:
            continue
        oi_now = latest.get("oi") or 0
        oi_base = base.get("oi") or 0
        if oi_base < NSU_MIN_OI:
            continue
        chg_pct = (oi_now - oi_base) / oi_base * 100
        if chg_pct > -NSU_UNWIND_DROP_PCT:
            continue

        alert_key = f"tg_nsu_{sym}_{strike}_{opt}_{ts_new}"
        if alert_key in _sent_alerts:
            continue

        pct_from_spot = abs(round((strike_f - spot) / spot * 100, 2))
        side = "resistance" if opt == "CE" else "support"
        text = (
            f"⚡ *Near-Strike Unwind*\n"
            f"*{sym}* {int(strike_f)} {opt} · {side} breaking ({pct_from_spot:.1f}% from spot)\n"
            f"OI {chg_pct:.1f}% in ~{NSU_BASELINE_LOOKBACK_MIN}min · now {int(oi_now):,} · LTP ₹{latest.get('last_price')}\n"
            f"_GreekNova · Informational only · candidate alert_"
        )
        if send_telegram(text):
            _sent_alerts.add(alert_key)
            print(f"[ALERTS] (candidate) Near-strike unwind sent: {sym} {strike} {opt}")


def _check_gex_regime_flip():
    """Fires when a symbol's near-spot gamma regime (SHORT_GAMMA /
    LONG_GAMMA) changes from what it was last cycle. Uses the already
    background-refreshed gamma_exposure cache — cheap, no extra compute."""
    from services.gamma_exposure import get_gamma_exposure

    data = get_gamma_exposure()
    watchlist = data.get("watchlist", []) if isinstance(data, dict) else []

    for row in watchlist:
        sym = row.get("symbol")
        regime = row.get("regime")
        if not sym or not regime:
            continue

        prev = _regime_state.get(sym)
        _regime_state[sym] = regime
        if prev is None or prev == regime:
            continue  # first sighting today (baseline) or unchanged — no alert

        flip_point = row.get("flip_point") or row.get("local_flip_point")
        spot = row.get("cmp")
        alert_key = f"tg_regime_{sym}_{regime}_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')}"
        if alert_key in _sent_alerts:
            continue

        regime_label = {"SHORT_GAMMA": "negative (amplifying)", "LONG_GAMMA": "positive (dampening)"}.get(regime, regime)
        flip_line = f"Flip point: {flip_point:,.0f}\n" if flip_point else ""
        text = (
            f"🔁 *Gamma Regime Flip*\n"
            f"*{sym}* · spot ₹{spot:,.1f}\n" if spot else f"🔁 *Gamma Regime Flip*\n*{sym}*\n"
        )
        text += (
            f"Regime changed: {regime_label}\n"
            f"{flip_line}"
            f"_GreekNova · Informational only · candidate alert_"
        )
        if send_telegram(text):
            _sent_alerts.add(alert_key)
            print(f"[ALERTS] (candidate) Regime flip sent: {sym} -> {regime}")


MAX_PAIN_MIN_SHIFT_PCT = 1.0  # ignore sub-1%-of-spot noise, same scale as other debounces here


def _check_max_pain_shift():
    """Fires when nearest-expiry max pain moves by >= MAX_PAIN_MIN_SHIFT_PCT
    of spot since the last time this check alerted for that symbol."""
    from api.max_pain import get_max_pain_all

    data = get_max_pain_all()
    symbols = data.get("symbols", []) if isinstance(data, dict) else []

    for row in symbols:
        sym = row.get("symbol")
        mp = row.get("max_pain")
        cmp = row.get("cmp")
        if not sym or not mp or not cmp:
            continue

        prev_mp = _max_pain_state.get(sym)
        if prev_mp is None:
            _max_pain_state[sym] = mp  # baseline only, no alert on first sighting
            continue
        if prev_mp == mp:
            continue

        shift_pct = abs(mp - prev_mp) / cmp * 100
        if shift_pct < MAX_PAIN_MIN_SHIFT_PCT:
            continue

        alert_key = f"tg_maxpain_{sym}_{mp}_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')}"
        if alert_key in _sent_alerts:
            continue

        text = (
            f"🎯 *Max Pain Shift*\n"
            f"*{sym}* {row.get('expiry','')} · spot ₹{cmp:,.1f}\n"
            f"Max pain moved {int(prev_mp):,} → {int(mp):,}\n"
            f"_GreekNova · Informational only · candidate alert_"
        )
        if send_telegram(text):
            _max_pain_state[sym] = mp
            _sent_alerts.add(alert_key)
            print(f"[ALERTS] (candidate) Max pain shift sent: {sym} {prev_mp} -> {mp}")


def _check_elite_tier_entry():
    """Fires once per symbol per day, the first cycle it's seen in Elite
    tier of Positional Intelligence's Stealth Buildup."""
    from api.positional_intelligence import get_positional_intelligence

    data = get_positional_intelligence()
    stealth = data.get("stealth_buildup", []) if isinstance(data, dict) else []

    for row in stealth:
        if row.get("tier") != "ELITE":
            continue
        sym = row.get("symbol")
        if not sym or sym in _elite_seen_today:
            continue

        text = (
            f"🏆 *Elite Tier Entry*\n"
            f"*{sym}* · ₹{row.get('cmp')}\n"
            f"Top-3 FUT OI rank · OI {row.get('today_oi_chg', 0):+.1f}% · Price {row.get('price_chg', 0):+.2f}%\n"
            f"_GreekNova · Informational only · candidate alert_"
        )
        if send_telegram(text):
            _elite_seen_today.add(sym)
            print(f"[ALERTS] (candidate) Elite tier entry sent: {sym}")


# ── Scheduled digests (candidate) — called directly from main.py's
# scheduler at fixed IST times, not from run_alert_check() ──────────────────

def send_premarket_digest():
    """Once daily, ~8:30 AM IST. Folds in the expiry reminder so it's one
    message, not two."""
    try:
        from utils.db import get_supabase
        from api.premarket_brief import get_premarket_brief
        from api.max_pain import get_max_pain_all

        supabase = get_supabase()
        brief = get_premarket_brief(supabase) or {}
        gift = brief.get("gift_nifty") or {}
        fii_dii = brief.get("fii_dii") or {}

        mp = get_max_pain_all()
        expiring_today = sorted({
            r["symbol"] for r in (mp.get("symbols") or [])
            if r.get("days_to_expiry") == 0
        })
        expiry_line = f"📅 Expiry today: {', '.join(expiring_today)}\n" if expiring_today else ""

        gift_line = ""
        if gift.get("pct_change") is not None:
            gift_line = f"GIFT NIFTY: {gift.get('pct_change'):+.2f}%\n"

        fii_line = ""
        if fii_dii.get("fii_net") is not None:
            fii_line = f"FII net: ₹{fii_dii.get('fii_net'):,.0f} Cr · DII net: ₹{fii_dii.get('dii_net', 0):,.0f} Cr\n"

        text = (
            f"📋 *Pre-Market — {brief.get('date','')}*\n"
            f"{expiry_line}"
            f"{gift_line}"
            f"{fii_line}"
            f"_GreekNova · Informational only · candidate alert_"
        )
        send_telegram(text)
        print("[ALERTS] (candidate) Pre-market digest sent")
    except Exception as e:
        print(f"[ALERTS] Pre-market digest error: {e}")


def send_eod_digest():
    """Once daily, ~15:45 IST, after close."""
    try:
        from api.positional_intelligence import get_positional_intelligence

        data = get_positional_intelligence()
        summary = data.get("summary", {}) if isinstance(data, dict) else {}
        elite_names = sorted({
            r["symbol"] for r in (data.get("stealth_buildup") or [])
            if r.get("tier") == "ELITE"
        })
        elite_line = f"Elite: {', '.join(elite_names[:10])}\n" if elite_names else ""

        text = (
            f"📊 *EOD Wrap — {data.get('date','')}*\n"
            f"Long Buildup: {summary.get('long_bias', 0)} · Short Buildup: {summary.get('short_bias', 0)}\n"
            f"Stealth Buildup today: {summary.get('stealth_buildup', 0)} stocks\n"
            f"{elite_line}"
            f"_GreekNova · Informational only · candidate alert_"
        )
        send_telegram(text)
        print("[ALERTS] (candidate) EOD digest sent")
    except Exception as e:
        print(f"[ALERTS] EOD digest error: {e}")
