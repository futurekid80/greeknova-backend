from utils.db import get_supabase
from datetime import datetime, timezone, date as date_type
from collections import defaultdict

def get_confluence():
    supabase = get_supabase()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Get latest timestamp
    latest = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .order("timestamp", desc=True)\
        .limit(1)\
        .execute()

    if not latest.data:
        return {"signals": []}

    ts = latest.data[0]["timestamp"]

    # Get previous timestamp
    prev = supabase.from_("oi_snapshots")\
        .select("timestamp")\
        .order("timestamp", desc=True)\
        .limit(2)\
        .execute()

    ts_prev = prev.data[1]["timestamp"] if len(prev.data) > 1 else None

    # ── Paginated fetch for current snapshot ──────────────────────────────────
    data = []
    for offset in range(0, 200000, 1000):
        batch = supabase.from_("oi_snapshots")\
            .select("*")\
            .eq("timestamp", ts)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        data.extend(batch.data)
        if len(batch.data) < 1000:
            break

    # ── Paginated fetch for previous snapshot ─────────────────────────────────
    prev_data = []
    if ts_prev:
        for offset in range(0, 200000, 1000):
            batch = supabase.from_("oi_snapshots")\
                .select("*")\
                .eq("timestamp", ts_prev)\
                .range(offset, offset + 999)\
                .execute()
            if not batch.data:
                break
            prev_data.extend(batch.data)
            if len(batch.data) < 1000:
                break

    # ── CMP — paginate to cover all 66 symbols ───────────────────────────────
    cmp_raw = []
    for offset in range(0, 10000, 1000):
        batch = supabase.from_("cmp_prices")\
            .select("*")\
            .order("timestamp", desc=True)\
            .range(offset, offset + 999)\
            .execute()
        if not batch.data:
            break
        cmp_raw.extend(batch.data)
        if len(batch.data) < 1000:
            break

    cmp_map = {}
    seen = set()
    for c in cmp_raw:
        if c["symbol"] not in seen:
            cmp_map[c["symbol"]] = c["cmp"]
            seen.add(c["symbol"])

    # Build prev OI map
    prev_map = {}
    for row in prev_data:
        prev_map[f"{row['symbol']}_{row['tradingsymbol']}"] = row

    # ── Nearest expiry filter ─────────────────────────────────────────────────
    today_str = date_type.today().isoformat()

    symbols = list(set(r["symbol"] for r in data))
    confluence_signals = []

    for symbol in symbols:
        rows = [r for r in data if r["symbol"] == symbol]

        # Filter to nearest expiry only
        expiries = sorted(set(
            r["expiry"] for r in rows
            if r.get("expiry") and r["expiry"] >= today_str
        ))
        nearest_expiry = expiries[0] if expiries else None
        if nearest_expiry:
            rows = [r for r in rows if r["expiry"] == nearest_expiry]

        ce_rows = [r for r in rows if r["option_type"] == "CE"]
        pe_rows = [r for r in rows if r["option_type"] == "PE"]

        total_ce = sum(r["oi"] for r in ce_rows)
        total_pe = sum(r["oi"] for r in pe_rows)
        if not total_ce and not total_pe:
            continue

        cmp = cmp_map.get(symbol, 0)

        # PCR: ATM ±10 strikes only
        strikes = sorted(set(r["strike"] for r in rows))
        if cmp > 0 and strikes:
            atm = min(strikes, key=lambda s: abs(s - cmp))
            atm_idx = strikes.index(atm)
            pcr_set = set(strikes[max(0, atm_idx - 10):atm_idx + 11])
            pcr_ce = sum(r["oi"] for r in ce_rows if r["strike"] in pcr_set)
            pcr_pe = sum(r["oi"] for r in pe_rows if r["strike"] in pcr_set)
        else:
            pcr_ce = total_ce
            pcr_pe = total_pe

        pcr = pcr_pe / pcr_ce if pcr_ce > 0 else 0

        # Signal 1: Scanner signal
        ratio = total_pe / (total_ce + total_pe) if (total_ce + total_pe) > 0 else 0
        if pcr > 1.4: scanner_signal = "PUT_WRITING"
        elif pcr < 0.6: scanner_signal = "CALL_WRITING"
        elif 0.44 < ratio < 0.56: scanner_signal = "BATTLEGROUND"
        else: scanner_signal = "SQUEEZE"

        # Signal 2: OI structure
        ce_wall = max(ce_rows, key=lambda x: x["oi"])["strike"] if ce_rows else 0
        pe_wall = max(pe_rows, key=lambda x: x["oi"])["strike"] if pe_rows else 0
        dist_ce = ((ce_wall - cmp) / cmp * 100) if ce_wall > cmp > 0 else 0
        dist_pe = ((cmp - pe_wall) / cmp * 100) if pe_wall < cmp > 0 else 0

        if dist_ce <= 0.5: structure = "Breakout Watch"
        elif dist_pe <= 0.5: structure = "Breakdown Watch"
        elif dist_ce <= 2: structure = "Resistance Test"
        elif dist_pe <= 2: structure = "Support Test"
        elif dist_ce < dist_pe: structure = "Upper Range"
        elif dist_pe < dist_ce: structure = "Lower Range"
        else: structure = "Mid Range"

        # Signal 3: OI spike vs previous snapshot
        oi_spike = None
        vol_spike = None
        if prev_data:
            all_sym_rows = [r for r in data if r["symbol"] == symbol]
            for row in all_sym_rows:
                key = f"{symbol}_{row['tradingsymbol']}"
                prev_row = prev_map.get(key)
                if not prev_row:
                    continue
                old_oi = prev_row["oi"] or 0
                new_oi = row["oi"] or 0
                old_vol = prev_row["volume"] or 0
                new_vol = row["volume"] or 0

                if old_oi > 1000:
                    oi_pct = (new_oi - old_oi) / old_oi * 100
                    if abs(oi_pct) >= 10:
                        oi_spike = {
                            "strike": row["strike"],
                            "option_type": row["option_type"],
                            "oi_pct": round(oi_pct, 1),
                            "direction": "BUILD" if oi_pct > 0 else "UNWIND"
                        }

                if old_vol > 10000:
                    vol_pct = (new_vol - old_vol) / old_vol * 100
                    if vol_pct >= 20:
                        oi_pct_for_vol = (new_oi - old_oi) / old_oi * 100 if old_oi > 0 else 0
                        vol_signal = "FRESH_BUILD" if oi_pct_for_vol > 5 else "UNWINDING" if oi_pct_for_vol < -5 else "CHURN"
                        vol_spike = {
                            "strike": row["strike"],
                            "option_type": row["option_type"],
                            "vol_pct": round(vol_pct, 1),
                            "signal": vol_signal
                        }

        # Count active signals
        active_signals = []
        if scanner_signal in ["CALL_WRITING", "PUT_WRITING"]: active_signals.append(scanner_signal)
        if structure in ["Breakout Watch", "Breakdown Watch", "Resistance Test", "Support Test"]: active_signals.append(structure)
        if oi_spike: active_signals.append(f"OI {oi_spike['direction']} {oi_spike['option_type']} {oi_spike['strike']}")
        if vol_spike: active_signals.append(f"Vol {vol_spike['signal']} {vol_spike['option_type']} {vol_spike['strike']}")

        if len(active_signals) < 2:
            continue

        # Determine overall bias
        bearish_signals = sum(1 for s in active_signals if any(x in s for x in ["CALL_WRITING", "Breakdown", "Resistance", "BUILD CE", "FRESH_BUILD CE"]))
        bullish_signals = sum(1 for s in active_signals if any(x in s for x in ["PUT_WRITING", "Breakout", "Support", "BUILD PE", "FRESH_BUILD PE"]))

        if bearish_signals > bullish_signals: bias = "BEARISH"
        elif bullish_signals > bearish_signals: bias = "BULLISH"
        else: bias = "MIXED"

        confluence_signals.append({
            "symbol": symbol,
            "cmp": cmp,
            "pcr": round(pcr, 2),
            "scanner_signal": scanner_signal,
            "oi_structure": structure,
            "oi_spike": oi_spike,
            "vol_spike": vol_spike,
            "active_signals": active_signals,
            "signal_count": len(active_signals),
            "bias": bias,
            "ce_wall": ce_wall,
            "pe_wall": pe_wall,
            "dist_ce": round(dist_ce, 1),
            "dist_pe": round(dist_pe, 1),
            "is_index": symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        })

    confluence_signals.sort(key=lambda x: (x["signal_count"], abs(x["pcr"] - 1)), reverse=True)

    # ── MOMENTUM SCANNER ──────────────────────────────────────────────────────
    # Compares today's open (first CMP snapshot) vs latest CMP
    # Shows stocks moving ≥2% from market open (9:15 IST) to now
    # Much more useful than 25-min window — user sees day's winners/losers instantly
    momentum_signals = []
    try:
        # Fetch ALL of today's CMP snapshots for all symbols
        cmp_today = supabase.from_("cmp_prices")\
            .select("symbol, cmp, timestamp")\
            .gte("timestamp", today + "T00:00:00+00:00")\
            .order("timestamp", desc=False)\
            .limit(5000)\
            .execute().data or []

        # Group by symbol — ascending order so first = open, last = latest
        cmp_by_symbol: dict = defaultdict(list)
        for row in cmp_today:
            cmp_by_symbol[row["symbol"]].append(float(row["cmp"]))

        # Build OI change map for confirmation (latest vs prev snapshot)
        oi_chg_map: dict = {}
        for sym in symbols:
            sym_new = [r for r in data if r["symbol"] == sym]
            sym_old = [r for r in prev_data if r["symbol"] == sym]
            new_total = sum(r["oi"] for r in sym_new)
            old_total = sum(r["oi"] for r in sym_old)
            if old_total > 0:
                oi_chg_map[sym] = round((new_total - old_total) / old_total * 100, 2)
            else:
                oi_chg_map[sym] = 0.0

        for sym, prices in cmp_by_symbol.items():
            if len(prices) < 2:
                continue

            # First price of day = open, last price = current
            open_cmp = prices[0]
            current_cmp = prices[-1]

            if open_cmp <= 0:
                continue

            # Open-to-now move
            price_chg_pct = round((current_cmp - open_cmp) / open_cmp * 100, 2)

            # Only show stocks moving ≥2% from open
            if abs(price_chg_pct) < 2.0:
                continue

            direction = "BULLISH" if price_chg_pct > 0 else "BEARISH"
            oi_chg = oi_chg_map.get(sym, 0.0)

            # OI confirms: rising OI = fresh positions supporting the move
            oi_confirms = oi_chg > 3.0

            # Vol confirms: check if this symbol has a vol spike in confluence
            sym_conf = next((s for s in confluence_signals if s["symbol"] == sym), None)
            vol_confirms = bool(sym_conf and sym_conf.get("vol_spike"))

            # PCR from confluence data if available
            sym_pcr = sym_conf["pcr"] if sym_conf else 0.0

            # Conviction score 1-4
            conviction = 1
            if abs(price_chg_pct) >= 3.0: conviction += 1  # strong move
            if oi_confirms: conviction += 1                  # OI backing move
            if vol_confirms: conviction += 1                 # volume spike too

            momentum_signals.append({
                "symbol": sym,
                "cmp": current_cmp,
                "open_cmp": open_cmp,
                "price_chg_pct": price_chg_pct,
                "direction": direction,
                "pcr": sym_pcr,
                "oi_confirms": oi_confirms,
                "oi_chg_pct": oi_chg,
                "vol_confirms": vol_confirms,
                "conviction": conviction,
                "is_index": sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
            })

        # Sort by absolute move size — biggest movers first
        momentum_signals.sort(key=lambda x: abs(x["price_chg_pct"]), reverse=True)

    except Exception as e:
        print(f"[Momentum] Error: {e}")

    return {
        "timestamp": ts,
        "total": len(confluence_signals),
        "signals": confluence_signals,
        "momentum": {
            "total": len(momentum_signals),
            "signals": momentum_signals,
            "threshold_pct": 2.0,
            "window": "open_to_now",
        }
    }
