"""
delivery_confluence.py
Delivery + FUT OI confluence scoring for all 70 F&O stocks.
Combines:
  - Today's delivery %
  - 5-day delivery trend (rising = accumulation)
  - FUT signal alignment
  - FUT OI change strength
"""
from datetime import datetime, timedelta
from collections import defaultdict

def get_delivery_confluence(supabase):
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()

    # Get last trading day
    check = today
    for _ in range(7):
        if check.weekday() < 5:
            break
        check -= timedelta(days=1)
    last_trading_day = check.isoformat()

    # ── Fetch last 10 days delivery data ─────────────────────────────────────
    hist_start = (check - timedelta(days=20)).isoformat()
    try:
        del_res = supabase.from_("delivery_data")\
            .select("symbol, trade_date, delivery_pct")\
            .gte("trade_date", hist_start)\
            .lte("trade_date", last_trading_day)\
            .order("trade_date", desc=False)\
            .execute()
        del_rows = del_res.data or []
    except Exception as e:
        print(f"[DelivConf] Delivery fetch failed: {e}")
        return {"error": str(e), "results": []}

    # Group by symbol
    sym_delivery = defaultdict(list)
    for r in del_rows:
        sym_delivery[r["symbol"]].append({
            "date": r["trade_date"],
            "pct": float(r["delivery_pct"] or 0)
        })

    # ── Fetch today's FUT signals ─────────────────────────────────────────────
    try:
        fut_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_signal, fut_oi_chg_pct, price_chg_pct, close_price")\
            .eq("trade_date", last_trading_day)\
            .execute()
        fut_map = {r["symbol"]: r for r in (fut_res.data or [])}
    except Exception as e:
        print(f"[DelivConf] FUT fetch failed: {e}")
        fut_map = {}

    # ── Score each symbol ─────────────────────────────────────────────────────
    results = []

    for sym, history in sym_delivery.items():
        if len(history) < 3:
            continue

        today_row = next((h for h in reversed(history) if h["date"] == last_trading_day), None)
        if not today_row:
            continue

        today_pct = today_row["pct"]
        fut = fut_map.get(sym, {})
        fut_signal = fut.get("fut_signal") or "NEUTRAL"
        fut_oi_chg = float(fut.get("fut_oi_chg_pct") or 0)
        price_chg = float(fut.get("price_chg_pct") or 0)
        close_price = float(fut.get("close_price") or 0)

        # ── Component 1: Delivery % today (0-30) ─────────────────────────────
        if today_pct >= 70:
            del_score = 30
        elif today_pct >= 60:
            del_score = 20
        elif today_pct >= 50:
            del_score = 10
        elif today_pct >= 40:
            del_score = 5
        else:
            del_score = 0

        # ── Component 2: 5-day delivery trend (0-25) ─────────────────────────
        last_5 = [h["pct"] for h in history[-5:]]
        if len(last_5) >= 3:
            # Linear regression slope via simple diff
            avg_first_half = sum(last_5[:len(last_5)//2]) / (len(last_5)//2)
            avg_second_half = sum(last_5[len(last_5)//2:]) / (len(last_5) - len(last_5)//2)
            trend_diff = avg_second_half - avg_first_half
            if trend_diff >= 5:
                trend_score = 25
                trend_label = "Rising ↑"
            elif trend_diff >= 2:
                trend_score = 15
                trend_label = "Mild Rise ↗"
            elif trend_diff <= -5:
                trend_score = 0
                trend_label = "Falling ↓"
            elif trend_diff <= -2:
                trend_score = 5
                trend_label = "Mild Fall ↘"
            else:
                trend_score = 10
                trend_label = "Stable →"
        else:
            trend_score = 10
            trend_label = "Stable →"

        # ── Component 3: FUT + Delivery alignment (0-30) ─────────────────────
        bullish_signals = {"LONG_BUILDUP", "SHORT_COVERING"}
        bearish_signals = {"SHORT_BUILDUP", "LONG_UNWINDING"}

        if fut_signal in bullish_signals and today_pct >= 55:
            align_score = 30
            confluence_type = "BULLISH"
            confluence_label = "🐂 Bullish Confluence"
        elif fut_signal in bearish_signals and today_pct >= 55:
            align_score = 30
            confluence_type = "BEARISH"
            confluence_label = "🐻 Bearish Conviction"
        elif fut_signal in bullish_signals and today_pct >= 40:
            align_score = 15
            confluence_type = "MILD_BULLISH"
            confluence_label = "📈 Mild Bullish"
        elif fut_signal in bearish_signals and today_pct >= 40:
            align_score = 15
            confluence_type = "MILD_BEARISH"
            confluence_label = "📉 Mild Bearish"
        elif fut_signal == "NEUTRAL" and today_pct >= 65:
            align_score = 20  # High delivery with no price move = stealth
            confluence_type = "STEALTH"
            confluence_label = "🕵️ Stealth Accumulation"
        elif fut_signal == "NEUTRAL":
            align_score = 5
            confluence_type = "NEUTRAL"
            confluence_label = "⚪ Neutral"
        else:
            align_score = 0
            confluence_type = "WEAK"
            confluence_label = "⚠️ Weak Signal"

        # ── Component 4: FUT OI strength (0-15) ──────────────────────────────
        abs_oi = abs(fut_oi_chg)
        if abs_oi >= 15:
            oi_score = 15
        elif abs_oi >= 10:
            oi_score = 12
        elif abs_oi >= 5:
            oi_score = 8
        elif abs_oi >= 2:
            oi_score = 4
        else:
            oi_score = 0

        total_score = del_score + trend_score + align_score + oi_score

        # ── Grade ─────────────────────────────────────────────────────────────
        if total_score >= 85:
            grade = "A+"
            grade_color = "GOLD"
        elif total_score >= 70:
            grade = "A"
            grade_color = "EMERALD"
        elif total_score >= 55:
            grade = "B"
            grade_color = "AMBER"
        elif total_score >= 40:
            grade = "C"
            grade_color = "GRAY"
        else:
            grade = "D"
            grade_color = "RED"

        # Include NEUTRAL if delivery is very high (≥65%) — stealth accumulation
        if fut_signal == "NEUTRAL" and today_pct < 65:
            continue
        if total_score < 40:
            continue
        # For NEUTRAL signals, flag as stealth
        if fut_signal == "NEUTRAL":
            confluence_type = "STEALTH"
            confluence_label = "🕵️ Stealth Accumulation"

        results.append({
            "symbol":           sym,
            "close_price":      close_price,
            "score":            total_score,
            "grade":            grade,
            "grade_color":      grade_color,
            "confluence_type":  confluence_type,
            "confluence_label": confluence_label,
            "fut_signal":       fut_signal,
            "fut_oi_chg_pct":   round(fut_oi_chg, 2),
            "price_chg_pct":    round(price_chg, 2),
            "delivery_pct":     round(today_pct, 1),
            "delivery_trend":   trend_label,
            "delivery_5d":      [round(p, 1) for p in last_5],
            "breakdown": {
                "delivery_today": del_score,
                "delivery_trend": trend_score,
                "fut_alignment":  align_score,
                "oi_strength":    oi_score,
            }
        })

    # Sort by score desc, bullish first within same score
    results.sort(key=lambda x: (
        -x["score"],
        0 if "BULLISH" in x["confluence_type"] else 1
    ))

    bullish = [r for r in results if "BULLISH" in r["confluence_type"]]
    bearish = [r for r in results if "BEARISH" in r["confluence_type"]]
    stealth = [r for r in results if r["confluence_type"] == "STEALTH"]

    return {
        "date":     last_trading_day,
        "total":    len(results),
        "bullish":  len(bullish),
        "bearish":  len(bearish),
        "stealth":  len(stealth),
        "results":  results,
        "top_bullish": (bullish + stealth)[:5],
        "top_bearish": bearish[:5],
    }
