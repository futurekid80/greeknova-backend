"""Gamma Squeeze scanner — implements the "Gamma Move" strategy described by
Vivek (SEBI-registered analyst) on the TradeAlphaGuru podcast
(youtube.com/watch?v=W88GygpXZWI, ~46:22-60:00):

  1. For each symbol, the strike carrying the SINGLE HIGHEST open interest
     (per side, CE/PE, nearest active expiry) IS the support/resistance
     level to watch — that's where writers are concentrated.
  2. Watch that one key strike for three things happening together, in the
     recent ~30 min:
       - OI at that strike falling meaningfully (writers stopped out,
         unwinding)
       - Volume abnormal vs. that strike's own recent baseline rate (not a
         flat floor)
       - The option's own premium rising
  3. Only meaningful in the last ~2 weeks to expiry — early in a fresh
     monthly cycle this setup doesn't behave the same way (too much time
     value, no urgency to cover).

CE-side trigger = writers being squeezed out of a resistance = bullish.
PE-side trigger = writers being squeezed out of a support = bearish.
"""
from utils.db import get_supabase
from datetime import datetime, timezone, timedelta, date as date_type
import time as time_module

_gs_cache: dict = {}
_gs_cache_time: float = 0
GS_CACHE_TTL = 240  # 4 minutes

from api.uoa import is_market_hours, is_post_market

# ── Tunable thresholds ────────────────────────────────────────────────────
OI_DECLINE_30MIN_PCT = -4.0   # key strike's OI must have fallen at least this much in ~30 min
LTP_RISE_30MIN_PCT   = 3.0    # key strike's own premium must have risen at least this much in ~30 min
VOL_SPIKE_RATIO_MIN  = 1.5    # recent 30-min volume rate vs. the session's baseline rate before that
MAX_DAYS_TO_EXPIRY   = 14     # podcast: avoid the first ~2 weeks of a fresh monthly contract
MIN_OPEN_OI          = 5000   # ignore illiquid strikes so "highest OI" isn't noise


