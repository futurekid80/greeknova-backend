"""
Earnings (financial results) calendar.

Source: NSE's public board-meetings feed (purpose containing "result"), with a
CSV import as a fallback for when NSE blocks the server. Rows live in
public.earnings_calendar (symbol, result_date). Other modules call
upcoming_results_map() to tag stocks with their next results date.
"""
import csv
import hmac
import io
import os
import time
from datetime import date, datetime, timedelta

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from utils.db import get_supabase

router = APIRouter()

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/corporate-board-meetings"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d-%b-%y")

_cache = {"at": 0.0, "map": {}}


def _authorised(request: Request) -> bool:
    key = os.getenv("GATE_STATS_KEY", "")
    sent = request.headers.get("x-gate-key", "")
    return bool(key) and hmac.compare_digest(key, sent)


def _parse_date(v):
    if not v:
        return None
    v = str(v).strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(v, f).date()
        except ValueError:
            continue
    return None


def _pick(d: dict, *names):
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if low.get(n.lower()) not in (None, ""):
            return low[n.lower()]
    return None


def fetch_from_nse(days_ahead: int = 60):
    """Returns (records, diagnostics). records = [(symbol, date, purpose)]."""
    diag = {"ok": False}
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept": "application/json,text/plain,*/*",
                      "Accept-Language": "en-US,en;q=0.9", "Referer": NSE_HOME})
    try:
        r0 = s.get(NSE_HOME, timeout=15)
        diag["home_status"] = r0.status_code
        today = date.today()
        params = {"index": "equities",
                  "from_date": today.strftime("%d-%m-%Y"),
                  "to_date": (today + timedelta(days=days_ahead)).strftime("%d-%m-%Y")}
        time.sleep(0.5)
        r = s.get(NSE_API, params=params, timeout=20)
        diag["api_status"] = r.status_code
        if r.status_code != 200:
            diag["error"] = f"NSE returned {r.status_code}"
            return [], diag
        data = r.json()
    except Exception as e:
        diag["error"] = f"{type(e).__name__}: {e}"
        return [], diag

    if isinstance(data, dict):
        data = data.get("data") or data.get("rows") or []
    diag["raw_rows"] = len(data)
    if data:
        diag["sample_keys"] = list(data[0].keys())[:20]
    out = []
    for d in data:
        sym = _pick(d, "bm_symbol", "symbol", "sm_symbol")
        dt = _parse_date(_pick(d, "bm_date", "meetingDate", "meeting_date", "date"))
        purpose = str(_pick(d, "bm_purpose", "purpose", "bm_desc", "description") or "")
        if not sym or not dt:
            continue
        if "result" not in purpose.lower():
            continue
        out.append((str(sym).strip().upper(), dt, purpose[:200]))
    diag["result_rows"] = len(out)
    diag["ok"] = len(out) > 0
    return out, diag


def parse_csv(text: str):
    """NSE event-calendar CSV: SYMBOL, COMPANY, PURPOSE, DETAILS, DATE (case/order tolerant)."""
    out = []
    rd = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for row in rd:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        sym = _pick(row, "symbol")
        dt = _parse_date(_pick(row, "date", "bm_date", "meeting date"))
        purpose = str(_pick(row, "purpose", "details") or "")
        if not sym or not dt:
            continue
        if purpose and "result" not in purpose.lower():
            continue
        out.append((sym.upper(), dt, purpose[:200]))
    return out


def save_records(records, source: str):
    if not records:
        return 0
    sb = get_supabase()
    now = datetime.utcnow().isoformat() + "Z"
    rows = [{"symbol": s, "result_date": d.isoformat(), "purpose": p, "source": source, "updated_at": now}
            for s, d, p in records]
    for i in range(0, len(rows), 500):
        sb.from_("earnings_calendar").upsert(rows[i:i + 500], on_conflict="symbol,result_date").execute()
    _cache["at"] = 0.0
    return len(rows)


def refresh_from_nse():
    records, diag = fetch_from_nse()
    diag["saved"] = save_records(records, "nse") if records else 0
    print(f"[earnings] refresh: {diag}")
    return diag


def upcoming_results_map(from_date: date = None, cache_seconds: int = 600):
    """{symbol: next result date (date)} for results on/after from_date."""
    now = time.time()
    if _cache["map"] and now - _cache["at"] < cache_seconds:
        return _cache["map"]
    start = (from_date or date.today()).isoformat()
    try:
        res = (get_supabase().from_("earnings_calendar").select("symbol,result_date")
               .gte("result_date", start).order("result_date").execute())
    except Exception as e:
        print(f"[earnings] map load failed: {e}")
        return _cache["map"]
    m = {}
    for r in res.data or []:
        try:
            m.setdefault(r["symbol"], date.fromisoformat(r["result_date"]))
        except Exception:
            continue
    _cache["map"], _cache["at"] = m, now
    return m


@router.get("/test-earnings-fetch")
async def test_earnings_fetch(request: Request):
    if not _authorised(request):
        return JSONResponse({"error": "not authorised"}, status_code=403)
    records, diag = await run_in_threadpool(fetch_from_nse)
    diag["sample"] = [(s, d.isoformat(), p) for s, d, p in records[:5]]
    return diag


@router.post("/run-earnings-refresh")
async def run_earnings_refresh(request: Request):
    if not _authorised(request):
        return JSONResponse({"error": "not authorised"}, status_code=403)
    return await run_in_threadpool(refresh_from_nse)


@router.post("/admin/earnings-import")
async def earnings_import(request: Request):
    if not _authorised(request):
        return JSONResponse({"error": "not authorised"}, status_code=403)
    text = (await request.body()).decode("utf-8", errors="replace")
    records = parse_csv(text)
    saved = await run_in_threadpool(save_records, records, "csv")
    return {"parsed": len(records), "saved": saved}


@router.get("/earnings/upcoming")
async def earnings_upcoming(days: int = 30):
    days = max(1, min(days, 120))
    m = await run_in_threadpool(upcoming_results_map)
    end = date.today() + timedelta(days=days)
    return {"results": [{"symbol": s, "date": d.isoformat(), "days": (d - date.today()).days}
                        for s, d in sorted(m.items(), key=lambda kv: kv[1]) if d <= end]}
