"""
delivery_confluence.py
Delivery + FUT OI confluence scoring for all 70 F&O stocks.
"""
from datetime import datetime, timedelta
from collections import defaultdict
import time as _time

_confluence_cache: dict = {}
_confluence_cache_time: float = 0
_CONFLUENCE_TTL = 3600  # 1 hour post-market

def get_delivery_confluence(supabase):
    global _confluence_cache, _confluence_cache_time

    if _confluence_cache and (_time.time() - _confluence_cache_time) < _CONFLUENCE_TTL:
        return _confluence_cache

    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()

    check = today
    for _ in range(7):
        if check.weekday() < 5:
            break
        check -= timedelta(days=1)
    last_trading_day = check.isoformat()

    # Invalidate cache if date changed
    if _confluence_cache.get("date") and _confluence_cache["date"] != last_trading_day:
        _confluence_cache = {}
        _confluence_cache_time = 0

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

    sym_delivery = defaultdict(list)
    for r in del_rows:
        sym_delivery[r["symbol"]].append({
            "date": r["trade_date"],
            "pct": float(r["delivery_pct"] or 0)
        })

    try:
        fut_res = supabase.from_("daily_oi_summary")\
            .select("symbol, fut_signal, fut_oi_chg_pct, price_chg_pct, close_price")\
            .eq("trade_date", last_trading_day)\
            .execute()
        fut_map = {r["symbol"]: r for r in (fut_res.data or [])}
    except Exception as e:
        print(f"[DelivConf] FUT fetch failed: {e}")
        fut_map = {}

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

        if today_pct >= 70:      del_score = 30
        elif today_pct >= 60:    del_score = 20
        elif today_pct >= 50:    del_score = 10
        elif today_pct >= 40:    del_score = 5
        else:                    del_score = 0

        last_5 = [h["pct"] for h in history[-5:]]
        if len(last_5) >= 3:
            avg_first_half  = sum(last_5[:len(last_5)//2]) / (len(last_5)//2)
            avg_second_half = sum(last_5[len(last_5)//2:]) / (len(last_5) - len(last_5)//2)
            trend_diff = avg_second_half - avg_first_half
            if trend_diff >= 5:     trend_score, trend_label = 25, "Rising ↑"
            elif trend_diff >= 2:   trend_score, trend_label = 15, "Mild Rise ↗"
            elif trend_diff <= -5:  trend_score, trend_label = 0,  "Falling ↓"
            elif trend_diff <= -2:  trend_score, trend_label = 5,  "Mild Fall ↘"
            else:                   trend_score, trend_label = 10, "Stable →"
        else:
            trend_score, trend_label = 10, "Stable →"

        bullish_signals = {"LONG_BUILDUP", "SHORT_COVERING"}
        bearish_signals = {"SHORT_BUILDUP", "LONG_UNWINDING"}

        if fut_signal in bullish_signals and today_pct >= 55:
            align_score, confluence_type, confluence_label = 30, "BULLISH", "🐂 Bullish Confluence"
        elif fut_signal in bearish_signals and today_pct >= 55:
            align_score, confluence_type, confluence_label = 30, "BEARISH", "🐻 Bearish Conviction"
        elif fut_signal in bullish_signals and today_pct >= 40:
            align_score, confluence_type, confluence_label = 15, "MILD_BULLISH", "📈 Mild Bullish"
        elif fut_signal in bearish_signals and today_pct >= 40:
            align_score, confluence_type, confluence_label = 15, "MILD_BEARISH", "📉 Mild Bearish"
        elif fut_signal == "NEUTRAL" and today_pct >= 65:
            align_score, confluence_type, confluence_label = 20, "STEALTH", "🕵️ Stealth Accumulation"
        elif fut_signal == "NEUTRAL":
            align_score, confluence_type, confluence_label = 5, "NEUTRAL", "⚪ Neutral"
        else:
            align_score, confluence_type, confluence_label = 0, "WEAK", "⚠️ Weak Signal"

        abs_oi = abs(fut_oi_chg)
        if abs_oi >= 15:      oi_score = 15
        elif abs_oi >= 10:    oi_score = 12
        elif abs_oi >= 5:     oi_score = 8
        elif abs_oi >= 2:     oi_score = 4
        else:                 oi_score = 0

        total_score = del_score + trend_score + align_score + oi_score

        if total_score >= 85:    grade, grade_color = "A+", "GOLD"
        elif total_score >= 70:  grade, grade_color = "A",  "EMERALD"
        elif total_score >= 55:  grade, grade_color = "B",  "AMBER"
        elif total_score >= 40:  grade, grade_color = "C",  "GRAY"
        else:                    grade, grade_color = "D",  "RED"

        if fut_signal == "NEUTRAL" and today_pct < 65:
            continue
        if total_score < 40:
            continue
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

    results.sort(key=lambda x: (-x["score"], 0 if "BULLISH" in x["confluence_type"] else 1))

    bullish = [r for r in results if "BULLISH" in r["confluence_type"]]
    bearish = [r for r in results if "BEARISH" in r["confluence_type"]]
    stealth = [r for r in results if r["confluence_type"] == "STEALTH"]
    top_bullish_combined = sorted(bullish + stealth, key=lambda x: -x["score"])

    result = {
        "date":        last_trading_day,
        "total":       len(results),
        "bullish":     len(bullish),
        "bearish":     len(bearish),
        "stealth":     len(stealth),
        "results":     results,
        "top_bullish": top_bullish_combined[:5],
        "top_bearish": bearish[:5],
    }

    _confluence_cache = result
    _confluence_cache_time = _time.time()
    print(f"[DelivConf] Computed and cached {len(results)} signals for {last_trading_day}")
    return result
