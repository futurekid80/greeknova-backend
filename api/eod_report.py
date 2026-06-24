"""
eod_report.py - Premium EOD Intelligence Report
Aggregates daily_oi_summary, iv_history, participant_flow for a given date.
"""
from datetime import datetime, timezone, timedelta

def get_eod_report(supabase, date: str = None):
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)

    # Default to most recent trading day
    if not date:
        d = now_ist.date()
        # If before 4:30 PM, use previous trading day
        if now_ist.hour < 16 or (now_ist.hour == 16 and now_ist.minute < 30):
            d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        date = d.isoformat()

    # ── 1. FUT OI Movers ─────────────────────────────────────────────────────
    try:
        movers_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_oi_chg_pct, price_chg_pct, fut_signal, close_price, fut_vol, ce_oi, pe_oi")\
            .eq("trade_date", date)\
            .not_.is_("fut_signal", "null")\
            .neq("fut_signal", "NEUTRAL")\
            .limit(200)\
            .execute()
        all_movers = movers_res.data or []
    except Exception as e:
        print(f"[EOD] Movers fetch failed: {e}")
        all_movers = []

    long_buildup = sorted(
        [r for r in all_movers if r.get("fut_signal") == "LONG_BUILDUP"],
        key=lambda x: float(x.get("fut_oi_chg_pct") or 0), reverse=True
    )[:5]

    short_buildup = sorted(
        [r for r in all_movers if r.get("fut_signal") == "SHORT_BUILDUP"],
        key=lambda x: float(x.get("fut_oi_chg_pct") or 0), reverse=True
    )[:5]

    short_covering = sorted(
        [r for r in all_movers if r.get("fut_signal") == "SHORT_COVERING"],
        key=lambda x: abs(float(x.get("fut_oi_chg_pct") or 0)), reverse=True
    )[:3]

    long_unwinding = sorted(
        [r for r in all_movers if r.get("fut_signal") == "LONG_UNWINDING"],
        key=lambda x: abs(float(x.get("fut_oi_chg_pct") or 0)), reverse=True
    )[:3]

    # ── 2. Market Breadth ─────────────────────────────────────────────────────
    try:
        breadth_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_signal")\
            .eq("trade_date", date)\
            .limit(200)\
            .execute()
        breadth_data = breadth_res.data or []
    except:
        breadth_data = []

    signal_counts = {"LONG_BUILDUP": 0, "SHORT_BUILDUP": 0,
                     "SHORT_COVERING": 0, "LONG_UNWINDING": 0, "NEUTRAL": 0}
    for r in breadth_data:
        sig = r.get("fut_signal") or "NEUTRAL"
        signal_counts[sig] = signal_counts.get(sig, 0) + 1

    total_symbols = len(breadth_data)
    bullish = signal_counts["LONG_BUILDUP"] + signal_counts["SHORT_COVERING"]
    bearish = signal_counts["SHORT_BUILDUP"] + signal_counts["LONG_UNWINDING"]
    neutral = signal_counts["NEUTRAL"]

    # ── 3. Stealth Buildup EOD ────────────────────────────────────────────────
    try:
        stealth_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_oi_chg_pct, price_chg_pct, close_price, fut_signal")\
            .eq("trade_date", date)\
            .gte("fut_oi_chg_pct", 1.5)\
            .limit(200)\
            .execute()
        stealth_raw = stealth_res.data or []
    except:
        stealth_raw = []

    stealth = []
    for r in stealth_raw:
        oi = float(r.get("fut_oi_chg_pct") or 0)
        price = float(r.get("price_chg_pct") or 0)
        if oi >= 2.0 and abs(price) <= 0.5:
            tier = "ELITE"
        elif oi >= 1.5 and abs(price) <= 1.0:
            tier = "STRONG"
        else:
            continue
        stealth.append({
            "symbol": r["symbol"],
            "fut_oi_chg_pct": round(oi, 2),
            "price_chg_pct": round(price, 2),
            "close_price": float(r.get("close_price") or 0),
            "tier": tier,
        })
    stealth = sorted(stealth, key=lambda x: (
        0 if x["tier"] == "ELITE" else 1, -x["fut_oi_chg_pct"]
    ))[:8]

    # ── 4. IV History (indices) ───────────────────────────────────────────────
    try:
        iv_res = supabase.from_("iv_history")\
            .select("symbol, atm_iv, atm_ce_iv, atm_pe_iv, dte")\
            .eq("trade_date", date)\
            .in_("symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY"])\
            .execute()
        iv_data = {r["symbol"]: r for r in (iv_res.data or [])}
    except:
        iv_data = {}

    # ── 5. Participant Flow ───────────────────────────────────────────────────
    try:
        pf_res = supabase.from_("participant_flow")\
            .select("*")\
            .eq("trade_date", date)\
            .execute()
        pf_data = {r["participant"]: r for r in (pf_res.data or [])}
    except:
        pf_data = {}

    # ── 6. Series Buildup snapshot ────────────────────────────────────────────
    try:
        series_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_signal, fut_oi_chg_pct, price_chg_pct, close_price")\
            .eq("trade_date", date)\
            .in_("fut_signal", ["LONG_BUILDUP", "SHORT_BUILDUP"])\
            .gte("fut_oi_chg_pct", 2.0)\
            .order("fut_oi_chg_pct", desc=True)\
            .limit(10)\
            .execute()
        top_signals = series_res.data or []
    except:
        top_signals = []

   # ── 6b. FII/DII Cash market data from NSE (today only) ───────────────────
    cash_data = {}
    import pytz as _pytz
    _ist = _pytz.timezone("Asia/Kolkata")
    _today = datetime.now(_ist).strftime('%Y-%m-%d')
    if date == _today:
        try:
            import requests
            res = requests.get(
                "https://www.nseindia.com/api/fiidiiTradeReact",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Referer": "https://www.nseindia.com/",
                },
                timeout=4
            )
            print(f"[EOD] NSE cash status: {res.status_code}")
            if res.status_code == 200:
                for row in res.json():
                    cat = str(row.get("category") or "").strip()
                    if "FII" in cat or "FPI" in cat:
                        cash_data["FII"] = {"buy": float(row.get("buyValue", 0)), "sell": float(row.get("sellValue", 0)), "net": float(row.get("netValue", 0))}
                    elif "DII" in cat:
                        cash_data["DII"] = {"buy": float(row.get("buyValue", 0)), "sell": float(row.get("sellValue", 0)), "net": float(row.get("netValue", 0))}
                print(f"[EOD] Cash: FII={cash_data.get('FII',{}).get('net')} DII={cash_data.get('DII',{}).get('net')}")
        except Exception as e:
            print(f"[EOD] Cash fetch failed: {e}")
    else:
        print(f"[EOD] Skipping NSE cash for historical date {date}")

    # ── 7. Available dates for date picker ────────────────────────────────────

    # ── 7. Available dates for date picker ────────────────────────────────────
    try:
        dates_res = supabase.from_("daily_oi_summary")\
            .select("trade_date")\
            .eq("symbol", "NIFTY")\
            .order("trade_date", desc=True)\
            .limit(30)\
            .execute()
        available_dates = [r["trade_date"] for r in (dates_res.data or [])]
    except:
        available_dates = [date]

    def fmt_signal(r):
        return {
            "symbol": r.get("symbol"),
            "fut_oi_chg_pct": round(float(r.get("fut_oi_chg_pct") or 0), 2),
            "price_chg_pct": round(float(r.get("price_chg_pct") or 0), 2),
            "close_price": float(r.get("close_price") or 0),
            "fut_signal": r.get("fut_signal"),
            "fut_vol": int(r.get("fut_vol") or 0),
        }

    return {
        "date": date,
        "available_dates": available_dates,
        "market_breadth": {
            "total": total_symbols,
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "long_buildup": signal_counts["LONG_BUILDUP"],
            "short_buildup": signal_counts["SHORT_BUILDUP"],
            "short_covering": signal_counts["SHORT_COVERING"],
            "long_unwinding": signal_counts["LONG_UNWINDING"],
        },
        "fut_movers": {
            "long_buildup": [fmt_signal(r) for r in long_buildup],
            "short_buildup": [fmt_signal(r) for r in short_buildup],
            "short_covering": [fmt_signal(r) for r in short_covering],
            "long_unwinding": [fmt_signal(r) for r in long_unwinding],
        },
        "stealth_buildup": stealth,
        "iv_data": iv_data,
        "participant_flow": pf_data,
        "top_signals": [fmt_signal(r) for r in top_signals],
        "cash_flow": cash_data,
    }
