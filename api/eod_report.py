"""
eod_report.py - Premium EOD Intelligence Report
Aggregates daily_oi_summary, iv_history, participant_flow for a given date.
"""
from datetime import datetime, timezone, timedelta

def get_eod_report(supabase, date: str = None):
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)

    # Default to most recent available report date
    if not date:
        try:
            last_res = supabase.from_("daily_oi_summary")\
                .select("trade_date")\
                .order("trade_date", desc=True)\
                .limit(1)\
                .execute()
            date = last_res.data[0]["trade_date"] if last_res.data else now_ist.date().isoformat()
        except:
            date = now_ist.date().isoformat()

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
        if r.get("price_chg_pct") is None:
            continue  # Skip new stocks with no previous day close
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
    _existing_cash = None
    try:
        _existing_res = supabase.from_("fii_dii_cash").select("*").eq("trade_date", date).limit(1).execute()
        if _existing_res.data and _existing_res.data[0].get("fii_net") is not None:
            _existing_cash = _existing_res.data[0]
    except:
        pass

    if date == _today and _existing_cash:
        # Already have manually-verified or previously-scraped data — don't overwrite
        r = _existing_cash
        cash_data["FII"] = {"buy": float(r["fii_buy"] or 0), "sell": float(r["fii_sell"] or 0), "net": float(r["fii_net"] or 0)}
        cash_data["DII"] = {"buy": float(r["dii_buy"] or 0), "sell": float(r["dii_sell"] or 0), "net": float(r["dii_net"] or 0)}
    elif date == _today:
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
                # Save to Supabase for historical access
                if cash_data.get('FII') or cash_data.get('DII'):
                    try:
                        supabase.from_("fii_dii_cash").upsert({
                            "trade_date": date,
                            "fii_buy":  cash_data.get('FII', {}).get('buy'),
                            "fii_sell": cash_data.get('FII', {}).get('sell'),
                            "fii_net":  cash_data.get('FII', {}).get('net'),
                            "dii_buy":  cash_data.get('DII', {}).get('buy'),
                            "dii_sell": cash_data.get('DII', {}).get('sell'),
                            "dii_net":  cash_data.get('DII', {}).get('net'),
                        }).execute()
                        print(f"[EOD] Cash saved to Supabase for {date}")
                    except Exception as se:
                        print(f"[EOD] Cash save failed: {se}")
        except Exception as e:
            print(f"[EOD] Cash fetch failed: {e}")
    else:
        # Try loading from Supabase for historical dates
        try:
            saved = supabase.from_("fii_dii_cash").select("*").eq("trade_date", date).limit(1).execute()
            if saved.data:
                r = saved.data[0]
                if r.get("fii_net") is not None:
                    cash_data["FII"] = {"buy": float(r["fii_buy"] or 0), "sell": float(r["fii_sell"] or 0), "net": float(r["fii_net"] or 0)}
                if r.get("dii_net") is not None:
                    cash_data["DII"] = {"buy": float(r["dii_buy"] or 0), "sell": float(r["dii_sell"] or 0), "net": float(r["dii_net"] or 0)}
                print(f"[EOD] Cash loaded from Supabase for {date}")
        except Exception as e:
            print(f"[EOD] Cash load failed: {e}")

    # ── 7. Delivery data ─────────────────────────────────────────────────────
    try:
        del_res = supabase.from_("delivery_data")\
            .select("symbol, delivery_pct, deliverable_qty")\
            .eq("trade_date", date)\
            .order("delivery_pct", desc=True)\
            .execute()
        delivery_rows = del_res.data or []
        # Enrich stealth with delivery %
        delivery_map = {r["symbol"]: float(r["delivery_pct"] or 0) for r in delivery_rows}
        for s in stealth:
            s["delivery_pct"] = delivery_map.get(s["symbol"], None)
        # High delivery stocks (≥60%) for EOD section
        high_delivery = [
            {
                "symbol": r["symbol"],
                "delivery_pct": float(r["delivery_pct"] or 0),
                "deliverable_L": round(int(r["deliverable_qty"] or 0) / 100000, 1),
            }
            for r in delivery_rows if float(r["delivery_pct"] or 0) >= 60
        ][:10]
        avg_delivery = round(sum(float(r["delivery_pct"] or 0) for r in delivery_rows) / len(delivery_rows), 1) if delivery_rows else None
    except Exception as e:
        print(f"[EOD] Delivery fetch failed: {e}")
        high_delivery = []
        avg_delivery = None
        for s in stealth:
            s["delivery_pct"] = None

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
        "delivery": {
            "high_delivery": high_delivery,
            "avg_delivery_pct": avg_delivery,
        },
    }
