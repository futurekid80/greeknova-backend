from utils.db import get_supabase
from datetime import datetime, timezone, timedelta

SYMBOLS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY",
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","BAJFINANCE","WIPRO","HCLTECH","TATACONSUM",
    "TATASTEEL","ADANIENT","POWERGRID","NTPC","ONGC","JSWSTEEL","COALINDIA",
    "BAJAJFINSV","TECHM","APOLLOHOSP","BAJAJ-AUTO","BPCL","BRITANNIA","CIPLA",
    "DRREDDY","EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","HDFCLIFE",
    "INDUSINDBK","JIOFIN","M&M","NESTLEIND","SBILIFE","SHRIRAMFIN","TRENT",
    "ADANIPORTS","BANKBARODA","BEL","CANBK","CHOLAFIN","DLF","GAIL","HAVELLS",
    "HAL","INDIGO","PFC","RECLTD","SAIL","TATAPOWER","VEDL",
]


def count_consecutive(series: list, direction: str) -> int:
    count = 0
    for i in range(len(series) - 1, 0, -1):
        if direction == 'up' and series[i] > series[i - 1]:
            count += 1
        elif direction == 'down' and series[i] < series[i - 1]:
            count += 1
        else:
            break
    return count


def get_positional_radar(days: int = 5):
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()

    # Find last N+1 trading dates
    trading_dates = []
    for i in range(90):
        d = (today - timedelta(days=i)).isoformat()
        check = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .limit(1).execute()
        if check.data:
            trading_dates.append(d)
        if len(trading_dates) >= days + 1:
            break

    if len(trading_dates) < 2:
        return {"error": "Not enough trading days data", "results": []}

    analysis_dates = list(reversed(trading_dates[:days + 1]))

    oi_by_date:  dict = {}
    vol_by_date: dict = {}
    cmp_by_date: dict = {}

    for d in analysis_dates:
        ts_q = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(1).execute()

        if not ts_q.data:
            continue

        eod_ts = ts_q.data[0]["timestamp"]

        raw = []
        for offset in range(0, 100000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("symbol, oi, volume")\
                .eq("timestamp", eod_ts)\
                .range(offset, offset + 999).execute()
            if not batch.data:
                break
            raw.extend(batch.data)
            if len(batch.data) < 1000:
                break

        sym_oi:  dict = {}
        sym_vol: dict = {}
        for r in raw:
            sym = r["symbol"]
            sym_oi[sym]  = sym_oi.get(sym, 0)  + (r["oi"]     or 0)
            sym_vol[sym] = sym_vol.get(sym, 0) + (r["volume"] or 0)

        oi_by_date[d]  = sym_oi
        vol_by_date[d] = sym_vol

        cmp_q = supabase.from_("cmp_prices")\
            .select("symbol, cmp")\
            .gte("timestamp", f"{d}T00:00:00+00:00")\
            .lt("timestamp",  f"{d}T23:59:59+00:00")\
            .order("timestamp", desc=True)\
            .limit(500).execute().data or []

        day_cmp: dict = {}
        seen: set = set()
        for r in cmp_q:
            if r["symbol"] not in seen:
                day_cmp[r["symbol"]] = float(r["cmp"])
                seen.add(r["symbol"])
        cmp_by_date[d] = day_cmp

    available_dates = [d for d in analysis_dates if d in oi_by_date]
    if len(available_dates) < 2:
        return {"error": "Insufficient data points", "results": []}

    results = []

    for sym in SYMBOLS:
        oi_series  = []
        vol_series = []
        cmp_series = []
        date_labels = []

        for d in available_dates:
            oi_val  = oi_by_date.get(d, {}).get(sym, 0)
            vol_val = vol_by_date.get(d, {}).get(sym, 0)
            cmp_val = cmp_by_date.get(d, {}).get(sym, 0)
            if oi_val > 0 and cmp_val > 0:
                oi_series.append(oi_val)
                vol_series.append(vol_val)
                cmp_series.append(cmp_val)
                date_labels.append(d)

        if len(oi_series) < 2:
            continue

        actual_days = len(oi_series) - 1

        oi_chg_pct  = round((oi_series[-1]  - oi_series[0])  / oi_series[0]  * 100, 2) if oi_series[0]  > 0 else 0
        vol_chg_pct = round((vol_series[-1] - vol_series[0]) / vol_series[0] * 100, 2) if vol_series[0] > 0 else 0
        cmp_chg_pct = round((cmp_series[-1] - cmp_series[0]) / cmp_series[0] * 100, 2) if cmp_series[0] > 0 else 0

        oi_consec_up   = count_consecutive(oi_series,  'up')
        oi_consec_down = count_consecutive(oi_series,  'down')
        vol_consec_up  = count_consecutive(vol_series, 'up')
        cmp_consec_up  = count_consecutive(cmp_series, 'up')
        cmp_consec_down= count_consecutive(cmp_series, 'down')

        oi_rising    = oi_chg_pct  >  2.0
        oi_falling   = oi_chg_pct  < -2.0
        vol_rising   = vol_chg_pct >  5.0
        price_rising = cmp_chg_pct >  0.5
        price_falling= cmp_chg_pct < -0.5

        if oi_rising and price_rising:
            signal = "LONG_BUILDUP"
            bias   = "BULLISH"
            consec = min(oi_consec_up, cmp_consec_up)
        elif oi_rising and price_falling:
            signal = "SHORT_BUILDUP"
            bias   = "BEARISH"
            consec = min(oi_consec_up, cmp_consec_down)
        elif oi_falling and price_rising:
            signal = "SHORT_COVERING"
            bias   = "BULLISH"
            consec = min(oi_consec_down, cmp_consec_up)
        elif oi_falling and price_falling:
            signal = "LONG_UNWINDING"
            bias   = "BEARISH"
            consec = min(oi_consec_down, cmp_consec_down)
        else:
            continue

        if consec >= max(2, actual_days - 1):
            conviction = "HIGH"
        elif consec >= 2:
            conviction = "MEDIUM"
        else:
            conviction = "LOW"

        triple_confirm = (
            signal == "LONG_BUILDUP" and
            vol_rising and
            vol_consec_up >= 2
        )

        results.append({
            "symbol":          sym,
            "is_index":        sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "signal":          signal,
            "bias":            bias,
            "conviction":      conviction,
            "consec_days":     consec,
            "actual_days":     actual_days,
            "triple_confirm":  triple_confirm,
            "oi_chg_pct":      oi_chg_pct,
            "vol_chg_pct":     vol_chg_pct,
            "cmp_chg_pct":     cmp_chg_pct,
            "oi_consec_up":    oi_consec_up,
            "oi_consec_down":  oi_consec_down,
            "vol_consec_up":   vol_consec_up,
            "cmp_consec_up":   cmp_consec_up,
            "cmp_consec_down": cmp_consec_down,
            "oi_series":       [round(x / 100000, 1) for x in oi_series],
            "vol_series":      [round(x / 100000, 1) for x in vol_series],
            "cmp_series":      cmp_series,
            "date_labels":     date_labels,
            "cmp":             cmp_series[-1],
            "oi_now":          oi_series[-1],
            "vol_now":         vol_series[-1],
        })

    results.sort(key=lambda x: (
        0 if x["triple_confirm"] else 1,
        {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["conviction"]],
        -abs(x["oi_chg_pct"])
    ))

    summary = {
        "long_buildup":   sum(1 for r in results if r["signal"] == "LONG_BUILDUP"),
        "short_buildup":  sum(1 for r in results if r["signal"] == "SHORT_BUILDUP"),
        "short_covering": sum(1 for r in results if r["signal"] == "SHORT_COVERING"),
        "long_unwinding": sum(1 for r in results if r["signal"] == "LONG_UNWINDING"),
        "high_conviction":sum(1 for r in results if r["conviction"] == "HIGH"),
        "triple_confirm": sum(1 for r in results if r["triple_confirm"]),
    }

    return {
        "days":           days,
        "dates_analyzed": available_dates,
        "from_date":      available_dates[0]  if available_dates else "",
        "to_date":        available_dates[-1] if available_dates else "",
        "total":          len(results),
        "summary":        summary,
        "results":        results,
    }
