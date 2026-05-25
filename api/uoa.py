from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type
from collections import defaultdict
_uoa_cache: dict = {}
_uoa_cache_time: float = 0
import time as time_module
UOA_CACHE_TTL = 60

STOCK_NSE_MAP = {
    "RELIANCE":"NSE:RELIANCE","TCS":"NSE:TCS","HDFCBANK":"NSE:HDFCBANK",
    "INFY":"NSE:INFY","ICICIBANK":"NSE:ICICIBANK","HINDUNILVR":"NSE:HINDUNILVR",
    "ITC":"NSE:ITC","SBIN":"NSE:SBIN","BHARTIARTL":"NSE:BHARTIARTL",
    "KOTAKBANK":"NSE:KOTAKBANK","LT":"NSE:LT","AXISBANK":"NSE:AXISBANK",
    "ASIANPAINT":"NSE:ASIANPAINT","MARUTI":"NSE:MARUTI","TITAN":"NSE:TITAN",
    "SUNPHARMA":"NSE:SUNPHARMA","ULTRACEMCO":"NSE:ULTRACEMCO",
    "BAJFINANCE":"NSE:BAJFINANCE","WIPRO":"NSE:WIPRO","HCLTECH":"NSE:HCLTECH",
    "TATACONSUM":"NSE:TATACONSUM","TATASTEEL":"NSE:TATASTEEL",
    "ADANIENT":"NSE:ADANIENT","POWERGRID":"NSE:POWERGRID","NTPC":"NSE:NTPC",
    "ONGC":"NSE:ONGC","JSWSTEEL":"NSE:JSWSTEEL","COALINDIA":"NSE:COALINDIA",
    "BAJAJFINSV":"NSE:BAJAJFINSV","TECHM":"NSE:TECHM",
    "APOLLOHOSP":"NSE:APOLLOHOSP","BAJAJ-AUTO":"NSE:BAJAJ-AUTO",
    "BPCL":"NSE:BPCL","BRITANNIA":"NSE:BRITANNIA","CIPLA":"NSE:CIPLA",
    "DRREDDY":"NSE:DRREDDY","EICHERMOT":"NSE:EICHERMOT","GRASIM":"NSE:GRASIM",
    "HEROMOTOCO":"NSE:HEROMOTOCO","HINDALCO":"NSE:HINDALCO",
    "HDFCLIFE":"NSE:HDFCLIFE","INDUSINDBK":"NSE:INDUSINDBK",
    "JIOFIN":"NSE:JIOFIN","M&M":"NSE:M&M","NESTLEIND":"NSE:NESTLEIND",
    "SBILIFE":"NSE:SBILIFE","SHRIRAMFIN":"NSE:SHRIRAMFIN","TRENT":"NSE:TRENT",
    "ADANIPORTS":"NSE:ADANIPORTS","BANKBARODA":"NSE:BANKBARODA",
    "BEL":"NSE:BEL","CANBK":"NSE:CANBK","CHOLAFIN":"NSE:CHOLAFIN",
    "DLF":"NSE:DLF","GAIL":"NSE:GAIL","HAVELLS":"NSE:HAVELLS",
    "HAL":"NSE:HAL","INDIGO":"NSE:INDIGO","PFC":"NSE:PFC",
    "RECLTD":"NSE:RECLTD","SAIL":"NSE:SAIL","TATAPOWER":"NSE:TATAPOWER",
    "VEDL":"NSE:VEDL",
}
INDEX_NSE_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
}
ALL_NSE_MAP = {**INDEX_NSE_MAP, **STOCK_NSE_MAP}

MARKET_OPEN_UTC  = 3 * 60 + 45
MARKET_CLOSE_UTC = 10 * 60 + 0

def is_market_hours() -> bool:
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return False
    total = now_utc.hour * 60 + now_utc.minute
    return MARKET_OPEN_UTC <= total <= MARKET_CLOSE_UTC

def is_post_market() -> bool:
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return True
    total = now_utc.hour * 60 + now_utc.minute
    return total > MARKET_CLOSE_UTC

def to_ist(ts: str) -> str:
    try:
        clean = ts.split('+')[0].split('Z')[0]
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        ist = dt.hour * 60 + dt.minute + 330
        return f"{(ist//60)%24:02d}:{ist%60:02d}"
    except:
        return ts[11:16]

