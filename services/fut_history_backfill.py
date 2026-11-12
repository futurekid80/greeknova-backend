"""Nov 12 2026: fills missing days of daily_oi_summary for stocks that were only tracked part of the time
(SYRMA, KARURVYSYA, CUB, COROMANDEL, DATAPATTNS, NAVINFLUOR were missing 2-9 Nov).

Our Kite key has no Historical Data permission, so the numbers come from NSE's own end-of-day files:
  - F&O bhavcopy  -> futures OI, OI change, volume, open/close (nearest + next expiry)
  - cash bhavcopy -> official close and previous close (price_chg_pct)

Only the futures / price columns that the Signal History popup and the futures signal use are filled.
Options-based columns (total_oi, ce_oi, ...) keep their defaults (0) on backfilled rows.

Safety rules:
  * dry_run=True (default) writes nothing; it prints what it WOULD write and compares its numbers with rows we
    already hold, so the method can be checked before anything is written.
  * Existing rows are never overwritten, except that a NULL price_chg_pct (no previous close was available) is
    filled in together with the signal that depends on it.
"""
import csv
import io
import time
import zipfile
from datetime import date, datetime, timedelta

import requests

DEFAULT_SYMBOLS = ["SYRMA", "KARURVYSYA", "CUB", "COROMANDEL", "DATAPATTNS", "NAVINFLUOR"]
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}


def _get(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=40)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code == 404:
                return None
        except Exception as e:  # network hiccup
            last = str(e)
        time.sleep(2)
    print(f"[fut_backfill] giving up on {url}: {last}")
    return None


def _fo_rows(d: date):
    """{symbol: [futures rows sorted by expiry]} for one trading day, from the F&O bhavcopy."""
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
    raw = _get(url)
    if not raw:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        text = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    except Exception as e:
        print(f"[fut_backfill] could not unzip F&O file for {d}: {e}")
        return None
    out: dict = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("FinInstrmTp") != "STF":
            continue
        out.setdefault(r.get("TckrSymb", "").upper(), []).append(r)
    for s in out:
        out[s].sort(key=lambda x: x.get("XpryDt", ""))
    return out


def _cash_rows(d: date):
    """{symbol: {close, prev_close}} from the cash bhavcopy (EQ series)."""
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
    raw = _get(url)
    if not raw:
        return None
    out = {}
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "ignore"))):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") != "EQ":
            continue
        try:
            out[r["SYMBOL"].upper()] = {"close": float(r["CLOSE_PRICE"]), "prev": float(r["PREV_CLOSE"])}
        except Exception:
            continue
    return out


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _signal(oi_chg, price):
    if oi_chg is None or price is None:
        return "NEUTRAL"
    if oi_chg >= 2.0 and price >= 0.3:
        return "LONG_BUILDUP"
    if oi_chg >= 2.0 and price <= -0.3:
        return "SHORT_BUILDUP"
    if oi_chg <= -2.0 and price >= 0.3:
        return "SHORT_COVERING"
    if oi_chg <= -2.0 and price <= -0.3:
        return "LONG_UNWINDING"
    return "NEUTRAL"


def _oi_pct(row):
    close = _f(row.get("OpnIntrst"))
    chg = _f(row.get("ChngInOpnIntrst"))
    if close is None or chg is None or close - chg <= 0:
        return None
    return round(chg / (close - chg) * 100, 2)


def _build(sym, fo, cash):
    futs = (fo or {}).get(sym) or []
    c = (cash or {}).get(sym)
    if not futs or not c:
        return None
    n0 = futs[0]
    n1 = futs[1] if len(futs) > 1 else None
    price_chg = round((c["close"] - c["prev"]) / c["prev"] * 100, 2) if c["prev"] > 0 else None
    oi_chg = _oi_pct(n0)
    op, cl = _f(n0.get("OpnPric")), _f(n0.get("ClsPric"))
    return {
        "symbol": sym,
        "close_price": c["close"],
        "price_chg_pct": price_chg,
        # NSE's volume is in contracts; our table stores shares, so multiply by the lot size in the same file.
        "fut_vol": int((_f(n0.get("TtlTradgVol")) or 0) * (_f(n0.get("NewBrdLotQty")) or 0)),
        "fut_oi_chg_pct": oi_chg if oi_chg is not None else 0,
        "fut_oi_chg_pct_next": _oi_pct(n1) if n1 else None,
        "fut_oi_close": _f(n0.get("OpnIntrst")),
        "fut_oi_close_next": _f(n1.get("OpnIntrst")) if n1 else None,
        "fut_price_chg_pct": round((cl - op) / op * 100, 2) if op and cl else None,
        "fut_signal": _signal(oi_chg, price_chg),
    }


