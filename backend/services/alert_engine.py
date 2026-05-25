import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

ALERT_BOT_TOKEN = "8659302604:AAFWa38GGioCI6iEJwD1ZBS88MILPVhJys8"
PERSONAL_CHAT_ID = "5513733966"
TELEGRAM_URL = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"

_sent_alerts: set = set()
_last_walls:  dict = {}
_last_heartbeat: Optional[datetime] = None

IST = timezone(timedelta(hours=5, minutes=30))

def send_telegram(text: str) -> bool:
    try:
        r = requests.post(TELEGRAM_URL, json={"chat_id": PERSONAL_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[ALERTS] Telegram send failed: {e}")
        return False

def is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    total = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= total <= (15 * 60 + 30)

def run_alert_check():
    if not is_market_hours(): return
    print("[ALERTS] Running alert check...")
    try:
        from utils.db import get_supabase
        supabase = get_supabase()
        today = datetime.now(IST).strftime('%Y-%m-%d')
        ts_rows = supabase.from_("oi_snapshots").select("timestamp").eq("symbol", "NIFTY").gte("timestamp", f"{today}T00:00:00+00:00").order("timestamp", desc=True).limit(5000).execute()
        timestamps = sorted(set(r["timestamp"] for r in (ts_rows.data or [])))
        if len(timestamps) < 2: return
        ts_new = timestamps[-1]
        ts_old = timestamps[-2]
        alerts_fired = []
        alerts_fired.extend(_check_oi_spikes(supabase, today, ts_new, ts_old))
        alerts_fired.extend(_check_uoa_signals(supabase, today, timestamps))
        alerts_fired.extend(_check_wall_shifts(supabase, today, ts_new, ts_old))
        for alert in alerts_fired:
            key = alert.get("key", alert.get("text", "")[:50])
            if key not in _sent_alerts:
                send_telegram(alert["text"])
                _sent_alerts.add(key)
                print(f"[ALERTS] Sent: {key}")
        if not alerts_fired: print("[ALERTS] No new alerts this cycle")
        _maybe_send_heartbeat(supabase, today, ts_new, len(timestamps))
    except Exception as e:
        print(f"[ALERTS] Alert check error: {e}")

def _check_oi_spikes(supabase, today, ts_new, ts_old, threshold=15.0):
    alerts = []
    try:
        def fetch_snap(ts):
            rows = []
            for offset in range(0, 50000, 1000):
                batch = supabase.from_("oi_snapshots").select("symbol, tradingsymbol, strike, option_type, oi, last_price").eq("timestamp", ts).range(offset, offset+999).execute()
                if not batch.data: break
                rows.extend(batch.data)
                if len(batch.data) < 1000: break
            return rows
        new_rows = fetch_snap(ts_new)
        old_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in fetch_snap(ts_old)}
        for row in new_rows:
            sym = row["symbol"]
            key = f"{sym}_{row['tradingsymbol']}"
            old = old_map.get(key)
            if not old: continue
            new_oi = row["oi"] or 0
            old_oi = old["oi"] or 0
            if old_oi < 50000 or old_oi == 0: continue
            oi_pct = (new_oi - old_oi) / old_oi * 100
            if abs(oi_pct) < threshold: continue
            strike = row["strike"]; opt_type = row["option_type"]; ltp = row["last_price"] or 0
            direction = "📈 BUILD" if oi_pct > 0 else "📉 UNWIND"
            alert_key = f"spike_{sym}_{strike}_{opt_type}_{round(oi_pct)}"
            alerts.append({"key": alert_key, "text": f"🔥 *OI Spike Alert*\n*{sym}* {strike} {opt_type}\n{direction} · OI {oi_pct:+.1f}% in 5 mins\nLTP: ₹{ltp} · {_fmt(old_oi)} → {_fmt(new_oi)}\n_GreekNova · Informational only_"})
    except Exception as e:
        print(f"[ALERTS] OI spike error: {e}")
    return alerts

def _check_uoa_signals(supabase, today, timestamps):
    alerts = []
    try:
        from api.uoa import get_uoa
        for sig in get_uoa(date=today).get("signals", []):
            if sig.get("score", 0) < 4: continue
            sym = sig["symbol"]; strike = sig["strike"]; opt_type = sig["option_type"]
            alert_key = f"uoa_{sym}_{strike}_{opt_type}_{sig['signal_type']}"
            bias_icon = "🟢" if sig["bias"] == "BULLISH" else "🔴"
            alerts.append({"key": alert_key, "text": f"🐋 *High Conviction UOA* {bias_icon}\n*{sym}* {strike} {opt_type} · Score {sig['score']}/5\n{sig['signal_type'].replace('_',' ').title()}\nOI 30m: {sig.get('oi_chg_30min',0):+.1f}% · Vol/OI: {sig.get('vol_oi_ratio',0):.1f}x\n_GreekNova · Informational only_"})
    except Exception as e:
        print(f"[ALERTS] UOA error: {e}")
    return alerts

def _check_wall_shifts(supabase, today, ts_new, ts_old):
    global _last_walls
    alerts = []
    try:
        def get_walls(ts, symbol):
            rows = supabase.from_("oi_snapshots").select("strike, option_type, oi").eq("symbol", symbol).eq("timestamp", ts).limit(5000).execute().data or []
            ce_oi = {}; pe_oi = {}
            for r in rows:
                s = r["strike"]; oi = r["oi"] or 0
                if r["option_type"] == "CE": ce_oi[s] = ce_oi.get(s,0) + oi
                else: pe_oi[s] = pe_oi.get(s,0) + oi
            return max(ce_oi, key=ce_oi.get) if ce_oi else None, max(pe_oi, key=pe_oi.get) if pe_oi else None
        for sym in ["NIFTY","BANKNIFTY","FINNIFTY"]:
            ce_new, pe_new = get_walls(ts_new, sym)
            ce_old, pe_old = _last_walls.get(sym, (None, None))
            _last_walls[sym] = (ce_new, pe_new)
            if ce_old is None: continue
            changes = []
            if ce_new != ce_old and ce_new: changes.append(f"CE wall: {_fmt_strike(ce_old)} → *{_fmt_strike(ce_new)}*")
            if pe_new != pe_old and pe_new: changes.append(f"PE wall: {_fmt_strike(pe_old)} → *{_fmt_strike(pe_new)}*")
            if changes:
                alert_key = f"wall_{sym}_{ce_new}_{pe_new}"
                alerts.append({"key": alert_key, "text": f"🏗️ *Wall Shift — {sym}*\n" + "\n".join(changes) + "\n_GreekNova · Informational only_"})
    except Exception as e:
        print(f"[ALERTS] Wall shift error: {e}")
    return alerts

def _maybe_send_heartbeat(supabase, today, ts_new, snapshot_count):
    global _last_heartbeat
    now = datetime.now(IST)
    if _last_heartbeat and (now - _last_heartbeat).total_seconds() < 30*60: return
    _last_heartbeat = now
    try:
        cmp_row = supabase.from_("cmp_prices").select("cmp").eq("symbol","NIFTY").gte("timestamp",f"{today}T00:00:00+00:00").order("timestamp",desc=True).limit(1).execute()
        nifty_cmp = cmp_row.data[0]["cmp"] if cmp_row.data else "N/A"
        rows = supabase.from_("oi_snapshots").select("option_type,oi").eq("symbol","NIFTY").eq("timestamp",ts_new).limit(5000).execute().data or []
        total_ce = sum(r["oi"] or 0 for r in rows if r["option_type"]=="CE")
        total_pe = sum(r["oi"] or 0 for r in rows if r["option_type"]=="PE")
        pcr = round(total_pe/total_ce,2) if total_ce > 0 else "N/A"
        ce_wall, pe_wall = _last_walls.get("NIFTY",(None,None))
        send_telegram(f"💓 *GreekNova Heartbeat* · {now.strftime('%H:%M IST')}\nNIFTY: ₹{nifty_cmp}\nPCR: {pcr} · CE Wall: {_fmt_strike(ce_wall)} · PE Wall: {_fmt_strike(pe_wall)}\nSnapshots: {snapshot_count} · Alert engine: ✅\n_Market hours · Auto-monitoring active_")
        print(f"[ALERTS] Heartbeat sent")
    except Exception as e:
        print(f"[ALERTS] Heartbeat error: {e}")

def _fmt(n):
    if n >= 10_000_000: return f"{n/10_000_000:.1f}Cr"
    if n >= 100_000: return f"{n/100_000:.1f}L"
    if n >= 1_000: return f"{n/1_000:.0f}K"
    return str(n)

def _fmt_strike(s):
    if s is None: return "N/A"
    try: return f"{int(s):,}"
    except: return str(s)
