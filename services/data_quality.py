"""Nov 14 2026: daily data-quality checks ("gap finder").

Plain, deterministic checks that catch the kind of silent problems we found by hand: a morning job failing for
weeks, bars that disagree with the exchange, closing OI that never got corrected, symbols with missing days.

Three runs a day (all on trading days only):
  evening  20:45 IST  - after the nightly final-OI job: did final data get written, does it match NSE, are rows complete
  morning  09:05 IST  - after the 08:50 history job: did it run, is the Kite session alive
  live     09:45 IST  - 30 minutes into the session: are live snapshots flowing

Every check stores its result in job_status as "dq:<check>" (status success / warn / fail) so /admin/job-status and
/admin/data-quality show the latest state. Telegram gets a short evening summary every day (so silence means the
checker itself is broken) and morning/live messages only when something is wrong.
Nothing here changes data. It only reads and reports.
"""
from datetime import datetime, timedelta, timezone

OK, WARN, FAIL = "success", "warn", "fail"


def _ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _rec(supabase, name, status, detail):
    try:
        supabase.from_("job_status").upsert({
            "job_name": f"dq:{name}",
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "detail": str(detail)[:500],
            "records_count": None,
        }).execute()
    except Exception as e:
        print(f"[data_quality] could not record {name}: {e}")


def _trading_days_back(n, ref):
    from utils.market_calendar import is_trading_day
    out, d = [], ref
    while len(out) < n:
        d -= timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return out


def _rows(supabase, table, select, filters=None, page=1000, cap=6000):
    out, lo = [], 0
    while lo < cap:
        q = supabase.from_(table).select(select)
        for k, v in (filters or {}).items():
            q = q.eq(k, v)
        r = q.range(lo, lo + page - 1).execute().data or []
        out += r
        if len(r) < page:
            break
        lo += page
    return out


# ---------------------------------------------------------------- evening checks
def _check_final_oi_written(sb, today):
    r = sb.from_("job_status").select("detail,last_run_at").eq("job_name", "fut_oi_final").limit(1).execute().data or []
    got = (r[0].get("detail") or "")[:10] if r else ""
    if got >= today.isoformat():
        return OK, f"final OI written for {today}"
    late = _ist_now().hour < 22
    return (WARN if late else FAIL), f"final OI NOT written for {today} (last: {got or 'never'})" + (" - retry at 22:00 still pending" if late else "")


def _check_oi_matches_nse(sb, today):
    from services.fut_history_backfill import _fo_rows, _f
    from services.fut_oi_final import ROLLOVER_DAYS
    fo = _fo_rows(today)
    if fo is None:
        return WARN, "NSE F&O file for today not available yet"
    try:
        exp = min(datetime.strptime(v[0].get("XpryDt", "")[:10], "%Y-%m-%d").date() for v in fo.values() if v)
        if (exp - today).days <= ROLLOVER_DAYS:
            return OK, f"skipped: rollover week (near expiry {exp})"
    except Exception:
        pass
    rows = _rows(sb, "daily_oi_summary", "symbol,fut_oi_close", {"trade_date": today.isoformat()})
    n = bad = 0
    worst = []
    for r in rows:
        futs = fo.get(r["symbol"].upper()) or []
        mine, nse = _f(r.get("fut_oi_close")), _f(futs[0].get("OpnIntrst")) if futs else None
        if not mine or not nse:
            continue
        n += 1
        gap = abs(mine - nse) / nse * 100
        if gap > 0.5:
            bad += 1
            worst.append((gap, r["symbol"]))
    worst.sort(reverse=True)
    st = OK if bad <= 3 else (WARN if bad <= 15 else FAIL)
    return st, f"{bad} of {n} stocks differ from NSE final OI by >0.5%" + (f"; worst {[(s, round(g, 1)) for g, s in worst[:3]]}" if bad else "")


