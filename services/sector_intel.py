"""
sector_intel.py  --  Nov 22 2026. EXPERIMENT, admin-only (not linked anywhere for members).

Clue, not a prediction: which sectors are showing accumulation or distribution in futures OI + price,
and which are TURNING (weak for a few weeks, now flipping). Built only from data we already store:
daily_oi_summary (near + next month futures OI, price change) and the sector map.

Per stock and window the same four-way read as the OI Buildup tabs is used (OI change with price change):
  Long Buildup (+1)   OI up, price up       Short Covering (+0.5)  OI down, price up
  Short Buildup (-1)  OI up, price down     Long Unwinding (-0.5)  OI down, price down
OI change is near + next month COMBINED (rollover-neutral, same helper the Positional page uses).
A sector's score is the average of those weights over its stocks, scaled to -100..+100.

Also returns an internal history check (what sectors in each state did over the next 5 / 10 days, versus the
average F&O stock) so the idea can be judged before any member ever sees it. Overlapping windows and a short
history (full combined-OI data starts September 2026), so treat it as indicative only.
"""
import time
from datetime import datetime, timedelta

from services.sector_map import SECTOR_MAP, SYMBOL_SECTOR
from utils.oi_math import combined_oi_pct

HISTORY_FROM = "2026-09-01"   # full-universe near + next month OI starts here
RECENT = 5                    # trading days in the "recent" window
PRIOR = 15                    # trading days in the "prior" window (ends where recent starts)
MIN_PX = 0.3                  # same price band as the OI Buildup tabs
MIN_OI = 2.0                  # same OI floor as the OI Buildup tabs
WEIGHT = {"LB": 1.0, "SC": 0.5, "SB": -1.0, "LU": -0.5, "N": 0.0}

# Label thresholds (score is -100..+100). Kept as plain constants so they can be tuned during testing.
T_WEAK = -15.0      # "was weak"
T_UP = 10.0         # "now improving"
T_JUMP = 25.0       # minimum improvement versus the prior window
T_ACC = 20.0        # accumulating / distributing level
T_BREADTH = 55.0    # % of stocks up over the recent window for "turning up" (and 45 for "turning down")

_cache = {"t": 0.0, "data": None}
CACHE_SECONDS = 900


def _fetch_rows(supabase):
    rows, lo = [], 0
    while True:
        res = supabase.from_("daily_oi_summary").select(
            "symbol,trade_date,fut_oi_chg_pct,fut_oi_chg_pct_next,fut_oi_close,fut_oi_close_next,price_chg_pct"
        ).gte("trade_date", HISTORY_FROM).order("trade_date").range(lo, lo + 999).execute().data or []
        rows += res
        if len(res) < 1000:
            break
        lo += 1000
        if lo > 60000:
            break
    return rows


def _rollover_days(dates):
    """Dates that sit in a rollover week (near-month OI drains into next month, so near-only is misleading)."""
    from api.positional_radar import get_current_expiry
    from services.fut_oi_final import ROLLOVER_DAYS
    out = set()
    for d in dates:
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
            exp = datetime.strptime(get_current_expiry(dd), "%Y-%m-%d").date()
            if 0 <= (exp - dd).days <= ROLLOVER_DAYS:
                out.add(d)
        except Exception:
            pass
    return out


def _classify(oi, px):
    if oi is None or px is None or abs(oi) < MIN_OI:
        return "N"
    if oi > 0 and px >= MIN_PX:
        return "LB"
    if oi > 0 and px <= -MIN_PX:
        return "SB"
    if oi < 0 and px >= MIN_PX:
        return "SC"
    if oi < 0 and px <= -MIN_PX:
        return "LU"
    return "N"


def _build(rows):
    import math
    dates = sorted({r["trade_date"] for r in rows})
    didx = {d: i for i, d in enumerate(dates)}
    roll = _rollover_days(dates)
    # per stock: daily log factors for OI (combined) and price, with None for unknown OI days
    stocks = {}
    for r in rows:
        sym = r["symbol"]
        sec = SYMBOL_SECTOR.get(sym)
        if not sec:
            continue
        i = didx[r["trade_date"]]
        c = combined_oi_pct(r.get("fut_oi_close"), r.get("fut_oi_chg_pct"), r.get("fut_oi_close_next"), r.get("fut_oi_chg_pct_next"))
        if c is None and r["trade_date"] not in roll and r.get("fut_oi_chg_pct") is not None:
            c = float(r["fut_oi_chg_pct"])  # outside rollover week the near month alone is fine
        if c is not None and c <= -99:
            c = None
        try:
            p = float(r.get("price_chg_pct")) if r.get("price_chg_pct") is not None else None
        except Exception:
            p = None
        st = stocks.setdefault(sym, {"sec": sec, "oi": [None] * len(dates), "px": [None] * len(dates)})
        st["oi"][i] = math.log(1 + c / 100.0) if c is not None else None
        st["px"][i] = math.log(1 + p / 100.0) if p is not None and p > -99 else None
    return dates, stocks


def _window(st, end_i, k):
    """(oi_pct, px_pct) compounded over the k trading days ending at end_i, or None if any day is unknown."""
    import math
    lo = end_i - k + 1
    if lo < 1:
        return None
    o = st["oi"][lo:end_i + 1]
    p = st["px"][lo:end_i + 1]
    if any(x is None for x in o) or any(x is None for x in p):
        return None
    return (math.exp(sum(o)) - 1) * 100.0, (math.exp(sum(p)) - 1) * 100.0


