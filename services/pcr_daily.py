"""End-of-day put-call OI per index and expiry (Nov 9 2026).

Why: the 5-minute capture stores full strike data only for the nearest few
(weekly) expiries, so monthly expiries are missing for the first ~3 weeks of
each cycle and a clean monthly PCR history had gaps. This job runs once after
the close, pulls the FULL option chain (all strikes) for each index for the
expiries in the next ~100 days straight from Kite, and stores one small row per
(date, symbol, expiry) plus an 'ALL' row. Not shown in the frontend yet.
"""
from datetime import timedelta
import time

from utils.db import get_supabase
from services.kite_auth import get_kite_client

PCR_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
HORIZON_DAYS = 100


def _monthly_expiries(expiries):
    """Last expiry of each calendar month among the listed ones."""
    by_month = {}
    for e in sorted(expiries):
        by_month[(e.year, e.month)] = e
    return set(by_month.values())


def run_pcr_daily(trade_date=None):
    from utils.market_calendar import is_trading_day, today_ist
    d = trade_date or today_ist()
    if not is_trading_day(d):
        print(f"[pcr_daily] {d} is not a trading day -- skipped")
        return {"skipped": True}

    kite = get_kite_client()
    supabase = get_supabase()
    instruments = list(kite.instruments("NFO"))
    try:
        instruments += list(kite.instruments("BFO"))  # SENSEX
    except Exception as e:
        print(f"[pcr_daily] BFO instruments failed (SENSEX skipped): {e}")

    rows = []
    for sym in PCR_INDICES:
        opts = [i for i in instruments
                if i.get("name") == sym and i.get("instrument_type") in ("CE", "PE")
                and i.get("expiry") is not None and i["expiry"] >= d]
        if not opts:
            continue
        all_exp = sorted({i["expiry"] for i in opts})
        monthly = _monthly_expiries(all_exp)
        keep = {e for e in all_exp if e <= d + timedelta(days=HORIZON_DAYS)}
        sel = [i for i in opts if i["expiry"] in keep]
        exch = "BFO" if sym == "SENSEX" else "NFO"
        oi = {}
        for k in range(0, len(sel), 400):
            batch = sel[k:k + 400]
            try:
                q = kite.quote([f"{exch}:{i['tradingsymbol']}" for i in batch])
            except Exception as e:
                print(f"[pcr_daily] {sym} quote batch failed: {e}")
                continue
            for i in batch:
                v = q.get(f"{exch}:{i['tradingsymbol']}")
                if v is not None:
                    oi[i["tradingsymbol"]] = int(v.get("oi", 0) or 0)
            time.sleep(0.35)

        tot_ce = tot_pe = 0
        for e in sorted(keep):
            ce = sum(oi.get(i["tradingsymbol"], 0) for i in sel if i["expiry"] == e and i["instrument_type"] == "CE")
            pe = sum(oi.get(i["tradingsymbol"], 0) for i in sel if i["expiry"] == e and i["instrument_type"] == "PE")
            n = len({i["strike"] for i in sel if i["expiry"] == e})
            if ce <= 0 and pe <= 0:
                continue
            tot_ce += ce
            tot_pe += pe
            rows.append({"trade_date": d.isoformat(), "symbol": sym, "expiry": e.isoformat(),
                         "is_monthly": e in monthly, "ce_oi": ce, "pe_oi": pe,
                         "pcr": round(pe / ce, 4) if ce else None, "strikes": n})
        if tot_ce > 0:
            rows.append({"trade_date": d.isoformat(), "symbol": sym, "expiry": "ALL", "is_monthly": False,
                         "ce_oi": tot_ce, "pe_oi": tot_pe, "pcr": round(tot_pe / tot_ce, 4), "strikes": None})

    if rows:
        supabase.table("pcr_daily").upsert(rows, on_conflict="trade_date,symbol,expiry").execute()
    print(f"[pcr_daily] {d}: saved {len(rows)} rows")
    return {"saved": len(rows)}