def _check_summary_rows(sb, today):
    prev = _trading_days_back(1, today)[0]
    cnt = lambda d: len(_rows(sb, "daily_oi_summary", "symbol,price_chg_pct", {"trade_date": d.isoformat()}))
    rows_today = _rows(sb, "daily_oi_summary", "symbol,price_chg_pct", {"trade_date": today.isoformat()})
    n_today, n_prev = len(rows_today), cnt(prev)
    nulls = sum(1 for r in rows_today if r.get("price_chg_pct") is None)
    if n_prev and n_today < 0.9 * n_prev:
        return FAIL, f"daily_oi_summary has {n_today} rows today vs {n_prev} yesterday"
    if nulls > 5:
        return WARN, f"{n_today} rows, but {nulls} have no price change"
    return OK, f"{n_today} rows today ({n_prev} yesterday), {nulls} without price change"


def _check_symbol_gaps(sb, today):
    days = [today] + _trading_days_back(5, today)
    per_day = {d.isoformat(): {r["symbol"] for r in _rows(sb, "daily_oi_summary", "symbol", {"trade_date": d.isoformat()})} for d in days}
    cur = per_day[today.isoformat()]
    gaps = {}
    for s in cur:
        miss = [d for d, syms in per_day.items() if s not in syms]
        if miss:
            gaps[s] = len(miss)
    # A symbol that only just joined F&O legitimately has fewer days; report only those missing 1-3 days of the window
    flagged = {s: m for s, m in gaps.items() if m <= 3}
    if flagged:
        return WARN, f"{len(flagged)} stocks have missing days in the last 6 sessions: {sorted(flagged)[:8]}"
    return OK, "no symbol has missing days in the last 6 sessions"


def _check_bars(sb, today):
    prev = _trading_days_back(1, today)[0]
    bars = _rows(sb, "spot_daily_bars", "symbol,close", {"trade_date": today.isoformat()})
    prev_n = len(_rows(sb, "spot_daily_bars", "symbol", {"trade_date": prev.isoformat()}))
    if prev_n and len(bars) < 0.95 * prev_n:
        return FAIL, f"spot_daily_bars has {len(bars)} rows today vs {prev_n} yesterday"
    summ = {r["symbol"]: r.get("close_price") for r in _rows(sb, "daily_oi_summary", "symbol,close_price", {"trade_date": today.isoformat()})}
    n = off = 0
    for b in bars:
        c, s = b.get("close"), summ.get(b["symbol"])
        if c and s:
            n += 1
            if abs(float(c) - float(s)) / float(s) * 100 > 0.5:
                off += 1
    st = OK if (n == 0 or off / n <= 0.4) else WARN
    return st, f"{len(bars)} bars today; {off} of {n} closes differ from stored close by >0.5%"


def _check_bars_final(sb, today):
    r = sb.from_("job_status").select("detail").eq("job_name", "bars_final").limit(1).execute().data or []
    got = (r[0].get("detail") or "")[:10] if r else ""
    if got >= today.isoformat():
        return OK, f"bars matched to NSE cash file for {today}"
    late = _ist_now().hour < 22
    return (WARN if late else FAIL), f"bars NOT matched to NSE cash file for {today} (last: {got or 'never'})"


def _check_pcr_daily(sb, today):
    have = {r["symbol"] for r in _rows(sb, "pcr_daily", "symbol", {"trade_date": today.isoformat()})}
    need = {"NIFTY", "BANKNIFTY", "SENSEX"}
    miss = need - have
    return (OK, "pcr_daily present for NIFTY, BANKNIFTY, SENSEX") if not miss else (FAIL, f"pcr_daily missing: {sorted(miss)}")


def _check_snapshot_close(sb, today):
    r = sb.from_("oi_snapshots").select("timestamp").order("timestamp", desc=True).limit(1).execute().data or []
    if not r:
        return FAIL, "no snapshots at all"
    ts = datetime.fromisoformat(r[0]["timestamp"].replace("Z", "+00:00")) + timedelta(hours=5, minutes=30)
    if ts.date() != today or (ts.hour, ts.minute) < (15, 25):
        return FAIL, f"last live snapshot is {ts:%d %b %H:%M} IST (expected today after 15:25)"
    return OK, f"last live snapshot {ts:%H:%M} IST"


