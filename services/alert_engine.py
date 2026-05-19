"""
services/alert_engine.py

GreekNova Alert Engine — runs after every OI capture cycle.
Detects: OI Spikes, UOA High Conviction, Wall Shifts
Delivers: Telegram personal chat via new alerts bot
Deduplicates: tracks sent alerts in memory to avoid spam
"""

import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Telegram config ───────────────────────────────────────────────────────────
ALERT_BOT_TOKEN = "8659302604:AAFWa38GGioCI6iEJwD1ZBS88MILPVhJys8"
PERSONAL_CHAT_ID = "5513733966"
TELEGRAM_URL = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"

# ── Alert deduplication ───────────────────────────────────────────────────────
# In-memory store — resets on restart (intentional, avoids stale state)
_sent_alerts: set = set()        # keys of alerts already sent this session
_last_walls:  dict = {}          # symbol → (ce_wall, pe_wall) from last check
_last_heartbeat: Optional[datetime] = None

IST = timezone(timedelta(hours=5, minutes=30))


# ── Telegram send ─────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    try:
        r = requests.post(TELEGRAM_URL, json={
            "chat_id": PERSONAL_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[ALERTS] Telegram send failed: {e}")
        return False


def is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    total = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= total <= (15 * 60 + 30)


# ── Main alert check — called after every capture ─────────────────────────────
def run_alert_check():
    """Run after each OI capture. Checks all alert types and sends Telegram."""
    if not is_market_hours():
        return

    print("[ALERTS] Running alert check...")
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        today = datetime.now(IST).strftime('%Y-%m-%d')

        # Get last 2 timestamps
        ts_rows = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(20).execute()

        timestamps = sorted(set(r["timestamp"] for r in (ts_rows.data or [])))
        if len(timestamps) < 2:
            print("[ALERTS] Not enough snapshots yet")
            return

        ts_new = timestamps[-1]
        ts_old = timestamps[-2]

        alerts_fired = []

        # ── 1. OI SPIKE DETECTION ─────────────────────────────────────────────
        spike_alerts = _check_oi_spikes(supabase, today, ts_new, ts_old)
        alerts_fired.extend(spike_alerts)

        # ── 2. UOA HIGH CONVICTION ────────────────────────────────────────────
        uoa_alerts = _check_uoa_signals(supabase, today, timestamps)
        alerts_fired.extend(uoa_alerts)

        # ── 3. WALL SHIFT DETECTION ───────────────────────────────────────────
        wall_alerts = _check_wall_shifts(supabase, today, ts_new, ts_old)
        alerts_fired.extend(wall_alerts)

        # Send each alert via Telegram
        for alert in alerts_fired:
            alert_key = alert.get("key", alert.get("text", "")[:50])
            if alert_key not in _sent_alerts:
                send_telegram(alert["text"])
                _sent_alerts.add(alert_key)
                print(f"[ALERTS] Sent: {alert_key}")

        if not alerts_fired:
            print("[ALERTS] No new alerts this cycle")

        # ── 4. HEARTBEAT (every 30 mins) ──────────────────────────────────────
        _maybe_send_heartbeat(supabase, today, ts_new, len(timestamps))

    except Exception as e:
        print(f"[ALERTS] Alert check error: {e}")


# ── OI Spike Detection ────────────────────────────────────────────────────────
def _check_oi_spikes(supabase, today: str, ts_new: str, ts_old: str,
                     threshold: float = 15.0) -> list:
    """Detect OI changes >threshold% between last two snapshots."""
    alerts = []
    try:
        def fetch_snap(ts):
            rows = []
            for offset in range(0, 50000, 1000):
                batch = supabase.from_("oi_snapshots")\
                    .select("symbol, tradingsymbol, strike, option_type, oi, last_price")\
                    .eq("timestamp", ts)\
                    .range(offset, offset + 999).execute()
                if not batch.data: break
                rows.extend(batch.data)
                if len(batch.data) < 1000: break
            return rows

        new_rows = fetch_snap(ts_new)
        old_rows = fetch_snap(ts_old)

        old_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in old_rows}

        for row in new_rows:
            sym = row["symbol"]
            key = f"{sym}_{row['tradingsymbol']}"
            old = old_map.get(key)
            if not old:
                continue

            new_oi = row["oi"] or 0
            old_oi = old["oi"] or 0

            if old_oi < 50000:
                continue  # skip low-liquidity strikes

            if old_oi == 0:
                continue

            oi_pct = (new_oi - old_oi) / old_oi * 100

            if abs(oi_pct) < threshold:
                continue

            strike     = row["strike"]
            opt_type   = row["option_type"]
            ltp        = row["last_price"] or 0
            direction  = "📈 BUILD" if oi_pct > 0 else "📉 UNWIND"
            alert_key  = f"spike_{sym}_{strike}_{opt_type}_{round(oi_pct)}"

            text = (
                f"🔥 *OI Spike Alert*\n"
                f"*{sym}* {strike} {opt_type}\n"
                f"{direction} · OI {oi_pct:+.1f}% in 5 mins\n"
                f"LTP: ₹{ltp} · Old OI: {_fmt(old_oi)} → New OI: {_fmt(new_oi)}\n"
                f"_GreekNova · Informational only_"
            )

            alerts.append({"key": alert_key, "text": text})

    except Exception as e:
        print(f"[ALERTS] OI spike check error: {e}")

    return alerts


# ── UOA High Conviction ───────────────────────────────────────────────────────
def _check_uoa_signals(supabase, today: str, timestamps: list) -> list:
    """Detect UOA signals with score 4+ using same logic as api/uoa.py."""
    alerts = []
    try:
        from api.uoa import get_uoa
        uoa_data = get_uoa(date=today)
        signals = uoa_data.get("signals", [])

        for sig in signals:
            if sig.get("score", 0) < 4:
                continue

            sym       = sig["symbol"]
            strike    = sig["strike"]
            opt_type  = sig["option_type"]
            signal_t  = sig["signal_type"]
            bias      = sig["bias"]
            score     = sig["score"]
            ltp       = sig.get("ltp", 0)
            oi_chg    = sig.get("oi_chg_30min", 0)
            vol_ratio = sig.get("vol_oi_ratio", 0)
            alert_key = f"uoa_{sym}_{strike}_{opt_type}_{signal_t}"

            bias_icon = "🟢" if bias == "BULLISH" else "🔴"
            signal_label = signal_t.replace("_", " ").title()

            text = (
                f"🐋 *High Conviction UOA* {bias_icon}\n"
                f"*{sym}* {strike} {opt_type} · Score {score}/5\n"
                f"Signal: {signal_label}\n"
                f"OI 30m: {oi_chg:+.1f}% · Vol/OI: {vol_ratio:.1f}x · LTP: ₹{ltp}\n"
                f"Bias: {bias}\n"
                f"_GreekNova · Informational only_"
            )

            alerts.append({"key": alert_key, "text": text})

    except Exception as e:
        print(f"[ALERTS] UOA check error: {e}")

    return alerts


# ── Wall Shift Detection ──────────────────────────────────────────────────────
def _check_wall_shifts(supabase, today: str, ts_new: str, ts_old: str) -> list:
    """Detect if CE wall or PE wall has shifted for NIFTY/BANKNIFTY/FINNIFTY."""
    global _last_walls
    alerts = []

    INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]

    try:
        def get_walls(ts, symbol):
            rows = supabase.from_("oi_snapshots")\
                .select("strike, option_type, oi")\
                .eq("symbol", symbol)\
                .eq("timestamp", ts)\
                .limit(5000).execute().data or []

            ce_oi: dict = {}
            pe_oi: dict = {}
            for r in rows:
                s = r["strike"]
                oi = r["oi"] or 0
                if r["option_type"] == "CE":
                    ce_oi[s] = ce_oi.get(s, 0) + oi
                else:
                    pe_oi[s] = pe_oi.get(s, 0) + oi

            ce_wall = max(ce_oi, key=ce_oi.get) if ce_oi else None
            pe_wall = max(pe_oi, key=pe_oi.get) if pe_oi else None
            return ce_wall, pe_wall

        for sym in INDICES:
            ce_new, pe_new = get_walls(ts_new, sym)
            ce_old, pe_old = _last_walls.get(sym, (None, None))

            # Update stored walls
            _last_walls[sym] = (ce_new, pe_new)

            if ce_old is None:
                continue  # first run, no comparison

            wall_changes = []
            if ce_new != ce_old and ce_new is not None:
                wall_changes.append(f"CE wall: {_fmt_strike(ce_old)} → *{_fmt_strike(ce_new)}* (resistance shifted)")
            if pe_new != pe_old and pe_new is not None:
                wall_changes.append(f"PE wall: {_fmt_strike(pe_old)} → *{_fmt_strike(pe_new)}* (support shifted)")

            if not wall_changes:
                continue

            alert_key = f"wall_{sym}_{ce_new}_{pe_new}"
            text = (
                f"🏗️ *Wall Shift Alert — {sym}*\n"
                + "\n".join(wall_changes) +
                f"\n_GreekNova · Informational only_"
            )
            alerts.append({"key": alert_key, "text": text})

    except Exception as e:
        print(f"[ALERTS] Wall shift check error: {e}")

    return alerts