def _sector_read(members, end_i, k):
    cnt = {"LB": 0, "SC": 0, "SB": 0, "LU": 0, "N": 0}
    ups = n = 0
    pxs = []
    detail = []
    for sym, st in members:
        w = _window(st, end_i, k)
        if not w:
            continue
        oi, px = w
        s = _classify(oi, px)
        cnt[s] += 1
        n += 1
        ups += 1 if px > 0 else 0
        pxs.append(px)
        detail.append({"symbol": sym, "state": s, "oi_pct": round(oi, 1), "px_pct": round(px, 1)})
    if n < 3:
        return None
    score = sum(WEIGHT[s] * c for s, c in cnt.items()) / n * 100.0
    return {"n": n, "score": round(score, 1), "breadth": round(ups / n * 100.0, 0), "avg_px": round(sum(pxs) / len(pxs), 2),
            "counts": cnt, "detail": detail}


def _label(recent, prior):
    if not recent:
        return "Not enough data"
    r, b = recent["score"], recent["breadth"]
    p = prior["score"] if prior else None
    if p is not None:
        if p <= T_WEAK and r >= T_UP and (r - p) >= T_JUMP and b >= T_BREADTH:
            return "Turning up"
        if p >= -T_WEAK and r <= -T_UP and (p - r) >= T_JUMP and b <= 100 - T_BREADTH:
            return "Turning down"
    if r >= T_ACC and (p is None or p >= 0):
        return "Accumulating"
    if r <= -T_ACC and (p is None or p <= 0):
        return "Distributing"
    return "Mixed"


def _forward(members, end_i, h, dates):
    import math
    if end_i + h >= len(dates):
        return None
    vals = []
    for sym, st in members:
        seg = st["px"][end_i + 1:end_i + h + 1]
        if any(x is None for x in seg):
            continue
        vals.append((math.exp(sum(seg)) - 1) * 100.0)
    return sum(vals) / len(vals) if len(vals) >= 3 else None


def compute(supabase, force=False):
    if not force and _cache["data"] and time.time() - _cache["t"] < CACHE_SECONDS:
        return _cache["data"]
    rows = _fetch_rows(supabase)
    dates, stocks = _build(rows)
    if len(dates) < RECENT + PRIOR + 2:
        return {"error": "Not enough history yet", "dates": len(dates)}
    by_sector = {}
    for sym, st in stocks.items():
        by_sector.setdefault(st["sec"], []).append((sym, st))
    all_members = [(s, st) for lst in by_sector.values() for s, st in lst]

    last = len(dates) - 1
    sectors = []
    for sec, members in sorted(by_sector.items()):
        recent = _sector_read(members, last, RECENT)
        prior = _sector_read(members, last - RECENT, PRIOR)
        label = _label(recent, prior)
        if not recent:
            continue
        det = sorted(recent["detail"], key=lambda d: (WEIGHT[d["state"]], d["oi_pct"]), reverse=True)
        sectors.append({
            "sector": sec, "stocks": len(members), "label": label,
            "recent_score": recent["score"], "prior_score": prior["score"] if prior else None,
            "change": round(recent["score"] - prior["score"], 1) if prior else None,
            "breadth_pct": recent["breadth"], "avg_px_5d": recent["avg_px"],
            "avg_px_prior": prior["avg_px"] if prior else None,
            "counts": recent["counts"],
            "leaders": [d for d in det if WEIGHT[d["state"]] > 0][:4],
            "weak": [d for d in det[::-1] if WEIGHT[d["state"]] < 0][:4],
        })

    # internal history check: what happened next, by label, versus the average F&O stock over the same days
    stats = {}
    first_i = PRIOR + RECENT + 1
    for i in range(first_i, len(dates)):
        mkt5 = _forward(all_members, i, 5, dates)
        mkt10 = _forward(all_members, i, 10, dates)
        for sec, members in by_sector.items():
            rec = _sector_read(members, i, RECENT)
            pri = _sector_read(members, i - RECENT, PRIOR)
            lab = _label(rec, pri)
            f5, f10 = _forward(members, i, 5, dates), _forward(members, i, 10, dates)
            s = stats.setdefault(lab, {"n": 0, "n5": 0, "x5": 0.0, "h5": 0, "n10": 0, "x10": 0.0, "h10": 0})
            s["n"] += 1
            if f5 is not None and mkt5 is not None:
                s["n5"] += 1; s["x5"] += f5 - mkt5; s["h5"] += 1 if f5 > mkt5 else 0
            if f10 is not None and mkt10 is not None:
                s["n10"] += 1; s["x10"] += f10 - mkt10; s["h10"] += 1 if f10 > mkt10 else 0
    hist = []
    for lab, s in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
        hist.append({"label": lab, "sector_days": s["n"],
                     "n5": s["n5"], "excess_5d_pct": round(s["x5"] / s["n5"], 2) if s["n5"] else None,
                     "beat_market_5d_pct": round(s["h5"] / s["n5"] * 100) if s["n5"] else None,
                     "n10": s["n10"], "excess_10d_pct": round(s["x10"] / s["n10"], 2) if s["n10"] else None,
                     "beat_market_10d_pct": round(s["h10"] / s["n10"] * 100) if s["n10"] else None})

    order = {"Turning up": 0, "Accumulating": 1, "Mixed": 2, "Distributing": 3, "Turning down": 4}
    sectors.sort(key=lambda x: (order.get(x["label"], 5), -x["recent_score"]))
    data = {
        "as_of": dates[-1], "history_days": len(dates), "window_recent": RECENT, "window_prior": PRIOR,
        "sectors": sectors, "history_check": hist,
        "note": "Experiment, admin only. A clue from futures OI and price, not a prediction.",
    }
    _cache["t"], _cache["data"] = time.time(), data
    return data
