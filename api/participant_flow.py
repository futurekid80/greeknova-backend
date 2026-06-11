"""
api/participant_flow.py
NSE Participant-wise F&O OI data — FII, Client, DII, Pro
Source: https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv
Published daily at ~4:22 PM, available on archives from ~6 PM
"""
import requests
from datetime import datetime, timedelta, date as date_type
from utils.db import get_supabase
import pytz
import time as time_module

_cache: dict = {}
_cache_time: float = 0
_CACHE_TTL = 300

IST = pytz.timezone('Asia/Kolkata')


def _get_prev_trading_day(d=None):
    if d is None:
        d = datetime.now(IST).date()
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _parse_csv(text: str, trade_date) -> list:
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    records = []
    participant_map = {'Client': 'CLIENT', 'DII': 'DII', 'FII': 'FII', 'Pro': 'PRO'}
    for line in lines:
        parts = [p.strip() for p in line.split(',')]
        if not parts or parts[0] not in participant_map:
            continue
        try:
            records.append({
                'trade_date':         trade_date.isoformat(),
                'participant':        participant_map[parts[0]],
                'fut_idx_long':       int(parts[1] or 0),
                'fut_idx_short':      int(parts[2] or 0),
                'fut_stk_long':       int(parts[3] or 0),
                'fut_stk_short':      int(parts[4] or 0),
                'opt_idx_call_long':  int(parts[5] or 0),
                'opt_idx_put_long':   int(parts[6] or 0),
                'opt_idx_call_short': int(parts[7] or 0),
                'opt_idx_put_short':  int(parts[8] or 0),
                'opt_stk_call_long':  int(parts[9] or 0),
                'opt_stk_put_long':   int(parts[10] or 0),
                'opt_stk_call_short': int(parts[11] or 0),
                'opt_stk_put_short':  int(parts[12] or 0),
                'total_long':         int(parts[13] or 0),
                'total_short':        int(parts[14] or 0),
            })
        except (IndexError, ValueError) as e:
            print(f"[ParticipantFlow] Parse error: {line[:60]} — {e}")
    return records


def fetch_and_store_participant_flow(trade_date=None):
    supabase = get_supabase()

    if trade_date is None:
        today = datetime.now(IST).date()
        # Use today if weekday, else last trading day
        if today.weekday() >= 5:
            target_date = _get_prev_trading_day(today)
        else:
            target_date = today
    else:
        target_date = date_type.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date

    print(f"[ParticipantFlow] Fetching for {target_date}...")

    urls = [
        f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{target_date.strftime('%d%m%Y')}.csv",
        f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{target_date.strftime('%d%m%Y')}.csv",
    ]

    text = None
    for url in urls:
        try:
            resp = requests.get(url, timeout=20, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': 'text/csv,*/*',
                'Referer': 'https://www.nseindia.com/',
            })
            if resp.status_code == 200 and 'Client' in resp.text:
                text = resp.text
                print(f"[ParticipantFlow] ✅ Got CSV from {url}")
                break
        except Exception as e:
            print(f"[ParticipantFlow] URL failed: {e}")

    if not text:
        print(f"[ParticipantFlow] ❌ CSV not available for {target_date}")
        return {"error": f"CSV not available for {target_date}", "date": target_date.isoformat()}

    records = _parse_csv(text, target_date)
    if not records:
        return {"error": "Parse failed", "date": target_date.isoformat()}

    supabase.table("participant_flow")\
        .upsert(records, on_conflict="trade_date,participant")\
        .execute()

    global _cache, _cache_time
    _cache = {}
    _cache_time = 0

    print(f"[ParticipantFlow] ✅ Stored {len(records)} records for {target_date}")
    return {"stored": len(records), "date": target_date.isoformat()}


def get_participant_flow(days: int = 20):
    """Get participant flow summary for last N trading days."""
    global _cache, _cache_time

    if _cache and (time_module.time() - _cache_time) < _CACHE_TTL:
        return _cache

    supabase = get_supabase()

    rows = supabase.from_("participant_flow")\
        .select("*")\
        .order("trade_date", desc=True)\
        .limit(days * 4)\
        .execute()

    if not rows.data:
        return {"summary": [], "latest_date": None, "latest": {}, "error": "No data yet — runs daily at 6:30 PM"}

    # Group by date
    by_date: dict = {}
    for r in rows.data:
        d = r["trade_date"]
        if d not in by_date:
            by_date[d] = {}
        by_date[d][r["participant"]] = r

    dates = sorted(by_date.keys(), reverse=True)[:days]

    def safe(r, key):
        return int(r.get(key) or 0)

    summary = []
    for d in dates:
        day = by_date[d]
        fii = day.get("FII", {})
        client = day.get("CLIENT", {})
        dii = day.get("DII", {})
        pro = day.get("PRO", {})

        fii_idx_long  = safe(fii, "fut_idx_long")
        fii_idx_short = safe(fii, "fut_idx_short")
        fii_idx_net   = fii_idx_long - fii_idx_short
        fii_long_pct  = round(fii_idx_long / (fii_idx_long + fii_idx_short) * 100, 1) if (fii_idx_long + fii_idx_short) > 0 else 0

        client_idx_net = safe(client, "fut_idx_long") - safe(client, "fut_idx_short")
        dii_idx_net    = safe(dii, "fut_idx_long") - safe(dii, "fut_idx_short")
        pro_idx_net    = safe(pro, "fut_idx_long") - safe(pro, "fut_idx_short")

        fii_call_net = safe(fii, "opt_idx_call_long") - safe(fii, "opt_idx_call_short")
        fii_put_net  = safe(fii, "opt_idx_put_long")  - safe(fii, "opt_idx_put_short")

        summary.append({
            "date": d,
            # FII Index Futures
            "fii_fut_idx_long":     fii_idx_long,
            "fii_fut_idx_short":    fii_idx_short,
            "fii_fut_idx_net":      fii_idx_net,
            "fii_fut_idx_long_pct": fii_long_pct,
            # FII Options
            "fii_call_net":         fii_call_net,
            "fii_put_net":          fii_put_net,
            "fii_total_net":        safe(fii, "total_long") - safe(fii, "total_short"),
            # Other participants
            "client_idx_net":       client_idx_net,
            "dii_idx_net":          dii_idx_net,
            "pro_idx_net":          pro_idx_net,
            # Key signal: FII vs Client divergence (opposite sides = high conviction)
            "fii_client_opposite":  (fii_idx_net > 0) != (client_idx_net > 0),
            "fii_client_divergence": abs(fii_idx_net - client_idx_net),
            # Bias
            "fii_bias": "BULLISH" if fii_idx_net > 0 else "BEARISH" if fii_idx_net < 0 else "NEUTRAL",
        })

    latest_date = dates[0] if dates else None

    result = {
        "summary":     summary,
        "latest_date": latest_date,
        "latest":      {p: by_date[latest_date].get(p, {}) for p in ["FII","CLIENT","DII","PRO"]} if latest_date else {},
    }

    _cache = result
    _cache_time = time_module.time()
    return result