def get_gamma_squeeze(date: str = None):
    global _gs_cache, _gs_cache_time

    cache_ttl = GS_CACHE_TTL if is_market_hours() else 600
    if _gs_cache and (time_module.time() - _gs_cache_time) < cache_ttl:
        print(f"[GAMMA_SQUEEZE] Returning cached result ({int(time_module.time() - _gs_cache_time)}s old)")
        return _gs_cache

    supabase = get_supabase()
    today = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    post_market = is_post_market()

    # ── First and last capture today for NIFTY (written every cycle) ───────
    # NOTE: previously this paginated through every row with .range() ordered
    # only by `timestamp` to build a de-duplicated list of distinct capture
    # times. That's unsafe — dozens of NIFTY strikes share the exact same
    # timestamp per capture cycle, so ordering with no tie-breaker makes
    # Supabase's offset pagination non-deterministic: a page can come back
    # short mid-way through the day, the "stop when a page has <1000 rows"
    # check reads that as reaching the end, and the real latest captures
    # (everything after that point) get silently dropped — which is exactly
    # why this was stuck reporting a stale "latest" time for hours. Two
    # direct MIN/MAX queries can't have that failure mode.
    open_row = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=False)\
        .limit(1).execute()
    new_row = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .eq("symbol", "NIFTY")\
        .gte("timestamp", f"{today}T00:00:00+00:00")\
        .lt("timestamp",  f"{today}T23:59:59+00:00")\
        .order("timestamp", desc=True)\
        .limit(1).execute()

    if not open_row.data or not new_row.data:
        return {"signals": [], "total": 0, "date": today, "watchlist": []}

    ts_open = open_row.data[0]["timestamp"]
    ts_new  = new_row.data[0]["timestamp"]
    if ts_open == ts_new:
        return {"signals": [], "total": 0, "date": today, "watchlist": []}

    ts_new_dt = datetime.fromisoformat(ts_new.replace('+00:00', '')).replace(tzinfo=timezone.utc)
    ts_30min  = (ts_new_dt - timedelta(minutes=30)).isoformat()  # fetch_snapshot below
    # widens this into a search window anyway, so an approximate anchor is fine.

    def fetch_snapshot(ts):
        ts_dt = datetime.fromisoformat(ts.replace('+00:00', '')).replace(tzinfo=timezone.utc)
        window_start = (ts_dt - timedelta(minutes=4)).isoformat()
        rows = []
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .gte("timestamp", window_start)\
                .lte("timestamp", ts)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            rows.extend(batch.data)
            if len(batch.data) < 1000:
                break
        latest_by_key: dict = {}
        for r in rows:
            key = (r.get("symbol"), r.get("option_type"), r.get("strike"), r.get("expiry"))
            if key not in latest_by_key or r["timestamp"] > latest_by_key[key]["timestamp"]:
                latest_by_key[key] = r
        return list(latest_by_key.values())

    new_data_raw   = fetch_snapshot(ts_new)
    open_data_raw  = fetch_snapshot(ts_open)
    min30_data_raw = fetch_snapshot(ts_30min)

    # ── Nearest active expiry per symbol, restricted to the last
    # MAX_DAYS_TO_EXPIRY days (the podcast's "last 1-2 weeks" window) ──────
    today_date = date_type.fromisoformat(today) if date else datetime.now(timezone.utc).date()
    nearest_expiry_map: dict = {}
    for r in new_data_raw:
        sym = r["symbol"]
        exp = r.get("expiry")
        opt = r.get("option_type", "")
        if opt not in ("CE", "PE"):
            continue
        if not exp or exp < today_date.isoformat():
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    dte_map: dict = {}
    for sym, exp in nearest_expiry_map.items():
        try:
            dte_map[sym] = (date_type.fromisoformat(exp) - today_date).days
        except Exception:
            dte_map[sym] = 999

    # No longer a hard cutoff — every symbol with a valid upcoming expiry is
    # eligible. Days-to-expiry instead becomes a conviction tag + score weight
    # (near-expiry setups match the podcast's stated conditions best; early-
    # cycle ones can still be real moves, just lower conviction per their own
    # caveat, so we surface them tagged rather than hiding them).
    eligible_symbols = {sym for sym, dte in dte_map.items() if dte >= 0}

    def filter_rows(rows):
        return [
            r for r in rows
            if r["symbol"] in eligible_symbols
            and nearest_expiry_map.get(r["symbol"]) == r.get("expiry")
        ]

    new_data   = filter_rows(new_data_raw)
    open_data  = filter_rows(open_data_raw)
    min30_data = filter_rows(min30_data_raw)

    open_map  = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in open_data}
    min30_map = {f"{r['symbol']}_{r['tradingsymbol']}": r for r in min30_data}

    # ── CMP map (for spot reference only, not used as a gate) ──────────────
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
    cmp_map, seen_cmp = {}, set()
    for c in cmp_raw:
        if c["symbol"] not in seen_cmp:
            cmp_map[c["symbol"]] = c["cmp"]
            seen_cmp.add(c["symbol"])

    # ── Step 1: find the single highest-OI strike per (symbol, option_type)
    # — this IS the support/resistance level per the strategy ──────────────
    key_strikes: dict = {}  # (symbol, option_type) -> row with max OI
    for row in new_data:
        if row.get("option_type") not in ("CE", "PE"):
            continue  # skip futures rows — this strategy is options-only
        oi = row.get("oi") or 0
        if oi < MIN_OPEN_OI:
            continue
        gkey = (row["symbol"], row["option_type"])
        if gkey not in key_strikes or oi > (key_strikes[gkey].get("oi") or 0):
            key_strikes[gkey] = row

    def elapsed_minutes(ts_a, ts_b):
        try:
            a = datetime.fromisoformat(ts_a.replace('+00:00', '')).replace(tzinfo=timezone.utc)
            b = datetime.fromisoformat(ts_b.replace('+00:00', '')).replace(tzinfo=timezone.utc)
            return max(1.0, abs((b - a).total_seconds()) / 60.0)
        except Exception:
            return 30.0

    mins_open_to_30min = elapsed_minutes(ts_open, ts_30min)
    mins_30min_to_new  = elapsed_minutes(ts_30min, ts_new)

    squeezes = []
    watchlist = []
    for (sym, opt_type), row in key_strikes.items():
        ts_sym = row["tradingsymbol"]
        key    = f"{sym}_{ts_sym}"
        open_row  = open_map.get(key)
        min30_row = min30_map.get(key)
        if not open_row or not min30_row:
            continue

        strike   = row["strike"]
        new_vol  = row["volume"] or 0
        new_oi   = row["oi"] or 0
        new_ltp  = row["last_price"] or 0
        open_oi  = open_row["oi"] or 0
        open_vol = open_row["volume"] or 0
        open_ltp = open_row["last_price"] or 0
        min30_oi  = min30_row["oi"] or 0
        min30_vol = min30_row["volume"] or 0
        min30_ltp = min30_row["last_price"] or 0

        if min30_oi <= 0 or min30_ltp <= 0:
            continue

        oi_chg_30min_pct  = (new_oi - min30_oi) / min30_oi * 100
        ltp_chg_30min_pct = (new_ltp - min30_ltp) / min30_ltp * 100
        oi_chg_from_open_pct = ((new_oi - open_oi) / open_oi * 100) if open_oi > 0 else 0

        baseline_rate = max(0.0, (min30_vol - open_vol)) / mins_open_to_30min
        recent_rate   = max(0.0, (new_vol - min30_vol)) / mins_30min_to_new
        if baseline_rate > 0:
            vol_spike_ratio = recent_rate / baseline_rate
        else:
            # No real baseline yet (early session) — fall back to whether
            # there's at least real recent activity, don't just pass everyone.
            vol_spike_ratio = 1.0 if recent_rate > 0 else 0.0

        triggered = (
            oi_chg_30min_pct < OI_DECLINE_30MIN_PCT and
            ltp_chg_30min_pct > LTP_RISE_30MIN_PCT and
            vol_spike_ratio >= VOL_SPIKE_RATIO_MIN
        )

        # How close each leg is to firing (0-100%), so a near-miss is visibly
        # near-miss rather than lumped in with something nowhere close.
        oi_leg_pct  = round(min(100, max(0, (-oi_chg_30min_pct / -OI_DECLINE_30MIN_PCT) * 100)), 0)
        ltp_leg_pct = round(min(100, max(0, (ltp_chg_30min_pct / LTP_RISE_30MIN_PCT) * 100)), 0)
        vol_leg_pct = round(min(100, max(0, (vol_spike_ratio / VOL_SPIKE_RATIO_MIN) * 100)), 0)
        legs_met = int(oi_chg_30min_pct < OI_DECLINE_30MIN_PCT) + int(ltp_chg_30min_pct > LTP_RISE_30MIN_PCT) + int(vol_spike_ratio >= VOL_SPIKE_RATIO_MIN)

        cmp = cmp_map.get(sym, 0)
        dte = dte_map.get(sym, None)
        near_expiry = dte is not None and dte <= MAX_DAYS_TO_EXPIRY
        conviction = "HIGH" if near_expiry else "LOW"
        conviction_note = (
            f"Within {MAX_DAYS_TO_EXPIRY} days of expiry — matches the strategy's conditions"
            if near_expiry else
            f"{dte} days to expiry — outside the last-{MAX_DAYS_TO_EXPIRY}-day window the strategy "
            f"is built for, so writers may not be under real pressure to cover yet; treat as lower conviction"
        )

        bias  = "BULLISH" if opt_type == "CE" else "BEARISH"
        level_kind = "Resistance" if opt_type == "CE" else "Support"
        label = f"Call writers squeezed at resistance — Bullish" if opt_type == "CE" \
            else f"Put writers squeezed at support — Bearish"
        desc = (
            f"{sym} {strike:.0f}{opt_type} is the highest-OI strike ({level_kind.lower()} level) — "
            f"OI down {abs(oi_chg_30min_pct):.1f}% in ~30 min, volume running "
            f"{vol_spike_ratio:.1f}x its baseline rate, premium up {ltp_chg_30min_pct:.1f}% — "
            f"writers being forced to cover"
        )

        # Near-expiry setups score meaningfully higher (per the strategy's own
        # caveat); far-from-expiry ones still surface, just lower-ranked.
        expiry_weight = max(0, MAX_DAYS_TO_EXPIRY - dte) * 0.4 if near_expiry else -5
        squeeze_score = (
            abs(oi_chg_30min_pct) * 0.6 +
            ltp_chg_30min_pct * 0.8 +
            min(vol_spike_ratio, 5) * 3 +
            expiry_weight
        )

        row_out = {
            "symbol":              sym,
            "tradingsymbol":       ts_sym,
            "strike":              float(strike),
            "option_type":         opt_type,
            "level_kind":          level_kind,
            "cmp":                 float(cmp),
            "ltp":                 float(new_ltp),
            "ltp_chg_30min_pct":   round(ltp_chg_30min_pct, 2),
            "oi":                  new_oi,
            "oi_chg_30min_pct":    round(oi_chg_30min_pct, 2),
            "oi_chg_from_open_pct": round(oi_chg_from_open_pct, 2),
            "volume":              new_vol,
            "vol_spike_ratio":     round(vol_spike_ratio, 2),
            "days_to_expiry":      dte,
            "conviction":          conviction,
            "conviction_note":     conviction_note,
            "squeeze_score":       round(squeeze_score, 1),
            "bias":                bias,
            "label":               label,
            "desc":                desc,
            "triggered":           triggered,
            "legs_met":            legs_met,
            "oi_leg_pct":          oi_leg_pct,
            "ltp_leg_pct":         ltp_leg_pct,
            "vol_leg_pct":         vol_leg_pct,
        }
        watchlist.append(row_out)
        if triggered:
            squeezes.append(row_out)

    squeezes.sort(key=lambda x: x["squeeze_score"], reverse=True)
    # Watchlist: everything NOT already triggered (those are in "signals"),
    # ranked by how close it is to qualifying — most legs met first, then by
    # score, so a 2-of-3 near-miss always sits above a 0-of-3 non-starter.
    watchlist = [w for w in watchlist if not w["triggered"]]
    watchlist.sort(key=lambda x: (x["legs_met"], x["squeeze_score"]), reverse=True)

    def to_ist(ts):
        try:
            clean = ts.split('+')[0].split('Z')[0]
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            m = dt.hour * 60 + dt.minute + 330
            return f"{(m // 60) % 24:02d}:{m % 60:02d}"
        except Exception:
            return ts[11:16]

    result = {
        "date":           today,
        "open_time":      to_ist(ts_open),
        "window_time":    to_ist(ts_30min),
        "close_time":     to_ist(ts_new),
        "total":          len(squeezes),
        "signals":        squeezes[:40],
        "watchlist":      watchlist[:60],
        "is_post_market": post_market,
    }

    _gs_cache = result
    _gs_cache_time = time_module.time()
    print(f"[GAMMA_SQUEEZE] {len(squeezes)} squeeze setups found for {today} (key-strike method)")
    return result
