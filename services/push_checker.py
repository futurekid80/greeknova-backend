"""
push_checker.py
Server-side mirror of the OI spike / fresh build / UOA whale detection that
previously only ran inside the browser's service worker (frontend/public/sw.js).
Running this on the backend scheduler means alerts fire reliably even when
no browser tab is open.

Mirrors sw.js's checkOptionsJungle() and checkUOAWhales() logic and message
formatting exactly, so alerts look identical whether delivered via push or
via the (still-present) in-browser fallback.
"""
from datetime import datetime, timezone

_previous_keys: set = set()
_last_reset_date: str = ""

# Per-symbol last-known state for the technical-level crosses (EMA 9/21,
# PDH/PDL, PWH/PWL) -- cleared every new day so the first check of the day
# only establishes a baseline (no alert) rather than firing on a stale
# carry-over state from yesterday's close.
_tech_state: dict = {}


def _reset_if_new_day():
    global _previous_keys, _last_reset_date, _tech_state
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _last_reset_date:
        _previous_keys = set()
        _tech_state = {}
        _last_reset_date = today


def _fmt_oi(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    if abs(n) >= 10000000:
        return f"{n/10000000:.1f}Cr"
    if abs(n) >= 100000:
        return f"{n/100000:.1f}L"
    # (Oct 27 2026, Vinod feedback): below 1L this used to fall through to a
    # plain comma-grouped number (e.g. "47,175"), inconsistent with every
    # other OI figure on the page being in L/Cr. Round to K for readability.
    if abs(n) >= 1000:
        return f"{n/1000:.0f}K"
    return f"{n:,.0f}"


# ── Near-strike OI collapse ("wall breaking") config ──────────────────────
NEAR_STRIKE_PCT = 4.0       # only strikes within this % of spot matter -- far
                             # OTM/ITM OI moves on routine hedge flow, not a
                             # real support/resistance break
UNWIND_DROP_PCT = 40.0      # OI must have fallen at least this much vs the
                             # rolling baseline to count as a real unwind
BASELINE_LOOKBACK_MIN = 20  # compare against OI from ~this far back, not the
                             # day's open -- catches a SUDDEN drop happening
                             # right now, not a slow bleed since morning
NEAR_STRIKE_MIN_OI = 5000   # ignore illiquid strikes, same floor used elsewhere


def run_push_checks(supabase):
    """Called every 5 minutes by the scheduler during market hours."""
    _reset_if_new_day()
    try:
        _check_options_jungle(supabase)
    except Exception as e:
        print(f"[PushCheck] Options Jungle failed: {e}")
    try:
        _check_uoa_whales(supabase)
    except Exception as e:
        print(f"[PushCheck] UOA failed: {e}")
    try:
        _check_near_strike_unwind(supabase)
    except Exception as e:
        print(f"[PushCheck] Near-strike unwind failed: {e}")
    try:
        _check_technical_levels(supabase)
    except Exception as e:
        print(f"[PushCheck] Technical levels failed: {e}")
    try:
        _check_pcr_crossover(supabase)
    except Exception as e:
        print(f"[PushCheck] PCR crossover failed: {e}")


def _check_options_jungle(supabase):
    from main import options_jungle
    from api.push_notifications import broadcast_alert

    json_data = options_jungle(oi_threshold=2.0, vol_threshold=50.0)
    ts_new = json_data.get("ts_new", "")

    for spike in (json_data.get("oi_spikes") or []):
        key = f"oi_{spike.get('tradingsymbol')}_{ts_new}"
        if key in _previous_keys:
            continue
        _previous_keys.add(key)

        oi_pct = spike.get("oi_pct", 0)
        interp = spike.get("interpretation")
        # (Oct 27 2026, Vinod feedback): "in 5 min" was repeated on every
        # single OI-spike/Jungle-signal alert, all day -- all these alerts
        # share the SAME fixed 5-min window, so saying it once (as a page-
        # level note on /alerts) instead of per-row cuts the repetition
        # without losing the information. NOT applied to NEAR_STRIKE_UNWIND
        # below, which genuinely uses a different (~20min) window and still
        # needs to say so per-alert.
        body = (
            f"OI {'+' if oi_pct > 0 else ''}{oi_pct}% | "
            f"OI: {_fmt_oi(spike.get('new_oi'))} | LTP: ₹{spike.get('last_price')}"
            + (f" | {interp.replace('_', ' ')}" if interp else "")
        )

        # Use the real interpretation (Call Writing, Put Writing, Long
        # Buildup, etc.) as the alert's signal type when Jungle has one —
        # this makes it the SAME signal vocabulary as UOA, so a single
        # "Put Writing" toggle controls alerts from both pages at once.
        # Falls back to generic OI_SPIKE when there's no clear interpretation.
        known_interps = {
            "CALL_WRITING", "PUT_WRITING", "LONG_BUILDUP", "SHORT_BUILDUP",
            "SHORT_COVERING", "LONG_UNWINDING",
        }
        sig_type = interp if interp in known_interps else "OI_SPIKE"

        broadcast_alert(supabase, {
            "signal": sig_type,
            "symbol": spike.get("symbol"),
            "strike": spike.get("strike"),
            "optionType": spike.get("option_type"),
            "direction": spike.get("direction"),
            "message": body,
            "url": "/jungle",
            "oiPct": oi_pct,
            "ltp": spike.get("last_price"),
        })

    for spike in (json_data.get("vol_spikes") or []):
        if spike.get("vol_signal") != "FRESH_BUILD":
            continue
        key = f"vol_{spike.get('tradingsymbol')}_{ts_new}"
        if key in _previous_keys:
            continue
        _previous_keys.add(key)

        body = f"Vol +{spike.get('vol_pct')}% | OI +{spike.get('oi_pct')}% | LTP: ₹{spike.get('last_price')}"

        broadcast_alert(supabase, {
            "signal": "FRESH_BUILD",
            "symbol": spike.get("symbol"),
            "strike": spike.get("strike"),
            "optionType": spike.get("option_type"),
            "message": body,
            "url": "/jungle",
            "volPct": spike.get("vol_pct"),
            "oiPct": spike.get("oi_pct"),
            "ltp": spike.get("last_price"),
        })


def _check_uoa_whales(supabase):
    from main import uoa
    from api.push_notifications import broadcast_alert

    json_data = uoa()
    ts = json_data.get("timestamp", "")

    for sig in (json_data.get("signals") or []):
        if (sig.get("score") or 0) < 4:
            continue
        key = f"uoa_{sig.get('tradingsymbol')}_{ts}"
        if key in _previous_keys:
            continue
        _previous_keys.add(key)

        oi_chg = sig.get("oi_chg_30min", 0)
        ltp_chg = sig.get("ltp_chg_from_open", 0)
        body = (
            f"Score {sig.get('score')}/5 | OI 30m: {'+' if oi_chg > 0 else ''}{oi_chg}% | "
            f"LTP from open: {'+' if ltp_chg > 0 else ''}{ltp_chg}% | {sig.get('bias')} bias"
        )

        broadcast_alert(supabase, {
            "signal": sig.get("signal_type"),
            "symbol": sig.get("symbol"),
            "strike": sig.get("strike"),
            "optionType": sig.get("option_type"),
            "message": body,
            "url": "/uoa",
            "score": sig.get("score"),
            "bias": sig.get("bias"),
            "ltp": sig.get("ltp"),
        })



def _check_near_strike_unwind(supabase):
    """
    Catches a support/resistance wall breaking as it happens: a strike close
    to spot (CE = resistance, PE = support) whose OI has collapsed over the
    last ~20 minutes. Deliberately separate from the OI_SPIKE feed -- that
    one fires on any 5-min delta at ANY strike, including far OTM/ITM ones
    that move on routine hedge flow (noise for this purpose); this one only
    cares about strikes genuinely close to spot with a large, sudden,
    rolling-window drop -- e.g. DRREDDY 1140 CE unwinding 723k -> 300k OI
    while spot pushed from ~1150 to ~1175, a real short-covering break.
    """
    from api.push_notifications import broadcast_alert
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    # ── Latest snapshot (last ~7 min, same cadence as the rest of the app) ──
    latest_raw = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("symbol,strike,option_type,expiry,oi,last_price,timestamp")\
            .gte("timestamp", (now - timedelta(minutes=7)).isoformat())\
            .lte("timestamp", now.isoformat())\
            .in_("option_type", ["CE", "PE"])\
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        latest_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break
    if not latest_raw:
        return

    latest_by_key = {}
    ts_new = None
    for r in latest_raw:
        key = (r["symbol"], r["strike"], r["option_type"], r["expiry"])
        if key not in latest_by_key or r["timestamp"] > latest_by_key[key]["timestamp"]:
            latest_by_key[key] = r
        if ts_new is None or r["timestamp"] > ts_new:
            ts_new = r["timestamp"]

    # ── Baseline snapshot ~20 min back (narrow window around that point,
    # picking whichever row lands closest to the target time) ─────────────
    ts_new_dt = datetime.fromisoformat(ts_new.replace("+00:00", "")).replace(tzinfo=timezone.utc)
    baseline_center = ts_new_dt - timedelta(minutes=BASELINE_LOOKBACK_MIN)
    baseline_raw = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("symbol,strike,option_type,expiry,oi,timestamp")\
            .gte("timestamp", (baseline_center - timedelta(minutes=4)).isoformat())\
            .lte("timestamp", (baseline_center + timedelta(minutes=4)).isoformat())\
            .in_("option_type", ["CE", "PE"])\
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        baseline_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    def _ts(row):
        return datetime.fromisoformat(row["timestamp"].replace("+00:00", "")).replace(tzinfo=timezone.utc)

    baseline_by_key = {}
    for r in baseline_raw:
        key = (r["symbol"], r["strike"], r["option_type"], r["expiry"])
        cur = baseline_by_key.get(key)
        if cur is None or abs((_ts(r) - baseline_center).total_seconds()) < abs((_ts(cur) - baseline_center).total_seconds()):
            baseline_by_key[key] = r
    if not baseline_by_key:
        return

    # ── Spot per symbol ──────────────────────────────────────────────────
    cmp_res = supabase.from_("cmp_prices").select("symbol,cmp,timestamp")\
        .gte("timestamp", (now - timedelta(minutes=10)).isoformat())\
        .order("timestamp", desc=True).limit(2000).execute()
    cmp_map, seen_cmp = {}, set()
    for c in (cmp_res.data or []):
        if c["symbol"] not in seen_cmp:
            try:
                cmp_map[c["symbol"]] = float(c["cmp"])
            except (TypeError, ValueError):
                continue
            seen_cmp.add(c["symbol"])

    # ── Nearest expiry per symbol, from the latest batch itself ─────────
    nearest_expiry_map = {}
    for r in latest_raw:
        sym, exp = r["symbol"], r.get("expiry")
        if not exp or exp < today:
            continue
        if sym not in nearest_expiry_map or exp < nearest_expiry_map[sym]:
            nearest_expiry_map[sym] = exp

    for key, latest in latest_by_key.items():
        sym, strike, opt, expiry = key
        if nearest_expiry_map.get(sym) != expiry:
            continue
        spot = cmp_map.get(sym)
        if not spot or spot <= 0:
            continue
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        if abs(strike_f - spot) / spot > NEAR_STRIKE_PCT / 100:
            continue

        base = baseline_by_key.get(key)
        if not base:
            continue
        oi_now = latest.get("oi") or 0
        oi_base = base.get("oi") or 0
        if oi_base < NEAR_STRIKE_MIN_OI:
            continue
        chg_pct = (oi_now - oi_base) / oi_base * 100
        if chg_pct > -UNWIND_DROP_PCT:
            continue

        dedupe_key = f"nsu_{sym}_{strike}_{opt}_{ts_new}"
        if dedupe_key in _previous_keys:
            continue
        _previous_keys.add(dedupe_key)

        pct_from_spot = round((strike_f - spot) / spot * 100, 2)
        side = "resistance" if opt == "CE" else "support"
        body = (
            f"OI {chg_pct:.1f}% in ~{BASELINE_LOOKBACK_MIN}min | "
            f"OI: {_fmt_oi(oi_now)} | LTP: \u20b9{latest.get('last_price')} | "
            f"{side} breaking ({abs(pct_from_spot):.1f}% from spot)"
        )

        broadcast_alert(supabase, {
            "signal": "NEAR_STRIKE_UNWIND",
            "symbol": sym,
            "strike": strike_f,
            "optionType": opt,
            "direction": "bullish" if opt == "CE" else "bearish",
            "message": body,
            "url": "/gamma-squeeze",
            "oiPct": round(chg_pct, 1),
            "ltp": latest.get("last_price"),
        })


def _check_technical_levels(supabase):
    """Opt-in-only alerts: 9/21 EMA cross, and price crossing the previous
    day's or previous week's high/low, per symbol. Unlike the OI/UOA checks
    above, these aren't about any single option contract -- they're about
    the underlying stock/index, so one alert per symbol per event, not per
    strike. Only fires on an actual CROSS (state change from the previous
    5-min cycle), not on "currently above/below", so a stock sitting above
    its PDH all day doesn't spam one alert per cycle.
    """
    from services.technical_levels import refresh_levels_sync
    from api.push_notifications import broadcast_alert

    # Blocking is fine here -- this runs on the scheduler's own 5-min tick,
    # not in a request path. Doing a real (synchronous) refresh on every
    # tick is also what keeps the shared cache warm for Jungle/UOA's
    # request-path compute_levels() calls, which never block.
    levels = refresh_levels_sync(supabase)
    if not levels:
        return

    today = datetime.now(timezone.utc).date().isoformat()
    cmp_rows = []
    offset = 0
    while True:
        batch = supabase.from_("cmp_prices").select("symbol,cmp,timestamp") \
            .gte("timestamp", f"{today}T00:00:00+00:00") \
            .order("timestamp", desc=True) \
            .range(offset, offset + 999).execute()
        if not batch.data:
            break
        cmp_rows.extend(batch.data)
        if len(batch.data) < 1000:
            break
        offset += 1000

    cmp_map = {}
    seen = set()
    for r in cmp_rows:
        if r["symbol"] not in seen:
            cmp_map[r["symbol"]] = r["cmp"]
            seen.add(r["symbol"])

    for sym, lv in levels.items():
        cmp = cmp_map.get(sym)
        if not cmp:
            continue
        prev = _tech_state.get(sym, {})

        ema_bullish = lv.get("ema_bullish")
        if ema_bullish is not None:
            prev_ema = prev.get("ema_bullish")
            if prev_ema is not None and ema_bullish != prev_ema:
                sig = "EMA_BULLISH_CROSS" if ema_bullish else "EMA_BEARISH_CROSS"
                broadcast_alert(supabase, {
                    "signal": sig,
                    "symbol": sym,
                    "message": f"{sym}: 9 EMA crossed {'above' if ema_bullish else 'below'} "
                               f"21 EMA on the daily chart -- {'bullish' if ema_bullish else 'bearish'} trend shift",
                    "ltp": cmp,
                    "url": "/jungle",
                })
            prev["ema_bullish"] = ema_bullish

        pdh, pdl = lv.get("pdh"), lv.get("pdl")
        if pdh:
            above_pdh = cmp > pdh
            if prev.get("above_pdh") is False and above_pdh:
                broadcast_alert(supabase, {
                    "signal": "PDH_CROSS", "symbol": sym,
                    "message": f"{sym} crossed above previous day high (₹{pdh:,.1f})",
                    "ltp": cmp, "url": "/jungle",
                })
            prev["above_pdh"] = above_pdh
        if pdl:
            below_pdl = cmp < pdl
            if prev.get("below_pdl") is False and below_pdl:
                broadcast_alert(supabase, {
                    "signal": "PDL_CROSS", "symbol": sym,
                    "message": f"{sym} crossed below previous day low (₹{pdl:,.1f})",
                    "ltp": cmp, "url": "/jungle",
                })
            prev["below_pdl"] = below_pdl

        pwh, pwl = lv.get("pwh"), lv.get("pwl")
        if pwh:
            above_pwh = cmp > pwh
            if prev.get("above_pwh") is False and above_pwh:
                broadcast_alert(supabase, {
                    "signal": "PWH_CROSS", "symbol": sym,
                    "message": f"{sym} crossed above previous week high (₹{pwh:,.1f})",
                    "ltp": cmp, "url": "/jungle",
                })
            prev["above_pwh"] = above_pwh
        if pwl:
            below_pwl = cmp < pwl
            if prev.get("below_pwl") is False and below_pwl:
                broadcast_alert(supabase, {
                    "signal": "PWL_CROSS", "symbol": sym,
                    "message": f"{sym} crossed below previous week low (₹{pwl:,.1f})",
                    "ltp": cmp, "url": "/jungle",
                })
            prev["below_pwl"] = below_pwl

        _tech_state[sym] = prev


# ── PCR crossover (Oct 31 2026, Pawan's ask) ─────────────────────────────
# Fires when an index's Vol PCR crosses its OI PCR (the same crossover the PCR
# page describes in words). Opt-in signal. Hysteresis so a line hovering at
# equality doesn't chatter: a side only changes once the gap exceeds the band,
# and each symbol has a cool-down after firing.
PCR_CROSS_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
PCR_CROSS_BAND = 0.03        # |vol_pcr - oi_pcr| must exceed this to count as a side
PCR_CROSS_COOLDOWN_MIN = 30  # at most one alert per symbol per this many minutes
_pcr_side: dict = {}         # symbol -> "above" | "below" (day state, reset daily)
_pcr_last_fire: dict = {}    # symbol -> epoch seconds
_pcr_day: str = ""


def _check_pcr_crossover(supabase):
    global _pcr_side, _pcr_last_fire, _pcr_day
    import time as _t
    from api.pcr_trend import get_pcr_trend
    from api.push_notifications import broadcast_alert

    today = datetime.now(timezone.utc).date().isoformat()
    if today != _pcr_day:
        _pcr_side, _pcr_last_fire, _pcr_day = {}, {}, today

    for sym in PCR_CROSS_SYMBOLS:
        try:
            pts = get_pcr_trend(sym).get("points") or []
        except Exception as e:
            print(f"[PCRCross] {sym} fetch failed: {e}")
            continue
        if not pts:
            continue
        last = pts[-1]
        oi_pcr, vol_pcr = last.get("pcr"), last.get("vol_pcr")
        if not oi_pcr or not vol_pcr:
            continue
        gap = vol_pcr - oi_pcr
        side = "above" if gap > PCR_CROSS_BAND else "below" if gap < -PCR_CROSS_BAND else None
        if side is None:
            continue
        prev = _pcr_side.get(sym)
        _pcr_side[sym] = side
        if prev is None or prev == side:
            continue  # first reading of the day sets the baseline; no change
        if _t.time() - _pcr_last_fire.get(sym, 0) < PCR_CROSS_COOLDOWN_MIN * 60:
            continue
        _pcr_last_fire[sym] = _t.time()
        word = "above" if side == "above" else "below"
        meaning = ("today's put activity is running heavier than standing positioning"
                   if side == "above" else
                   "today's put activity is easing relative to standing positioning")
        broadcast_alert(supabase, {
            "signal": "PCR_CROSSOVER",
            "symbol": sym,
            "message": f"Vol PCR crossed {word} OI PCR ({vol_pcr:.2f} vs {oi_pcr:.2f}) at {last.get('time')} IST — {meaning}",
            "url": "/pcr",
        })
        print(f"[PCRCross] {sym}: {prev} -> {side} (vol {vol_pcr}, oi {oi_pcr})")
