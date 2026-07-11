"""
premarket_brief.py
Simple, focused pre-market page — NOT a mini EOD report. Six things a trader
actually wants before 9:15 AM: GIFT Nifty (gap indicator), global commodities,
yesterday's FII/DII flow, overnight carry-forward conviction, index key levels,
and high-delivery stocks from yesterday.

GIFT Nifty is fetched via Kite's NSEIX exchange segment (NOT the regular NSE/BSE/
NFO/CDS/BCD/MCX segments — this is Zerodha's separate GIFT City/NSE IX access,
easy to miss since it's not in the standard instrument dump).

Commodities use yfinance — free, no API key, same approach used in a prior
project (MCIS). Worth remembering this is an unofficial/free source, not a
licensed institutional feed — fine for a quick glance, not for anything mission
critical.
"""
from datetime import datetime, timedelta


def _get_gift_nifty():
    try:
        from services.kite_auth import get_kite_client
        kite = get_kite_client()
        q = kite.quote(["NSEIX:GIFT NIFTY"])
        d = q.get("NSEIX:GIFT NIFTY")
        if not d:
            return None
        ltp = d.get("last_price", 0)
        prev_close = d.get("ohlc", {}).get("close", 0)
        change = round(ltp - prev_close, 2) if prev_close else 0
        pct = round(change / prev_close * 100, 2) if prev_close else 0
        return {
            "ltp": ltp,
            "prev_close": prev_close,
            "change": change,
            "pct_change": pct,
            "direction": "UP" if change > 0 else "DOWN" if change < 0 else "FLAT",
        }
    except Exception as e:
        print(f"[Premarket] GIFT Nifty fetch failed: {e}")
        return None


def _get_commodities():
    try:
        import yfinance as yf
    except ImportError:
        print("[Premarket] yfinance not installed — skipping commodities")
        return []

    tickers = {
        "Gold": "GC=F",
        "Silver": "SI=F",
        "Crude (Brent)": "BZ=F",
        "Crude (WTI)": "CL=F",
    }
    results = []
    for name, ticker in tickers.items():
        try:
            t = yf.Ticker(ticker)
            info = dict(t.fast_info)
            ltp = info.get("lastPrice")
            prev_close = info.get("previousClose")
            if ltp is None or prev_close is None:
                continue
            change = round(ltp - prev_close, 2)
            pct = round(change / prev_close * 100, 2) if prev_close else 0
            results.append({
                "name": name,
                "ticker": ticker,
                "ltp": round(ltp, 2),
                "change": change,
                "pct_change": pct,
            })
        except Exception as e:
            print(f"[Premarket] Commodity fetch failed for {name} ({ticker}): {e}")
    return results


def get_premarket_brief(supabase) -> dict:
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today = now_ist.date()

    from utils.market_calendar import is_trading_day
    check = today
    if not is_trading_day(check):
        check -= timedelta(days=1)
        while not is_trading_day(check):
            check -= timedelta(days=1)
    last_trading_day = check.isoformat()

    gift_nifty = _get_gift_nifty()
    commodities = _get_commodities()

    fii_dii = None
    high_delivery = []
    try:
        from api.eod_report import get_eod_report
        eod = get_eod_report(supabase, last_trading_day)
        cash_flow = eod.get("cash_flow", {})
        if cash_flow.get("FII") or cash_flow.get("DII"):
            fii_dii = {
                "fii_net": cash_flow.get("FII", {}).get("net"),
                "dii_net": cash_flow.get("DII", {}).get("net"),
                "date": last_trading_day,
            }
        high_delivery = eod.get("delivery", {}).get("high_delivery", [])[:8]
    except Exception as e:
        print(f"[Premarket] EOD report reuse failed: {e}")

    overnight_conviction = []
    try:
        from api.positional_intelligence import get_positional_intelligence
        pi = get_positional_intelligence(min_consec=2)
        overnight_conviction = [
            {
                "symbol": r["symbol"],
                "cmp": r["cmp"],
                "signal": r["signal"],
                "consec_days": r["consec_days"],
                "consistency_pct": r["consistency_pct"],
                "cpr_position": r.get("cpr_position"),
            }
            for r in (pi.get("active_conviction") or [])
        ][:8]
    except Exception as e:
        print(f"[Premarket] Positional intelligence reuse failed: {e}")

    index_levels = []
    try:
        from api.max_pain import get_max_pain_all
        from api.oi_profile import get_oi_profile
        mp = get_max_pain_all()
        indices = [s for s in (mp.get("symbols") or []) if s.get("is_index")]
        for idx in indices:
            sym = idx["symbol"]
            entry = {
                "symbol": sym,
                "cmp": idx["cmp"],
                "pcr": idx["pcr"],
                "max_pain": idx["max_pain"],
                "dist_from_mp": idx["dist_from_mp"],
                "days_to_expiry": idx["days_to_expiry"],
                "ce_wall": None,
                "pe_wall": None,
            }
            try:
                profile = get_oi_profile(sym)
                entry["ce_wall"] = profile.get("ce_wall")
                entry["pe_wall"] = profile.get("pe_wall")
            except Exception as e:
                print(f"[Premarket] OI profile failed for {sym}: {e}")
            index_levels.append(entry)
    except Exception as e:
        print(f"[Premarket] Index levels fetch failed: {e}")

    return {
        "date": last_trading_day,
        "generated_at": now_ist.strftime("%H:%M IST"),
        "gift_nifty": gift_nifty,
        "commodities": commodities,
        "fii_dii": fii_dii,
        "high_delivery": high_delivery,
        "overnight_conviction": overnight_conviction,
        "index_levels": index_levels,
    }
