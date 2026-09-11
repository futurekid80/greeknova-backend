"""
CAS Indicative Closing Price capture.

During SEBI's Closing Auction Session (15:15-15:35 IST), NSE runs a call
auction: buy/sell orders accumulate and the price that would currently
match the maximum quantity is the "indicative close" -- the same number
Zerodha shows (blinking) on Kite web during this window.

NSE doesn't broadcast this as a single field; it's derived from the live
order book. We approximate it every capture tick from Kite's standard
5-level market depth (same depth quote() already returns elsewhere in
this codebase) using the same equilibrium-price logic used for the
9:00-9:08 pre-open session: the price level that maximizes matched
buy/sell quantity across the visible book.

Note: with only 5 depth levels (not the full order book NSE itself sees),
this is an approximation -- same limitation any broker-side depth-based
indicative price has. It gets more accurate as the auction progresses and
the book concentrates near the equilibrium price.
"""
import os
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

CAS_START = (15, 15)   # 15:15:00 IST
CAS_END = (15, 35)     # 15:35:00 IST — after this NSE finalizes the close


def _in_cas_window(now):
    start = now.replace(hour=CAS_START[0], minute=CAS_START[1], second=0, microsecond=0)
    end = now.replace(hour=CAS_END[0], minute=CAS_END[1], second=0, microsecond=0)
    return start <= now <= end


def _equilibrium_price(depth: dict, fallback_price: float):
    """Given Kite's {'buy': [...], 'sell': [...]} 5-level depth (each level
    {'price','quantity','orders'}), find the price that maximizes matched
    quantity, call-auction style. Returns (price, matched_qty, imbalance_qty,
    imbalance_side)."""
    buy_levels = [l for l in (depth or {}).get("buy", []) if l.get("price") and l.get("quantity")]
    sell_levels = [l for l in (depth or {}).get("sell", []) if l.get("price") and l.get("quantity")]

    if not buy_levels or not sell_levels:
        return fallback_price, 0, 0, None

    candidate_prices = sorted({l["price"] for l in buy_levels} | {l["price"] for l in sell_levels})

    best_price, best_matched = fallback_price, -1
    for p in candidate_prices:
        cum_buy = sum(l["quantity"] for l in buy_levels if l["price"] >= p)
        cum_sell = sum(l["quantity"] for l in sell_levels if l["price"] <= p)
        matched = min(cum_buy, cum_sell)
        if matched > best_matched or (
            matched == best_matched and abs(p - fallback_price) < abs(best_price - fallback_price)
        ):
            best_matched = matched
            best_price = p
            best_cum_buy, best_cum_sell = cum_buy, cum_sell

    imbalance_qty = abs(best_cum_buy - best_cum_sell)
    imbalance_side = "BUY" if best_cum_buy > best_cum_sell else ("SELL" if best_cum_sell > best_cum_buy else None)
    return best_price, best_matched, imbalance_qty, imbalance_side


def capture_cas_indicative():
    if os.getenv("CAPTURE_ENABLED", "false").lower() != "true":
        return
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return
    from utils.market_calendar import is_trading_day
    if not is_trading_day(now.date()):
        return
    if not _in_cas_window(now):
        return

    try:
        from services.kite_auth import get_kite_client
        from utils.db import get_supabase
        from main import INDEX_NSE_MAP, STOCK_NSE_MAP

        kite = get_kite_client()
        supabase = get_supabase()

        all_map = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}
        keys = list(all_map.values())
        trade_date = now.date().isoformat()
        updated_at = now.astimezone(timezone.utc).isoformat()

        # Kite quote() supports batching; chunk defensively to stay well
        # under any request-size limit.
        rows = []
        CHUNK = 200
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            try:
                quotes = kite.quote(chunk)
            except Exception as e:
                print(f"[cas_indicative] quote chunk failed: {e}")
                continue
            for sym, key in all_map.items():
                if key not in chunk:
                    continue
                q = quotes.get(key)
                if not q:
                    continue
                ltp = q.get("last_price") or 0
                prev_close = (q.get("ohlc") or {}).get("close") or 0
                depth = q.get("depth") or {}
                price, matched, imb_qty, imb_side = _equilibrium_price(depth, ltp)
                if not price:
                    continue
                chg_pct = round(((price - prev_close) / prev_close) * 100, 2) if prev_close else None
                rows.append({
                    "symbol": sym,
                    "trade_date": trade_date,
                    "indicative_price": float(price),
                    "prev_close": float(prev_close) if prev_close else None,
                    "chg_pct": chg_pct,
                    "imbalance_qty": int(imb_qty),
                    "imbalance_side": imb_side,
                    "matched_qty": int(matched) if matched and matched > 0 else 0,
                    "ltp_at_315": float(ltp),
                    "updated_at": updated_at,
                })

        if rows:
            supabase.from_("cas_indicative").upsert(rows, on_conflict="symbol,trade_date").execute()
            print(f"[cas_indicative] upserted {len(rows)} symbols @ {now.strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"[cas_indicative] capture failed: {e}")