def get_uoa(date: str = None):
    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    live = is_market_hours()
    post_market = is_post_market()

    # ── Get all timestamps for today ─────────────────────────────────────────
    all_ts_rows = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp")\
            .eq("symbol", "NIFTY")\
            .gte("timestamp", f"{today}T06:00:00+00:00")\
            .lt("timestamp",  f"{today}T23:59:59+00:00")\
            .order("timestamp", desc=False)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_ts_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    timestamps = sorted(set(r["timestamp"] for r in all_ts_rows))

    if len(timestamps) < 2:
        fallback_rows = []
        for offset in range(0, 50000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("timestamp")\
                .eq("symbol", "NIFTY")\
                .order("timestamp", desc=True)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            fallback_rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        timestamps = sorted(set(r["timestamp"] for r in fallback_rows))

    if len(timestamps) < 2:
        return {"signals": [], "total": 0}

    ts_open  = timestamps[0]
    ts_new   = timestamps[-1]
    ts_30min = timestamps[max(0, len(timestamps) - 7)]
    total_snaps = len(timestamps)

    now_utc = datetime.now(timezone.utc)
    market_close = now_utc.replace(hour=10, minute=0, second=0, microsecond=0)
    mins_to_close = int((market_close - now_utc).total_seconds() / 60)
    is_near_close      = 0 < mins_to_close <= 30
    is_very_near_close = 0 < mins_to_close <= 15

    # ── Fetch snapshots ───────────────────────────────────────────────────────
    def fetch_snapshot(ts):
        rows = []
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .eq("timestamp", ts)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        return rows

    new_data_raw   = fetch_snapshot(ts_new)
    open_data_raw  = fetch_snapshot(ts_open)
    min30_data_raw = fetch_snapshot(ts_30min)

    # ── Nearest active expiry per symbol ─────────────────────────────────────
    today_str = date_type.today().isoformat()
    nearest_expiry_map: dict = {}
    for r in new_data_raw:
        sym = r["symbol"]
        exp = r.get("expiry")
        if not exp or exp < today_str:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    def filter_to_nearest_expiry(rows):
        filtered = []
        for r in rows:
            sym = r["symbol"]
            exp = r.get("expiry")
            nearest = nearest_expiry_map.get(sym)
            if nearest and exp == nearest:
                filtered.append(r)
        return filtered

    new_data   = filter_to_nearest_expiry(new_data_raw)
    open_data  = filter_to_nearest_expiry(open_data_raw)
    min30_data = filter_to_nearest_expiry(min30_data_raw)

    # ── Persistence: fetch all day activity per tradingsymbol ─────────────────
    # Single query — all snapshots with vol > 50K across today
    all_day_rows = []
    for offset in range(0, 50000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("timestamp, tradingsymbol, volume")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .lt("timestamp",  f"{today}T23:59:59+00:00")\
            .gt("volume", 500000)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        all_day_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # Build persistence map per tradingsymbol
    ts_activity: dict = defaultdict(set)
    first_seen_map: dict = {}
    for r in all_day_rows:
        ts_key = r["tradingsymbol"]
        ts_activity[ts_key].add(r["timestamp"])
        if ts_key not in first_seen_map or r["timestamp"] < first_seen_map[ts_key]:
            first_seen_map[ts_key] = r["timestamp"]

    # ── CMP map ───────────────────────────────────────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .gte("timestamp", f"{today}T00:00:00+00:00")\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        cmp_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    cmp_map = {}
    seen_cmp = set()
    for c in cmp_raw:
        if c["symbol"] not in seen_cmp:
            cmp_map[c["symbol"]] = c["cmp"]
            seen_cmp.add(c["symbol"])

    # ── Day high map ──────────────────────────────────────────────────────────
    day_high_map: dict = {}
    if live:
        try:
            from services.kite_auth import get_kite_client
            kite = get_kite_client()
            candidate_syms = list(set(r["symbol"] for r in new_data))
            nse_keys = [ALL_NSE_MAP[s] for s in candidate_syms if s in ALL_NSE_MAP]
            if nse_keys:
                ohlc = kite.ohlc(nse_keys)
                for sym, nse_key in ALL_NSE_MAP.items():
                    if nse_key in ohlc:
                        day_high_map[sym] = ohlc[nse_key]["ohlc"]["high"]
        except Exception as e:
            print(f"[UOA] Kite OHLC failed: {e}")

    if not day_high_map:
        try:
            cmp_today = supabase.from_("cmp_prices")\
                .select("symbol, cmp")\
                .gte("timestamp", f"{today}T00:00:00+00:00")\
                .limit(5000)\
                .execute().data or []
            sym_prices: dict = defaultdict(list)
            for r in cmp_today:
                sym_prices[r["symbol"]].append(float(r["cmp"]))
            day_high_map = {sym: max(prices) for sym, prices in sym_prices.items()}
        except Exception as e:
            print(f"[UOA] Day high fallback failed: {e}")

    open_map  = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in open_data}
    min30_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in min30_data}

    avg_vol: dict = {}
    for key in set(list(open_map.keys()) + list(min30_map.keys())):
        vols = []
        if key in open_map and open_map[key].get("volume"):
            vols.append(open_map[key]["volume"])
        if key in min30_map and min30_map[key].get("volume"):
            vols.append(min30_map[key]["volume"])
        if vols:
            avg_vol[key] = sum(vols) / len(vols)

    uoa_signals = []

    for row in new_data:
        sym       = row["symbol"]
        ts        = row["tradingsymbol"]
        key       = f"{sym}_{ts}"
        open_row  = open_map.get(key)
        min30_row = min30_map.get(key)

        if not open_row or not min30_row:
            continue

        cmp       = cmp_map.get(sym, 0)
        strike    = row["strike"]
        opt_type  = row["option_type"]
        new_vol   = row["volume"] or 0
        new_oi    = row["oi"] or 0
        new_ltp   = row["last_price"] or 0
        open_ltp  = open_row["last_price"] or 0
        min30_oi  = min30_row["oi"] or 0
        min30_vol = min30_row["volume"] or 0

        if new_vol < 100000:
            continue

        ltp_chg_from_open = ((new_ltp - open_ltp) / open_ltp * 100) if open_ltp > 0 else 0
        price_rising  = ltp_chg_from_open > 2.0
        price_falling = ltp_chg_from_open < -2.0

        oi_chg_30min = ((new_oi - min30_oi) / min30_oi * 100) if min30_oi > 0 else 0
        oi_rising    = oi_chg_30min > 2.0
        oi_falling   = oi_chg_30min < -2.0

        stock_day_high = day_high_map.get(sym, 0)
        at_day_high = False
        day_high_pct = None
        if stock_day_high > 0 and cmp > 0:
            day_high_pct = round((stock_day_high - cmp) / cmp * 100, 2)
            at_day_high = day_high_pct <= 0.5 and opt_type == 'CE'

        avg = avg_vol.get(key, new_vol)
        vol_ratio    = new_vol / avg if avg > 0 else 1
        vol_oi_ratio = new_vol / new_oi if new_oi > 0 else 0
        vol_chg_30m  = ((new_vol - min30_vol) / min30_vol * 100) if min30_vol > 0 else 0

        if cmp > 0:
            dist_pct = ((strike - cmp) / cmp * 100)
            is_otm   = (opt_type == 'CE' and strike > cmp) or (opt_type == 'PE' and strike < cmp)
            otm_pct  = abs(dist_pct) if is_otm else 0
        else:
            otm_pct = 0
            is_otm  = False

        score = 0
        if vol_ratio > 6:      score += 2
        elif vol_ratio > 4:    score += 1
        if vol_oi_ratio > 4:   score += 2
        elif vol_oi_ratio > 2: score += 1
        if otm_pct > 3 and new_vol > 200000: score += 1
        if abs(oi_chg_30min) > 10 and vol_chg_30m > 20: score += 1
        if at_day_high and oi_rising and opt_type == 'CE': score += 1
        if opt_type == 'PE' and oi_rising and price_falling: score += 1

        if score < 3:
            continue

        if oi_rising and price_rising:
            if opt_type == 'CE':
                signal_type = "LONG_BUILDUP"
                signal_desc = "CE OI building last 30 mins · price rising from open · call accumulation observed"
                bias = "BULLISH"
            else:
                signal_type = "SHORT_BUILDUP"
                signal_desc = "PE OI building last 30 mins · price rising from open · put accumulation observed"
                bias = "BEARISH"
        elif oi_rising and price_falling:
            if opt_type == 'CE':
                signal_type = "CALL_WRITING"
                signal_desc = "CE OI rising · price falling from open · call writer activity observed"
                bias = "BEARISH"
            else:
                signal_type = "PUT_WRITING"
                signal_desc = "PE OI rising · price falling from open · put writer activity observed"
                bias = "BULLISH"
        elif oi_falling and price_rising:
            if opt_type == 'CE':
                signal_type = "SHORT_COVERING"
                signal_desc = "CE OI reducing · price rising from open · call short positions unwinding"
                bias = "BULLISH"
            else:
                signal_type = "LONG_UNWINDING"
                signal_desc = "PE OI reducing · price rising from open · put long positions exiting"
                bias = "BULLISH"
        elif oi_falling and price_falling:
            if opt_type == 'CE':
                signal_type = "LONG_UNWINDING"
                signal_desc = "CE OI reducing · price falling from open · call long positions exiting"
                bias = "BEARISH"
            else:
                signal_type = "SHORT_COVERING"
                signal_desc = "PE OI reducing · price falling from open · put short positions unwinding"
                bias = "BEARISH"
        elif vol_oi_ratio > 2 and not oi_rising and not oi_falling:
            if price_rising:
                signal_type = "BUYER_DOMINATED"
                signal_desc = "High volume · flat OI · price above open · buying interest observed"
                bias = "BULLISH" if opt_type == "CE" else "BEARISH"
            elif price_falling:
                signal_type = "SELLER_DOMINATED"
                signal_desc = "High volume · flat OI · price below open · selling pressure observed"
                bias = "BEARISH" if opt_type == "CE" else "BULLISH"
            else:
                continue
        elif otm_pct > 3 and new_vol > 200000:
            signal_type = "FAR_OTM_ACTIVITY"
            signal_desc = f"{otm_pct:.1f}% OTM · heavy volume · possible hedging or speculative interest"
            bias = "BULLISH" if opt_type == "PE" else "BEARISH"
        elif vol_ratio > 4 and (price_rising or price_falling):
            signal_type = "VOLUME_SURGE"
            signal_desc = f"{vol_ratio:.1f}x average volume · significant activity vs baseline"
            bias = "BULLISH" if (
                (opt_type == "CE" and price_rising) or
                (opt_type == "PE" and price_falling)
            ) else "BEARISH"
        else:
            continue

        if post_market:
            time_tag = "post_market"
        elif is_very_near_close:
            time_tag = "market_closing"
        elif is_near_close:
            time_tag = "positional_only"
        else:
            time_tag = "normal"

        # Persistence fields
        snap_set = ts_activity.get(ts, set())
        snap_count = len(snap_set)
        persistence_pct = round(snap_count / total_snaps * 100) if total_snaps > 0 else 0
        first_seen_ts = first_seen_map.get(ts, ts_new)

        uoa_signals.append({
            "symbol":             sym,
            "tradingsymbol":      ts,
            "strike":             strike,
            "option_type":        opt_type,
            "cmp":                float(cmp),
            "ltp":                float(new_ltp),
            "open_ltp":           float(open_ltp),
            "ltp_chg_from_open":  round(ltp_chg_from_open, 2),
            "volume":             new_vol,
            "oi":                 new_oi,
            "oi_chg_30min":       round(oi_chg_30min, 2),
            "vol_oi_ratio":       round(vol_oi_ratio, 2),
            "vol_ratio":          round(vol_ratio, 2),
            "vol_chg_30min":      round(vol_chg_30m, 2),
            "otm_pct":            round(otm_pct, 2),
            "is_otm":             is_otm,
            "signal_type":        signal_type,
            "signal_desc":        signal_desc,
            "bias":               bias,
            "score":              min(score, 5),
            "time_tag":           time_tag,
            "is_index":           sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            "day_high":           float(stock_day_high) if stock_day_high else None,
            "day_high_pct":       day_high_pct,
            "at_day_high":        at_day_high,
            "snapshot_count":     snap_count,
            "persistence_pct":    persistence_pct,
            "first_seen":         to_ist(first_seen_ts),
            "first_seen_ts":      first_seen_ts,
        })

    uoa_signals.sort(key=lambda x: (x["persistence_pct"], x["score"], abs(x["ltp_chg_from_open"])), reverse=True)

    return {
        "timestamp":         ts_new,
        "open_timestamp":    ts_open,
        "open_time":         to_ist(ts_open),
        "close_time":        to_ist(ts_new),
        "date":              today,
        "total":             len(uoa_signals),
        "signals":           uoa_signals[:50],
        "snapshot_count":    total_snaps,
        "mins_to_close":     max(0, mins_to_close),
        "is_post_market":    post_market,
        "market_close_time": "15:29",
    }
