"""
services/alert_engine.py

GreekNova Alert Engine — runs after every OI capture cycle.

Two alert types only:
1. HIGH CONV — FUT signal + Options Confirmation aligned, persistence ≥ 10 snaps
2. Near-ATM writing — UOA put/call writing within 2% of CMP, score ≥ 4

Delivers: Telegram only
Deduplicates: once per symbol per alert type per day
"""

import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Telegram config ───────────────────────────────────────────────────────────
ALERT_BOT_TOKEN  = "8659302604:AAFWa38GGioCI6iEJwD1ZBS88MILPVhJys8"
PERSONAL_CHAT_ID = "5513733966"
TELEGRAM_URL     = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"

# ── Deduplication — reset daily ───────────────────────────────────────────────
_sent_alerts: set = set()
_sent_date:   str = ""

IST = timezone(timedelta(hours=5, minutes=30))


def _reset_if_new_day():
    global _sent_alerts, _sent_date
    today = datetime.now(IST).strftime('%Y-%m-%d')
    if today != _sent_date:
        _sent_alerts = set()
        _sent_date   = today


def send_telegram(text: str) -> bool:
    try:
        r = requests.post(TELEGRAM_URL, json={
            "chat_id":    PERSONAL_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown"
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[ALERTS] Telegram send failed: {e}")
        return False


def is_market_hours() -> bool:
    now   = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    total = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= total <= (15 * 60 + 30)


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


# ── Alert 1: HIGH CONV ────────────────────────────────────────────────────────
def _check_high_conv_signals():
    """
    Fire when FUT signal + Options Confirmation both align,
    persistence >= 10 snapshots. Once per symbol per day.
    """
    try:
        from api.signal_log import get_signal_log
        data    = get_signal_log()
        signals = data.get("signals", [])

        for sig in signals:
            sym         = sig["symbol"]
            persistence = sig.get("persistence", 0)
            confirms    = sig.get("options_confirms")
            signal_type = sig.get("signal_type", "")
            label       = sig.get("label", "")
            oi_chg      = sig.get("oi_chg_pct", 0)
            price_chg   = sig.get("price_chg_pct", 0)
            cmp         = sig.get("cmp", 0)
            cpr_pos     = sig.get("cpr_position", "")
            opt_sig     = sig.get("options_signal")

            # Must be HIGH CONV: FUT signal + options confirms + persistence >= 10
            if not confirms:
                continue
            if persistence < 10:
                continue

            alert_key = f"highconv_{sym}_{signal_type}"
            if alert_key in _sent_alerts:
                continue

            bias_icon = "🟢" if sig.get("bias") == "BULLISH" else "🔴"
            opt_line  = ""
            if opt_sig:
                opt_line = f"Options: {opt_sig.get('label','')} · {opt_sig.get('strike','')} {opt_sig.get('option_type','')} · Score {opt_sig.get('score','')}/5\n"

            text = (
                f"🎯 *HIGH CONV Signal* {bias_icon}\n"
                f"*{sym}* · ₹{cmp}\n"
                f"FUT: {label} · OI {oi_chg:+.1f}% · Price {price_chg:+.2f}%\n"
                f"{opt_line}"
                f"CPR: {cpr_pos.replace('_',' ').title()} · Persistence: {persistence} snaps\n"
                f"_GreekNova · Informational only_"
            )

            if send_telegram(text):
                _sent_alerts.add(alert_key)
                print(f"[ALERTS] HIGH CONV sent: {sym}")

    except Exception as e:
        print(f"[ALERTS] High conv check error: {e}")


# ── Alert 2: Near-ATM Writing ─────────────────────────────────────────────────
def _check_near_atm_writing():
    """
    Fire when put/call writing appears within 2% of CMP, score >= 4.
    Once per symbol per option_type per day.
    """
    try:
        from api.uoa import get_uoa
        from utils.db import get_supabase

        today    = datetime.now(IST).strftime('%Y-%m-%d')
        uoa_data = get_uoa(date=today)
        signals  = uoa_data.get("signals", [])

        # Get CMP map
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

            sym      = sig["symbol"]
            strike   = float(sig.get("strike", 0))
            opt_type = sig.get("option_type", "")
            score    = sig.get("score", 0)
            ltp      = sig.get("ltp", 0)
            oi_chg   = sig.get("oi_chg_30min", 0)
            persist  = sig.get("persistence_pct", 0)
            cmp      = cmp_map.get(sym, 0)

            if cmp <= 0 or strike <= 0:
                continue

            distance_pct = abs(strike - cmp) / cmp * 100
            if distance_pct > 2.0:
                continue

            alert_key = f"nearatm_{sym}_{strike}_{opt_type}"
            if alert_key in _sent_alerts:
                continue

            bias_icon    = "🟢" if signal_type == "PUT_WRITING" else "🔴"
            signal_label = "Put Writing" if signal_type == "PUT_WRITING" else "Call Writing"
            direction    = "defending support" if signal_type == "PUT_WRITING" else "capping upside"

            text = (
                f"📍 *Near-ATM {signal_label}* {bias_icon}\n"
                f"*{sym}* · ₹{cmp:.1f} CMP\n"
                f"{strike} {opt_type} · {distance_pct:.1f}% from CMP · Score {score}/5\n"
                f"OI 30m: {oi_chg:+.1f}% · LTP: ₹{ltp} · Persist: {persist}%\n"
                f"Writer {direction}\n"
                f"_GreekNova · Informational only_"
            )

            if send_telegram(text):
                _sent_alerts.add(alert_key)
                print(f"[ALERTS] Near-ATM {signal_label} sent: {sym} {strike} {opt_type}")

    except Exception as e:
        print(f"[ALERTS] Near-ATM check error: {e}")

# ── Alert 3: Intraday Signal ──────────────────────────────────────────────────
def _check_intraday_signals():
    """
    Fire when a new intraday FUT signal appears with:
    - persistence >= 10 snapshots (confirmed, not noise)
    - vol_surge = True (volume confirmation)
    - OI change >= 5%
    Once per symbol per signal_type per day.
    Separate higher-priority alert for HIGH CONV.
    """
    try:
        from api.signal_log import get_signal_log
        data    = get_signal_log()
        signals = data.get("signals", [])

        for sig in signals:
            sym         = sig["symbol"]
            persistence = sig.get("persistence", 0)
            signal_type = sig.get("signal_type", "")
            label       = sig.get("label", "")
            oi_chg      = sig.get("oi_chg_pct", 0)
            price_chg   = sig.get("price_chg_pct", 0)
            vol_chg     = sig.get("vol_chg_pct", 0)
            vol_surge   = sig.get("vol_surge", False)
            cmp         = sig.get("cmp", 0)
            cpr_pos     = sig.get("cpr_position", "")
            first_seen  = sig.get("first_seen", "")
            confirms    = sig.get("options_confirms")
            ce_wall     = sig.get("ce_wall")
            pe_wall     = sig.get("pe_wall")
            trade_range_pct = sig.get("trade_range_pct")
            range_label = sig.get("range_label", "")

            # Qualification — persistence >= 10, vol surge, OI >= 5%
            if persistence < 10:
                continue
            if not vol_surge:
                continue
            if abs(oi_chg) < 5.0:
                continue

            # Skip if already sent HIGH CONV alert for same signal
            # (HIGH CONV is handled by _check_high_conv_signals)
            alert_key = f"intraday_{sym}_{signal_type}"
            if alert_key in _sent_alerts:
                continue

            # Build alert
            bias_icon = "🟢" if sig.get("bias") == "BULLISH" else "🔴"

            signal_icons = {
                "LONG_BUILDUP":   "🐂",
                "SHORT_BUILDUP":  "🐻",
                "SHORT_COVERING": "🔄",
                "LONG_UNWINDING": "⚠️",
            }
            sig_icon = signal_icons.get(signal_type, "📊")

            # CPR context
            cpr_line = ""
            if cpr_pos:
                virgin = "🔵 Virgin · " if sig.get("cpr_is_virgin") else ""
                cpr_line = f"CPR: {virgin}{cpr_pos}\n"

            # OI Walls
            walls_line = ""
            if ce_wall and pe_wall:
                walls_line = f"📈 CE ₹{ce_wall:,.0f} · 📉 PE ₹{pe_wall:,.0f}"
                if trade_range_pct:
                    walls_line += f" · {trade_range_pct}% {range_label}"
                walls_line += "\n"

            # Options confirmation
            conf_line = ""
            if confirms is True:
                conf_line = "✅ Options Confirms\n"
            elif confirms is False:
                conf_line = "⚠️ Options Contradicts\n"

            text = (
                f"{sig_icon} *GreekNova Intraday Signal* {bias_icon}\n"
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
                print(f"[ALERTS] Intraday signal sent: {sym} {signal_type}")

    except Exception as e:
        print(f"[ALERTS] Intraday signal check error: {e}")