EVENING = [("final_oi_written", _check_final_oi_written), ("oi_matches_nse", _check_oi_matches_nse),
           ("summary_rows", _check_summary_rows), ("symbol_gaps", _check_symbol_gaps), ("bars", _check_bars), ("bars_final", _check_bars_final),
           ("pcr_daily", _check_pcr_daily), ("snapshot_close", _check_snapshot_close)]


# ---------------------------------------------------------------- morning / live checks
def _check_heal_job(sb, today):
    r = sb.from_("job_status").select("status,detail,last_run_at").eq("job_name", "spot_bars_heal").limit(1).execute().data or []
    if not r:
        return WARN, "spot_bars_heal has never reported"
    at = datetime.fromisoformat(r[0]["last_run_at"].replace("Z", "+00:00")) + timedelta(hours=5, minutes=30)
    if at.date() != today:
        return FAIL, f"spot_bars_heal did not run today (last {at:%d %b %H:%M})"
    if r[0]["status"] != "success":
        return FAIL, f"spot_bars_heal: {r[0].get('detail')}"
    return OK, r[0].get("detail") or "ran"


def _check_kite_session(sb, today):
    from services.kite_auth import get_kite_client
    try:
        p = get_kite_client().profile()
        return OK, f"Kite session alive ({p.get('user_id', '?')})"
    except Exception as e:
        return FAIL, f"Kite session problem: {e}"


def _check_live_snapshots(sb, today):
    r = sb.from_("oi_snapshots").select("timestamp").order("timestamp", desc=True).limit(1).execute().data or []
    if not r:
        return FAIL, "no snapshots at all"
    ts = datetime.fromisoformat(r[0]["timestamp"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    if age > 12:
        return FAIL, f"live snapshots stalled: last one {age:.0f} min ago"
    syms = {x["symbol"] for x in (sb.from_("oi_snapshots").select("symbol").eq("timestamp", r[0]["timestamp"]).eq("option_type", "FUT").limit(1000).execute().data or [])}
    if len(syms) < 200:
        return WARN, f"latest snapshot covers only {len(syms)} futures symbols"
    return OK, f"last snapshot {age:.0f} min ago, {len(syms)} futures symbols"


def _check_pcr_live(sb, today):
    r = sb.from_("pcr_intraday").select("ts").eq("trade_date", today.isoformat()).order("ts", desc=True).limit(1).execute().data or []
    if not r:
        return FAIL, "no intraday PCR rows today"
    ts = datetime.fromisoformat(r[0]["ts"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    return (OK if age <= 25 else FAIL), f"latest PCR point {age:.0f} min ago"


MORNING = [("heal_job", _check_heal_job), ("kite_session", _check_kite_session)]
LIVE = [("live_snapshots", _check_live_snapshots), ("pcr_live", _check_pcr_live)]


def run_data_quality(supabase, kind="evening", notify=True, force=False):
    """kind: evening | morning | live. Returns the list of results. force=True on a non-trading day checks the
    last trading day instead (for manual test runs)."""
    from utils.market_calendar import is_trading_day, get_prev_trading_day
    today = _ist_now().date()
    if not is_trading_day(today):
        if not force:
            return {"skipped": "not a trading day"}
        today = get_prev_trading_day(today)
    checks = {"evening": EVENING, "morning": MORNING, "live": LIVE}[kind]
    results = []
    for name, fn in checks:
        try:
            st, detail = fn(supabase, today)
        except Exception as e:
            st, detail = WARN, f"check could not run: {e}"
        _rec(supabase, name, st, detail)
        results.append({"check": name, "status": st, "detail": detail})
        print(f"[data_quality] {kind}/{name}: {st} - {detail}")
    bad = [r for r in results if r["status"] != OK]
    if notify:
        try:
            from services.alert_engine import send_telegram
            if bad:
                lines = [f"{'🔴' if r['status'] == FAIL else '🟠'} {r['check']}: {r['detail']}" for r in bad]
                send_telegram(f"GreekNova data check ({kind}, {today:%d %b}) - {len(bad)} issue(s)\n" + "\n".join(lines), markdown=False)
            elif kind == "evening":
                send_telegram(f"✅ GreekNova data check ({today:%d %b}): all {len(results)} evening checks passed", markdown=False)
        except Exception as e:
            print(f"[data_quality] telegram failed: {e}")
    return results