def run_fut_history_backfill(supabase, symbols=None, start="2026-10-14", end=None, dry_run=True):
    from utils.market_calendar import is_trading_day
    symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
    end_d = datetime.strptime(end, "%Y-%m-%d").date() if end else date.today() - timedelta(days=1)
    d = datetime.strptime(start, "%Y-%m-%d").date()
    mode = "DRY RUN (nothing is written)" if dry_run else "WRITE"
    print(f"[fut_backfill] {mode}: {symbols} from {d} to {end_d}")

    existing = {}
    for s in symbols:
        rows = supabase.from_("daily_oi_summary").select(
            "trade_date,close_price,price_chg_pct,fut_vol,fut_oi_chg_pct,fut_oi_close,fut_signal"
        ).eq("symbol", s).execute().data or []
        existing[s] = {r["trade_date"]: r for r in rows}

    inserts, fills, report = [], [], []
    while d <= end_d:
        if d.weekday() >= 5 or not is_trading_day(d):
            d += timedelta(days=1)
            continue
        fo, cash = _fo_rows(d), _cash_rows(d)
        time.sleep(1)
        if fo is None or cash is None:
            print(f"[fut_backfill] {d}: NSE files not available (fo={fo is not None}, cash={cash is not None}); skipped")
            d += timedelta(days=1)
            continue
        for s in symbols:
            new = _build(s, fo, cash)
            have = existing[s].get(d.isoformat())
            if new is None:
                if not have:
                    print(f"[fut_backfill] {d} {s}: no futures contract in NSE file (not in F&O that day)")
                continue
            if have:
                # compare with what we already hold, so the method can be checked
                print(f"[fut_backfill] CHECK {d} {s}: oi_chg stored={have.get('fut_oi_chg_pct')} nse={new['fut_oi_chg_pct']} | "
                      f"fut_vol stored={have.get('fut_vol')} nse={new['fut_vol']} | close stored={have.get('close_price')} nse={new['close_price']} | "
                      f"oi_close stored={have.get('fut_oi_close')} nse={new['fut_oi_close']} | price_chg stored={have.get('price_chg_pct')} nse={new['price_chg_pct']}")
                if have.get("price_chg_pct") is None and new["price_chg_pct"] is not None:
                    fills.append((d.isoformat(), s, new["price_chg_pct"], new["fut_signal"]))
                    print(f"[fut_backfill] FILL  {d} {s}: price_chg_pct null -> {new['price_chg_pct']}, signal {have.get('fut_signal')} -> {new['fut_signal']}")
            else:
                row = dict(new)
                row["trade_date"] = d.isoformat()
                inserts.append(row)
                print(f"[fut_backfill] ADD   {d} {s}: close={new['close_price']} price_chg={new['price_chg_pct']} "
                      f"oi_chg={new['fut_oi_chg_pct']} vol={new['fut_vol']} signal={new['fut_signal']}")
        d += timedelta(days=1)

    print(f"[fut_backfill] {mode}: {len(inserts)} rows to add, {len(fills)} null price changes to fill")
    if not dry_run:
        for i in range(0, len(inserts), 100):
            supabase.from_("daily_oi_summary").upsert(inserts[i:i + 100], on_conflict="trade_date,symbol").execute()
        for dt, s, pc, sig in fills:
            supabase.from_("daily_oi_summary").update({"price_chg_pct": pc, "fut_signal": sig}) \
                .eq("symbol", s).eq("trade_date", dt).is_("price_chg_pct", "null").execute()
        print(f"[fut_backfill] WRITE finished: added {len(inserts)}, filled {len(fills)}")
    return {"added": len(inserts), "filled": len(fills), "dry_run": dry_run}
