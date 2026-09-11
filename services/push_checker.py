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


def _reset_if_new_day():
    global _previous_keys, _last_reset_date
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _last_reset_date:
        _previous_keys = set()
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
        body = (
            f"OI {'+' if oi_pct > 0 else ''}{oi_pct}% in 5 min | "
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
