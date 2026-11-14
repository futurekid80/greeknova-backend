"""Nov 14 2026: make spot_daily_bars match NSE's own end-of-day cash file.

Why: the 16:00 IST bar from our data provider misses the last trades of the day (the closing session), so about
two thirds of stocks have a close that differs from NSE's, volume is ~2-4% short, and on some days the true high
or close sits above the bar's high (HINDALCO 6 Nov: bar high 950, NSE high/close 958.40).

Fields set from NSE (EQ series, F&O stocks that already have a bar row): open, high, low, close, volume.
Index rows (NIFTY etc.) are not in the cash file and are never touched. Rows are only updated where a value
actually differs. dry_run=True (default) writes nothing and returns counts plus examples.
"""
import time
from datetime import datetime, timedelta

from services.bars_check import _nse_cash


def _differs(a, b, tol):
    return abs(float(a or 0) - float(b or 0)) > tol


def run_bars_final(supabase, start, end, dry_run=True, sample=None):
    from utils.market_calendar import is_trading_day
    d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    out, tot = [], {"rows": 0, "updated": 0, "close": 0, "high": 0, "low": 0, "volume": 0}
    while d <= end_d:
        if not is_trading_day(d):
            d += timedelta(days=1)
            continue
        nse = _nse_cash(d)
        time.sleep(1)
        if not nse:
            out.append({"date": d.isoformat(), "skipped": "NSE cash file not available"})
            d += timedelta(days=1)
            continue
        bars = supabase.from_("spot_daily_bars").select("symbol,open,high,low,close,volume").eq("trade_date", d.isoformat()).range(0, 999).execute().data or []
        n = upd = c = h = l = v = skipped_big = 0
        ex = []
        todo = []
        for b in bars:
            x = nse.get(b["symbol"].upper())
            if not x:
                continue
            n += 1
            new = {"open": x["OPEN_PRICE"], "high": x["HIGH_PRICE"], "low": x["LOW_PRICE"], "close": x["CLOSE_PRICE"],
                   "volume": int(x["TTL_TRD_QNTY"])}
            # Safety: a close more than 5% away from NSE's is more likely a split/bonus adjustment than a cut-off bar. Leave it.
            if b["close"] and abs(float(b["close"]) - float(new["close"])) / float(new["close"]) > 0.05:
                skipped_big += 1
                continue
            dc, dh, dl = _differs(b["close"], new["close"], 0.005), _differs(b["high"], new["high"], 0.005), _differs(b["low"], new["low"], 0.005)
            dv = _differs(b["volume"], new["volume"], 0.5)
            do = _differs(b["open"], new["open"], 0.005)
            if not (dc or dh or dl or dv or do):
                continue
            upd += 1; c += dc; h += dh; l += dl; v += dv
            if sample and b["symbol"].upper() in sample:
                ex.append({"symbol": b["symbol"], "before": {k: b[k] for k in ("open", "high", "low", "close", "volume")}, "after": new})
            todo.append((b["symbol"], new))
        if not dry_run and todo:
            from concurrent.futures import ThreadPoolExecutor
            _d = d.isoformat()
            def _w(item):
                try:
                    supabase.from_("spot_daily_bars").update(item[1]).eq("symbol", item[0]).eq("trade_date", _d).execute()
                except Exception as e:
                    print(f"[bars_final] {item[0]} {_d}: {e}")
            with ThreadPoolExecutor(8) as ex_pool:
                list(ex_pool.map(_w, todo))
        out.append({"date": d.isoformat(), "compared": n, "skipped_big_diff": skipped_big, "rows_to_update" if dry_run else "rows_updated": upd,
                    "close_changes": c, "high_changes": h, "low_changes": l, "volume_changes": v, "examples": ex})
        for k, val in (("rows", n), ("updated", upd), ("close", c), ("high", h), ("low", l), ("volume", v)):
            tot[k] += val
        d += timedelta(days=1)
    return {"dry_run": dry_run, "total": tot, "days": out}


def run_bars_final_today(supabase):
    """Nightly: today's bars from NSE. Returns today's date string when written, "" on a non-trading day,
    None when NSE's cash file is not out yet."""
    from datetime import timezone
    from utils.market_calendar import is_trading_day
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    if not is_trading_day(today):
        return ""
    r = run_bars_final(supabase, today.isoformat(), today.isoformat(), dry_run=False)
    if r["days"] and r["days"][0].get("skipped"):
        return None
    return today.isoformat()


def run_bars_history_background(supabase, start, end):
    """Runs run_bars_final day by day in a background thread; progress goes to job_status 'bars_final_history'."""
    import threading
    from datetime import timezone

    def _prog(status, detail):
        try:
            supabase.from_("job_status").upsert({"job_name": "bars_final_history",
                "last_run_at": datetime.now(timezone.utc).isoformat(), "status": status, "detail": detail[:500],
                "records_count": None}).execute()
        except Exception:
            pass

    def _go():
        d = datetime.strptime(start, "%Y-%m-%d").date()
        e = datetime.strptime(end, "%Y-%m-%d").date()
        tot_upd = tot_big = 0
        miss = []
        try:
            while d <= e:
                r = run_bars_final(supabase, d.isoformat(), d.isoformat(), dry_run=False)
                day = (r["days"] or [{}])[0]
                if day.get("skipped"):
                    miss.append(d.isoformat())
                else:
                    tot_upd += day.get("rows_updated", 0)
                    tot_big += day.get("skipped_big_diff", 0)
                _prog("running", f"at {d} updated={tot_upd} skipped_big={tot_big} no_nse_file={len(miss)}")
                d += timedelta(days=1)
            _prog("success", f"DONE {start}..{end} updated={tot_upd} skipped_big={tot_big} no_nse_file={miss[:20]}")
        except Exception as ex:
            _prog("fail", f"stopped at {d}: {ex}")
    threading.Thread(target=_go, daemon=True).start()
