"""
wall_migration.py
OI Wall Migration Scanner — detects wall shifts, convergence, and price breaches.
Experimental feature — Jun 2026.

Fix log:
- Wall computation: CE/PE wall = highest OI across ALL strikes (no CMP filter)
  This correctly identifies dominant institutional positions regardless of price location.
  Example: BAJAJ-AUTO PE 10,000 (1.5L OI) is the dominant wall even if price < 10,000.
- Coiling detection: catches ce_wall == pe_wall (perfect convergence = 0pt range)
- Range threshold raised to 2.0% to catch more coiling setups
"""

from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

STRIKE_INTERVALS = {
    "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
}
DEFAULT_INTERVAL = 5

def get_wall_migration(supabase) -> dict:
    try:
        ist_now = datetime.now(IST)

        # ── Find last trading weekday ─────────────────────────────────────
        check = ist_now.date()
        for _ in range(5):
            if check.weekday() < 5:
                break
            check -= timedelta(days=1)
        today = check.isoformat()

        # ── Get two most recent timestamps today ──────────────────────────
        ts_res = supabase.from_("oi_snapshots") \
            .select("timestamp") \
            .gte("timestamp", f"{today}T03:00:00+00:00") \
            .lte("timestamp", f"{today}T12:00:00+00:00") \
            .order("timestamp", desc=True) \
            .limit(20).execute()

        # If fewer than 2 snapshots today, look back to last trading day
        if len(ts_res.data or []) < 2:
            for i in range(1, 6):
                prev_day = (check - timedelta(days=i))
                if prev_day.weekday() >= 5:
                    continue
                prev_date = prev_day.isoformat()
                ts_res = supabase.from_("oi_snapshots") \
                    .select("timestamp") \
                    .gte("timestamp", f"{prev_date}T03:00:00+00:00") \
                    .lte("timestamp", f"{prev_date}T12:00:00+00:00") \
                    .order("timestamp", desc=True) \
                    .limit(20).execute()
                if len(ts_res.data or []) >= 2:
                    break

        timestamps = [r["timestamp"] for r in (ts_res.data or [])]
        if len(timestamps) < 2:
            return {
                "signals": [],
                "total": 0,
                "message": "Need at least 2 snapshots for wall migration analysis",
                "trade_date": today,
            }

        ts_latest = timestamps[0]
        ts_prev   = timestamps[4] if len(timestamps) >= 5 else timestamps[-1]

        # ── Fetch latest CMP for all symbols ─────────────────────────────
        cmp_res = supabase.from_("cmp_prices") \
            .select("symbol,cmp") \
            .gte("timestamp", f"{today}T03:00:00+00:00") \
            .order("timestamp", desc=True) \
            .limit(300).execute()

        cmp_map: dict[str, float] = {}
        seen = set()
        for r in (cmp_res.data or []):
            if r["symbol"] not in seen:
                cmp_map[r["symbol"]] = float(r["cmp"])
                seen.add(r["symbol"])

        # ── Fetch OI for both timestamps in one bulk query ────────────────
        oi_res = supabase.from_("oi_snapshots") \
            .select("symbol,strike,option_type,oi,timestamp,expiry") \
            .in_("timestamp", [ts_latest, ts_prev]) \
            .in_("option_type", ["CE", "PE"]) \
            .limit(15000).execute()

        # ── Find nearest expiry per symbol from latest snapshot ───────────
        nearest_expiry: dict = {}
        for r in (oi_res.data or []):
            if r["timestamp"] != ts_latest:
                continue
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if not exp or exp < today:
                continue
            if sym not in nearest_expiry or exp < nearest_expiry[sym]:
                nearest_expiry[sym] = exp

        snap: dict[str, dict] = {ts_latest: {}, ts_prev: {}}
        for r in (oi_res.data or []):
            ts  = r["timestamp"]
            sym = r["symbol"]
            exp = str(r.get("expiry") or "")
            if exp != nearest_expiry.get(sym, ""):
                continue
            s   = float(r["strike"])
            ot  = r["option_type"]
            oi  = int(r["oi"] or 0)
            if sym not in snap[ts]:
                snap[ts][sym] = {}
            if s not in snap[ts][sym]:
                snap[ts][sym][s] = {"ce_oi": 0, "pe_oi": 0}
            snap[ts][sym][s][f"{ot.lower()}_oi"] += oi

        # ── Compute walls for a snapshot ──────────────────────────────────
        def get_walls(strike_map: dict, cmp: float, sym: str) -> dict | None:
            if not strike_map or cmp <= 0:
                return None

            # Auto-detect strike interval
            strikes_sorted = sorted(strike_map.keys())
            if sym in STRIKE_INTERVALS:
                interval = STRIKE_INTERVALS[sym]
            elif len(strikes_sorted) >= 2:
                diffs = [strikes_sorted[i+1] - strikes_sorted[i]
                         for i in range(min(10, len(strikes_sorted)-1))]
                valid_diffs = [d for d in diffs if d > 0]
                interval = min(valid_diffs) if valid_diffs else DEFAULT_INTERVAL
            else:
                interval = DEFAULT_INTERVAL

            # ATM ±10 strike intervals — matches OI Profile exactly
            snapped_atm = round(cmp / interval) * interval
            strike_lower = snapped_atm - (10 * interval)
            strike_upper = snapped_atm + (10 * interval)

            ce_oi: dict = {}
            pe_oi: dict = {}
            for strike, v in strike_map.items():
                if strike < strike_lower or strike > strike_upper:
                    continue
                ce_oi[strike] = v.get("ce_oi", 0)
                pe_oi[strike] = v.get("pe_oi", 0)

            if not ce_oi or not pe_oi:
                return None

            # CE wall = strike with highest CE OI (no CMP filter)
            # PE wall = strike with highest PE OI (no CMP filter)
            # This correctly identifies dominant institutional positions
            # regardless of whether price is above/below the strike.
            # e.g. BAJAJ-AUTO PE 10,000 (1.5L) is the dominant wall
            # even when price = 9,900 (below the strike).
            max_ce = max(ce_oi.values(), default=0)
            max_pe = max(pe_oi.values(), default=0)

            if max_ce == 0 or max_pe == 0:
                return None

            # Apply 10% threshold to filter noise strikes
            ce_sig = {s: v for s, v in ce_oi.items() if v >= max_ce * 0.10}
            pe_sig = {s: v for s, v in pe_oi.items() if v >= max_pe * 0.10}

            if not ce_sig or not pe_sig:
                return None

            ce_wall = max(ce_sig, key=ce_sig.get)
            pe_wall = max(pe_sig, key=pe_sig.get)

            # POC = strike with highest combined CE+PE OI — same as OI Profile
            poc = max(
                strike_map.keys(),
                key=lambda s: strike_map[s].get("ce_oi", 0) + strike_map[s].get("pe_oi", 0)
            )

            # Allow ce_wall == pe_wall (perfect convergence)
            # Only skip if CE wall is strictly BELOW PE wall (impossible structure)
            if ce_wall < pe_wall:
                return None

            trade_range = round(abs(ce_wall - pe_wall), 1)
            trade_range_pct = round(trade_range / cmp * 100, 2) if cmp > 0 else 0

            return {
                "ce_wall":       ce_wall,
                "pe_wall":       pe_wall,
                "ce_wall_oi":    ce_sig[ce_wall],
                "pe_wall_oi":    pe_sig[pe_wall],
                "poc":           poc,
                "range":         trade_range,
                "range_pct":     trade_range_pct,
            }

        # ── Analyze each symbol ───────────────────────────────────────────
        signals = []
        all_symbols = set(snap[ts_latest].keys()) & set(snap[ts_prev].keys())

        for sym in all_symbols:
            cmp = cmp_map.get(sym, 0)
            if cmp <= 0:
                continue

            interval = STRIKE_INTERVALS.get(sym, DEFAULT_INTERVAL)

            latest_walls = get_walls(snap[ts_latest].get(sym, {}), cmp, sym)
            prev_walls   = get_walls(snap[ts_prev].get(sym, {}), cmp, sym)

            if not latest_walls or not prev_walls:
                continue

            ce_now  = latest_walls["ce_wall"]
            pe_now  = latest_walls["pe_wall"]
            ce_prev = prev_walls["ce_wall"]
            pe_prev = prev_walls["pe_wall"]

            alerts = []

            # 1. Price above CE wall — breakout
            if cmp > ce_now:
                dist = round(cmp - ce_now, 1)
                alerts.append({
                    "type": "PRICE_ABOVE_CE_WALL",
                    "label": "Price Above CE Wall",
                    "icon": "🔴",
                    "severity": "HIGH",
                    "detail": f"CMP ₹{cmp:,.0f} above CE wall {ce_now:,.0f} by {dist} pts",
                    "color": "red",
                })

            # 2. Price below PE wall — breakdown
            if cmp < pe_now:
                dist = round(pe_now - cmp, 1)
                alerts.append({
                    "type": "PRICE_BELOW_PE_WALL",
                    "label": "Price Below PE Wall",
                    "icon": "🟢",
                    "severity": "HIGH",
                    "detail": f"CMP ₹{cmp:,.0f} below PE wall {pe_now:,.0f} by {dist} pts",
                    "color": "emerald",
                })

            # 3. Walls converging
            range_now  = latest_walls["range"]
            range_prev = prev_walls["range"]
            if range_prev > 0:
                convergence_pct = round((range_prev - range_now) / range_prev * 100, 1)
                if convergence_pct >= 15:
                    alerts.append({
                        "type": "WALLS_CONVERGING",
                        "label": "Walls Converging",
                        "icon": "⚡",
                        "severity": "MEDIUM",
                        "detail": f"Range compressed {convergence_pct}% · {range_prev:.0f} → {range_now:.0f} pts",
                        "color": "orange",
                    })

            # 4. CE wall shifting up (bullish)
            ce_shift = round(ce_now - ce_prev, 1)
            if ce_shift >= interval:
                alerts.append({
                    "type": "CE_WALL_SHIFT_UP",
                    "label": "CE Wall Shifting Up",
                    "icon": "📈",
                    "severity": "MEDIUM",
                    "detail": f"Resistance moved up {ce_shift} pts · {ce_prev:,.0f} → {ce_now:,.0f}",
                    "color": "emerald",
                })

            # 5. CE wall shifting down (bearish pressure)
            if ce_shift <= -interval:
                alerts.append({
                    "type": "CE_WALL_SHIFT_DOWN",
                    "label": "CE Wall Pressing Down",
                    "icon": "📉",
                    "severity": "MEDIUM",
                    "detail": f"Resistance moving down {abs(ce_shift)} pts · {ce_prev:,.0f} → {ce_now:,.0f}",
                    "color": "red",
                })

            # 6. PE wall shifting down (bearish — support abandoned)
            pe_shift = round(pe_now - pe_prev, 1)
            if pe_shift <= -interval:
                alerts.append({
                    "type": "PE_WALL_SHIFT_DOWN",
                    "label": "PE Wall Shifting Down",
                    "icon": "📉",
                    "severity": "MEDIUM",
                    "detail": f"Support abandoned · {pe_prev:,.0f} → {pe_now:,.0f} ({abs(pe_shift)} pts)",
                    "color": "red",
                })

            # 7. PE wall shifting up (bullish — support building)
            if pe_shift >= interval:
                alerts.append({
                    "type": "PE_WALL_SHIFT_UP",
                    "label": "PE Wall Shifting Up",
                    "icon": "🛡️",
                    "severity": "MEDIUM",
                    "detail": f"Support building higher · {pe_prev:,.0f} → {pe_now:,.0f} (+{pe_shift} pts)",
                    "color": "emerald",
                })

            # 8. Coiling — narrow range or perfect convergence
            # Catches: range < 2% OR ce_wall == pe_wall (0pt range)
            if latest_walls["range_pct"] < 2.0 and (ce_now == pe_now or (cmp > pe_now and cmp < ce_now)):
                alerts.append({
                    "type": "NARROW_RANGE_COILING",
                    "label": "Coiling — Narrow Range",
                    "icon": "🎯",
                    "severity": "LOW",
                    "detail": f"Only {latest_walls['range_pct']}% between walls · {pe_now:,.0f}–{ce_now:,.0f}",
                    "color": "amber",
                })

            if not alerts:
                continue

            sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            alerts.sort(key=lambda a: sev_order.get(a["severity"], 3))

            poc = latest_walls.get("poc", 0)

            # CMP zone classification
            if cmp < pe_now:
                zone = "BELOW_SUPPORT"
                zone_label = "Below Support"
                zone_color = "red"
            elif cmp > ce_now:
                zone = "ABOVE_RESISTANCE"
                zone_label = "Above Resistance"
                zone_color = "emerald"
            else:
                zone = "IN_ZONE"
                zone_label = "In Zone"
                zone_color = "amber"

            # Convergence flag — CE wall, PE wall, POC all within 2% of each other
            convergence_zone = False
            if poc > 0 and cmp > 0:
                levels = [ce_now, pe_now, poc]
                spread = (max(levels) - min(levels)) / cmp * 100
                convergence_zone = spread <= 2.0

            signals.append({
                "symbol":          sym,
                "cmp":             cmp,
                "ce_wall":         ce_now,
                "pe_wall":         pe_now,
                "poc":             poc,
                "ce_wall_prev":    ce_prev,
                "pe_wall_prev":    pe_prev,
                "range_pts":       latest_walls["range"],
                "range_pct":       latest_walls["range_pct"],
                "zone":            zone,
                "zone_label":      zone_label,
                "zone_color":      zone_color,
                "convergence_zone": convergence_zone,
                "alerts":          alerts,
                "top_alert":       alerts[0],
                "alert_count":     len(alerts),
            })

        signals.sort(key=lambda s: (
            0 if s["top_alert"]["severity"] == "HIGH" else
            1 if s["top_alert"]["severity"] == "MEDIUM" else 2,
            -s["alert_count"]
        ))

        return {
            "signals":    signals,
            "total":      len(signals),
            "trade_date": today,
            "ts_latest":  ts_latest,
            "ts_prev":    ts_prev,
            "generated_at": ist_now.strftime("%H:%M IST"),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"signals": [], "total": 0, "error": str(e)}
