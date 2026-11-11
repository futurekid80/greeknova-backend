"""Whole-chain intraday PCR (Nov 11 2026).

The 5-minute capture stores only the ~51 strikes around the price, so PCR built from it differs from what
brokers show (they add every strike). This job pulls EVERY strike of the nearest expiries straight from Kite
every few minutes and stores one small row per (time, index, expiry): total call/put OI and volume.
The PCR page prefers these rows and falls back to the old nearby-strike data when none exist yet.
"""
from datetime import datetime, timezone, timedelta
import time

from utils.db import get_supabase
from services.kite_auth import get_kite_client

PCR_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
NEAREST_EXPIRIES = 3

_inst_cache = {"day": None, "rows": []}


def _instruments(kite, day):
    if _inst_cache["day"] == day and _inst_cache["rows"]:
        return _inst_cache["rows"]
    rows = list(kite.instruments("NFO"))
    try:
        rows += list(kite.instruments("BFO"))
    except Exception as e:
        print(f"[pcr_intraday] BFO instruments failed (SENSEX skipped): {e}")
    _inst_cache["day"], _inst_cache["rows"] = day, rows
    return rows


def run_pcr_intraday():
    from utils.market_calendar import is_trading_day
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    d = now_ist.date()
    mins = now_ist.hour * 60 + now_ist.minute
    if not is_trading_day(d) or not (9 * 60 + 15 <= mins <= 15 * 60 + 45):
        return {"skipped": True}

    kite = get_kite_client()
    supabase = get_supabase()
    instruments = _instruments(kite, d)
    ts = now_ist.replace(second=0, microsecond=0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    rows = []
    for sym in PCR_INDICES:
        opts = [i for i in instruments
                if i.get("name") == sym and i.get("instrument_type") in ("CE", "PE")
                and i.get("expiry") is not None and i["expiry"] >= d]
        if not opts:
            continue
        keep = sorted({i["expiry"] for i in opts})[:NEAREST_EXPIRIES]
        sel = [i for i in opts if i["expiry"] in keep]
        exch = "BFO" if sym == "SENSEX" else "NFO"
        oi, vol = {}, {}
        for k in range(0, len(sel), 400):
            batch = sel[k:k + 400]
            try:
                q = kite.quote([f"{exch}:{i['tradingsymbol']}" for i in batch])
            except Exception as e:
                print(f"[pcr_intraday] {sym} quote batch failed: {e}")
                continue
            for i in batch:
                v = q.get(f"{exch}:{i['tradingsymbol']}")
                if v is not None:
                    oi[i["tradingsymbol"]] = int(v.get("oi", 0) or 0)
                    vol[i["tradingsymbol"]] = int(v.get("volume", 0) or v.get("volume_traded", 0) or 0)
            time.sleep(0.35)
        for e in keep:
            ce_s = [i for i in sel if i["expiry"] == e and i["instrument_type"] == "CE"]
            pe_s = [i for i in sel if i["expiry"] == e and i["instrument_type"] == "PE"]
            ce = sum(oi.get(i["tradingsymbol"], 0) for i in ce_s)
            pe = sum(oi.get(i["tradingsymbol"], 0) for i in pe_s)
            cv = sum(vol.get(i["tradingsymbol"], 0) for i in ce_s)
            pv = sum(vol.get(i["tradingsymbol"], 0) for i in pe_s)
            if ce <= 0 and pe <= 0:
                continue
            rows.append({"trade_date": d.isoformat(), "ts": ts.isoformat(), "symbol": sym, "expiry": e.isoformat(),
                         "ce_oi": ce, "pe_oi": pe, "ce_vol": cv, "pe_vol": pv,
                         "pcr": round(pe / ce, 4) if ce else None,
                         "vol_pcr": round(pv / cv, 4) if cv else None,
                         "strikes": len({i["strike"] for i in sel if i["expiry"] == e})})
    if rows:
        supabase.table("pcr_intraday").upsert(rows, on_conflict="ts,symbol,expiry").execute()
    print(f"[pcr_intraday] {ts.strftime('%H:%M')}: saved {len(rows)} rows")
    return {"saved": len(rows)}
