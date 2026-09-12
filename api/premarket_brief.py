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
from concurrent.futures import ThreadPoolExecutor


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
    # PERF FIX (Sep 12 2026): these 4 yfinance calls used to run one after
    # another -- each is its own blocking network round trip to Yahoo
    # Finance, so this alone could take several seconds. They're
    # independent of each other, so fetch them concurrently instead.
    def _fetch_one(item):
        name, ticker = item
        try:
            t = yf.Ticker(ticker)
            info = dict(t.fast_info)
            ltp = info.get("lastPrice")
            prev_close = info.get("previousClose")
            if ltp is None or prev_close is None:
                return None
            change = round(ltp - prev_close, 2)
            pct = round(change / prev_close * 100, 2) if prev_close else 0
            return {
                "name": name,
                "ticker": ticker,
                "ltp": round(ltp, 2),
                "change": change,
                "pct_change": pct,
            }
        except Exception as e:
            print(f"[Premarket] Commodity fetch failed for {name} ({ticker}): {e}")
            return None

    order = list(tickers.keys())
    with ThreadPoolExecutor(max_workers=len(tickers)) as ex:
        fetched = dict(zip(order, ex.map(_fetch_one, tickers.items())))
    return [fetched[name] for name in order if fetched[name] is not None]


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

    # PERF FIX (Sep 12 2026): GIFT Nifty, commodities, EOD-report reuse,
    # positional-intelligence reuse, and index levels are five independent
    # data pulls that used to run strictly one after another -- each
    # waiting on the last even though none of them depend on each other's
    # result. This was the main reason Pre-Market Report was slow to load.
    # Run them concurrently instead.

    def _fetch_fii_dii():
        try:
            from api.eod_report import get_eod_report
            eod = get_eod_report(supabase, last_trading_day)
            cash_flow = eod.get("cash_flow", {})
            fii_dii = None
            if cash_flow.get("FII") or cash_flow.get("DII"):
                fii_dii = {
                    "fii_net": cash_flow.get("FII", {}).get("net"),
                    "dii_net": cash_flow.get("DII", {}).get("net"),
                    "date": last_trading_day,
                }
            high_delivery = eod.get("delivery", {}).get("high_delivery", [])[:8]
            return fii_dii, high_delivery
        except Exception as e:
            print(f"[Premarket] EOD report reuse failed: {e}")
            return None, []

    def _fetch_overnight_conviction():
        try:
            from api.positional_intelligence import get_positional_intelligence
            pi = get_positional_intelligence(min_consec=2)
            return [
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
            return []

    def _fetch_index_levels():
        index_levels = []
        try:
            from api.max_pain import get_max_pain_all
            from api.oi_profile import get_oi_profile
            mp = get_max_pain_all()
            indices = [s for s in (mp.get("symbols") or []) if s.get("is_index")]

            def _with_profile(idx):
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
                return entry

            if indices:
                with ThreadPoolExecutor(max_workers=len(indices)) as ex:
                    index_levels = list(ex.map(_with_profile, indices))
        except Exception as e:
            print(f"[Premarket] Index levels fetch failed: {e}")
        return index_levels

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_gift        = ex.submit(_get_gift_nifty)
        f_commodities = ex.submit(_get_commodities)
        f_fii_dii     = ex.submit(_fetch_fii_dii)
        f_conviction  = ex.submit(_fetch_overnight_conviction)
        f_levels      = ex.submit(_fetch_index_levels)

        gift_nifty = f_gift.result()
        commodities = f_commodities.result()
        fii_dii, high_delivery = f_fii_dii.result()
        overnight_conviction = f_conviction.result()
        index_levels = f_levels.result()

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
