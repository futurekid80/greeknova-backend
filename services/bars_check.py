"""Nov 14 2026: read-only comparison of spot_daily_bars with NSE's own cash end-of-day file.
Answers: how far are the bars' close/high/low/volume from NSE's figures, and is the bar close the last traded price
(LAST_PRICE) or NSE's official close (CLOSE_PRICE)? Changes nothing."""
import csv
import io
from datetime import timedelta

import requests

from services.fut_history_backfill import _HEADERS, _get


def _nse_cash(d):
    raw = _get(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv")
    if not raw:
        return None
    out = {}
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "ignore"))):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") != "EQ":
            continue
        try:
            out[r["SYMBOL"].upper()] = {k: float(r[k]) for k in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "LAST_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY")}
        except Exception:
            continue
    return out


def _pct(a, b):
    return abs(a - b) / b * 100 if b else 0


def run_bars_check(supabase, days=5, sample=None):
    from utils.market_calendar import today_ist, is_trading_day
    res, d, got = [], today_ist(), 0
    while got < days:
        if is_trading_day(d):
            nse = _nse_cash(d)
            if nse:
                bars = supabase.from_("spot_daily_bars").select("symbol,open,high,low,close,volume").eq("trade_date", d.isoformat()).range(0, 999).execute().data or []
                n = c_off = c_off05 = h_off = l_off = v_off = eq_last = eq_close = hi_below_close = 0
                ex = []
                for b in bars:
                    x = nse.get(b["symbol"].upper())
                    if not x:
                        continue
                    n += 1
                    cl, hi, lo, vol = float(b["close"]), float(b["high"]), float(b["low"]), float(b["volume"] or 0)
                    gap = _pct(cl, x["CLOSE_PRICE"])
                    if gap > 0.1: c_off += 1
                    if gap > 0.5: c_off05 += 1
                    if abs(hi - x["HIGH_PRICE"]) > 0.05: h_off += 1
                    if abs(lo - x["LOW_PRICE"]) > 0.05: l_off += 1
                    if x["TTL_TRD_QNTY"] and _pct(vol, x["TTL_TRD_QNTY"]) > 1: v_off += 1
                    if abs(cl - x["LAST_PRICE"]) <= 0.05: eq_last += 1
                    if abs(cl - x["CLOSE_PRICE"]) <= 0.05: eq_close += 1
                    if x["CLOSE_PRICE"] > hi + 0.05: hi_below_close += 1
                    if sample and b["symbol"].upper() in sample:
                        ex.append({"symbol": b["symbol"], "bar": [b["open"], hi, lo, cl, vol], "nse": [x["OPEN_PRICE"], x["HIGH_PRICE"], x["LOW_PRICE"], x["CLOSE_PRICE"], x["LAST_PRICE"], x["TTL_TRD_QNTY"]]})
                res.append({"date": d.isoformat(), "compared": n, "close_off_0.1pct": c_off, "close_off_0.5pct": c_off05,
                            "high_differs": h_off, "low_differs": l_off, "volume_off_1pct": v_off,
                            "bar_close_equals_nse_last_price": eq_last, "bar_close_equals_nse_official_close": eq_close,
                            "nse_close_above_bar_high": hi_below_close, "examples": ex})
            got += 1
        d -= timedelta(days=1)
    return res
