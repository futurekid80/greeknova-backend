import ast

path = "api/daily_oi_summary.py"
with open(path) as f:
    content = f.read()

old = '''        # ── Server-side aggregation via RPC ───────────────────────────────
        # ── Fetch official NSE settlement close via Kite historical_data ──
        official_close_map = {}
        try:
            from services.kite_auth import get_kite_client
            kite = get_kite_client()
            instruments = kite.instruments("NSE")
            token_map = {}
            INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "FINNIFTY": 257801, "MIDCPNIFTY": 288009, "SENSEX": 265}
            token_map.update(INDEX_TOKENS)
            for inst in instruments:
                if inst["tradingsymbol"] in SYMBOLS:
                    token_map[inst["tradingsymbol"]] = inst["instrument_token"]

            import time as _time
            for sym in SYMBOLS:
                token = token_map.get(sym)
                if not token:
                    continue
                try:
                    candles = kite.historical_data(
                        instrument_token=token,
                        from_date=trade_date,
                        to_date=trade_date,
                        interval="day",
                        continuous=False,
                        oi=False,
                    )
                    for c in candles:
                        if str(c["date"])[:10] == trade_date:
                            official_close_map[sym] = float(c["close"])
                            break
                    _time.sleep(0.05)
                except Exception as e:
                    print(f"[DailyOI] Kite close {sym}: {e}")
        except Exception as e:
            print(f"[DailyOI] Kite init failed: {e}")

       # ── Fetch official NSE settlement close via Kite ──────────────────
        official_close_map = {}
        try:
            from services.kite_auth import get_kite_client
            import time as _time
            kite = get_kite_client()
            instruments = kite.instruments("NSE")
            token_map = {**{"NIFTY": 256265, "BANKNIFTY": 260105, "FINNIFTY": 257801, "MIDCPNIFTY": 288009, "SENSEX": 265}}
            for inst in instruments:
                if inst["tradingsymbol"] in SYMBOLS:
                    token_map[inst["tradingsymbol"]] = inst["instrument_token"]
            for sym in SYMBOLS:
                token = token_map.get(sym)
                if not token:
                    continue
                try:
                    candles = kite.historical_data(
                        instrument_token=token,
                        from_date=trade_date,
                        to_date=trade_date,
                        interval="day",
                        continuous=False,
                        oi=False,
                    )
                    for c in candles:
                        if str(c["date"])[:10] == trade_date:
                            official_close_map[sym] = float(c["close"])
                            break
                    _time.sleep(0.05)
                except Exception as e:
                    print(f"[DailyOI] Kite close {sym}: {e}")
        except Exception as e:
            print(f"[DailyOI] Kite init failed: {e}")'''

new = '''        # ── Server-side aggregation via RPC ───────────────────────────────
        # ── Official close, trade_date's own session ──────────────────────
        # FIX (Oct 23 2026): kite.historical_data() has no real permission
        # on this API key (same root cause as the CPR/Wolves fixes earlier
        # today -- see api/cpr.py and services/wolves_scanner.py). This used
        # to call it TWICE (one block's result was immediately discarded by
        # the other -- dead code, ~20s of wasted kite.instruments()+per-symbol
        # historical_data() calls for a map that was overwritten) on top of
        # being broken. Replaced with a single cmp_prices-based close --
        # last tick of trade_date's own session, same pattern as cpr.py's
        # prev-day close.
        official_close_map = {}
        try:
            day_start = f"{trade_date}T00:00:00+00:00"
            from datetime import datetime as _dt, timedelta as _td
            day_end = (_dt.strptime(trade_date, "%Y-%m-%d") + _td(days=1)).isoformat() + "T00:00:00+00:00"
            for sym in SYMBOLS:
                try:
                    rows = supabase.from_("cmp_prices")\\
                        .select("cmp, timestamp")\\
                        .eq("symbol", sym)\\
                        .gte("timestamp", day_start)\\
                        .lt("timestamp", day_end)\\
                        .order("timestamp")\\
                        .limit(2000)\\
                        .execute()
                    cmps = [float(r["cmp"]) for r in (rows.data or []) if r.get("cmp")]
                    if cmps:
                        official_close_map[sym] = cmps[-1]
                except Exception as e:
                    print(f"[DailyOI] cmp_prices close {sym}: {e}")
        except Exception as e:
            print(f"[DailyOI] Official close computation failed: {e}")'''

assert old in content, "duplicate historical_data block not found"
content = content.replace(old, new)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
