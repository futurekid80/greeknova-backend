from backend.utils.db import get_supabase
from datetime import datetime, timezone

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

    # Get current snapshot
    data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts).execute().data
    prev_data = supabase.from_("oi_snapshots").select("*").eq("timestamp", ts_prev).execute().data if ts_prev else []

    # Get CMPs
    cmp_data = supabase.from_("cmp_prices")\
        .select("*")\
        .order("timestamp", desc=True)\
        .limit(100)\
        .execute().data

    cmp_map = {}
    seen = set()
    for c in cmp_data:
        if c["symbol"] not in seen:
            cmp_map[c["symbol"]] = c["cmp"]
            seen.add(c["symbol"])

    # Build prev OI map
    prev_map = {}
    for row in prev_data:
        prev_map[f"{row['symbol']}_{row['tradingsymbol']}"] = row

    symbols = list(set(r["symbol"] for r in data))
    confluence_signals = []

    for symbol in symbols:
        rows = [r for r in data if r["symbol"] == symbol]
        ce_rows = [r for r in rows if r["option_type"] == "CE"]
        pe_rows = [r for r in rows if r["option_type"] == "PE"]

        total_ce = sum(r["oi"] for r in ce_rows)
        total_pe = sum(r["oi"] for r in pe_rows)
        if not total_ce and not total_pe:
            continue

        pcr = total_pe / total_ce if total_ce > 0 else 0
        cmp = cmp_map.get(symbol, 0)

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

        # Signal 3: OI spike (vs previous snapshot)
        oi_spike = None
        vol_spike = None
        if prev_data:
            for row in rows:
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

        # Determine confluence strength
        strength = len(active_signals)
        if strength < 2:
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
            "signal_count": strength,
            "bias": bias,
            "ce_wall": ce_wall,
            "pe_wall": pe_wall,
            "dist_ce": round(dist_ce, 1),
            "dist_pe": round(dist_pe, 1),
            "is_index": symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        })

    # Sort by signal count then by PCR extremity
    confluence_signals.sort(key=lambda x: (x["signal_count"], abs(x["pcr"] - 1)), reverse=True)
    return {
        "timestamp": ts,
        "total": len(confluence_signals),
        "signals": confluence_signals
    }