# ── Heartbeat (every 30 mins) ─────────────────────────────────────────────────
def _maybe_send_heartbeat(supabase, today: str, ts_new: str, snapshot_count: int):
    global _last_heartbeat
    now = datetime.now(IST)

    # Send heartbeat every 30 mins
    if _last_heartbeat and (now - _last_heartbeat).total_seconds() < 30 * 60:
        return

    _last_heartbeat = now

    try:
        # Get NIFTY CMP
        cmp_row = supabase.from_("cmp_prices")\
            .select("cmp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()

        nifty_cmp = cmp_row.data[0]["cmp"] if cmp_row.data else "N/A"

        # Get NIFTY PCR
        rows = supabase.from_("oi_snapshots")\
            .select("option_type, oi")\
            .eq("symbol", "NIFTY")\
            .eq("timestamp", ts_new)\
            .limit(5000).execute().data or []

        total_ce = sum(r["oi"] or 0 for r in rows if r["option_type"] == "CE")
        total_pe = sum(r["oi"] or 0 for r in rows if r["option_type"] == "PE")
        pcr = round(total_pe / total_ce, 2) if total_ce > 0 else "N/A"

        ce_wall, pe_wall = _last_walls.get("NIFTY", ("N/A", "N/A"))
        time_str = now.strftime("%H:%M IST")

        text = (
            f"💓 *GreekNova Heartbeat* · {time_str}\n"
            f"NIFTY: ₹{nifty_cmp}\n"
            f"PCR: {pcr} · CE Wall: {_fmt_strike(ce_wall)} · PE Wall: {_fmt_strike(pe_wall)}\n"
            f"Snapshots today: {snapshot_count}\n"
            f"Alert engine: ✅ Running\n"
            f"_Market hours · Auto-monitoring active_"
        )
        send_telegram(text)
        print(f"[ALERTS] Heartbeat sent at {time_str}")

    except Exception as e:
        print(f"[ALERTS] Heartbeat error: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt(n: int) -> str:
    if n >= 10_000_000: return f"{n/10_000_000:.1f}Cr"
    if n >= 100_000:    return f"{n/100_000:.1f}L"
    if n >= 1_000:      return f"{n/1_000:.0f}K"
    return str(n)

def _fmt_strike(s) -> str:
    if s is None: return "N/A"
    try: return f"{int(s):,}"
    except: return str(s)
