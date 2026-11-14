"""Nov 13 2026: replace the closing futures OI we stored from our live feed with NSE's final end-of-day OI.

Why: the live feed's last reading of the day is not the exchange's final figure. The exchange revises OI
overnight (DATAPATTNS 12 Nov: live 469,000 vs final 423,750; the first reading on 13 Nov matched NSE exactly).
Across 222 stocks the stored 12 Nov closing OI differed from the final figure by >1% on 120 stocks.

Source: NSE F&O bhavcopy (OpnIntrst = final OI, ChngInOpnIntrst = change vs previous day's final OI).
Fields touched: fut_oi_close, fut_oi_chg_pct, fut_oi_close_next, fut_oi_chg_pct_next, fut_signal.
The price move (price_chg_pct) is never touched; fut_signal is re-derived from the NEW OI change and the stored
cash price move with the same rule the daily job uses.

dry_run=True (default) writes nothing and logs what would change, including how many Stealth (watch band) and
buildup tags would flip.
"""
import time
from datetime import datetime, timedelta

from services.fut_history_backfill import _fo_rows, _f, _oi_pct, _signal


ROLLOVER_DAYS = 6  # calendar days before near-month expiry (expiry Tuesday: Wed..Tue sessions)


def _stealth(oi, price):
    """Watch band of the Stealth tag: OI building at least 1.0% with a quiet cash price (|move| <= 1.0%)."""
    return oi is not None and price is not None and oi >= 1.0 and abs(price) <= 1.0


def run_fut_oi_final(supabase, start="2026-10-14", end=None, dry_run=True):
    from utils.market_calendar import is_trading_day
    end_d = datetime.strptime(end, "%Y-%m-%d").date() if end else datetime.utcnow().date()
    d = datetime.strptime(start, "%Y-%m-%d").date()
    mode = "DRY RUN (nothing is written)" if dry_run else "WRITE"
    print(f"[fut_oi_final] {mode}: {d} to {end_d}")
    tot = {"rows": 0, "changed": 0, "gt1": 0, "gt3": 0, "gt10": 0, "sig_flips": 0, "stealth_in": 0, "stealth_out": 0}

    while d <= end_d:
        if d.weekday() >= 5 or not is_trading_day(d):
            d += timedelta(days=1)
            continue
        fo = _fo_rows(d)
        time.sleep(1)
        if fo is None:
            print(f"[fut_oi_final] {d}: NSE F&O file not available; skipped")
            d += timedelta(days=1)
            continue
        # Rollover week: near-month OI drains as traders roll into the next series. NSE's day-over-day change then
        # reads as 'unwinding/covering', which is rollover, not conviction. Leave those days as stored.
        try:
            _exp = min(datetime.strptime(v[0].get("XpryDt", "")[:10], "%Y-%m-%d").date() for v in fo.values() if v)
            if (_exp - d).days <= ROLLOVER_DAYS:
                print(f"[fut_oi_final] {d}: rollover week (near expiry {_exp}); left as stored")
                d += timedelta(days=1)
                continue
        except Exception as _e:
            print(f"[fut_oi_final] {d}: could not read expiry ({_e}); continuing")
        rows, lo = [], 0
        while True:
            res = supabase.from_("daily_oi_summary").select(
                "symbol,price_chg_pct,fut_oi_chg_pct,fut_oi_close,fut_oi_chg_pct_next,fut_oi_close_next,fut_signal"
            ).eq("trade_date", d.isoformat()).range(lo, lo + 999).execute().data or []
            rows += res
            if len(res) < 1000:
                break
            lo += 1000
        n = ch = g1 = g3 = g10 = flips = s_in = s_out = 0
        examples, updates = [], []
        for r in rows:
            futs = fo.get(r["symbol"].upper()) or []
            if not futs:
                continue
            n0, n1 = futs[0], (futs[1] if len(futs) > 1 else None)
            new_close = _f(n0.get("OpnIntrst"))
            new_chg = _oi_pct(n0)
            if new_close is None or new_chg is None:
                continue
            old_close = _f(r.get("fut_oi_close"))
            old_chg = _f(r.get("fut_oi_chg_pct"))
            price = _f(r.get("price_chg_pct"))
            n += 1
            if old_close and old_close > 0:
                gap = abs(new_close - old_close) / old_close * 100
                if gap > 1: g1 += 1
                if gap > 3: g3 += 1
                if gap > 10: g10 += 1
            new_sig = _signal(new_chg, price) if price is not None else r.get("fut_signal")
            old_sig = r.get("fut_signal")
            if new_sig != old_sig:
                flips += 1
                if len(examples) < 8:
                    examples.append(f"{r['symbol']} oi {old_chg}->{new_chg} px {price} {old_sig}->{new_sig}")
            if _stealth(new_chg, price) and not _stealth(old_chg, price): s_in += 1
            if _stealth(old_chg, price) and not _stealth(new_chg, price): s_out += 1
            if old_close != new_close or old_chg != new_chg:
                ch += 1
                upd = {"fut_oi_close": new_close, "fut_oi_chg_pct": new_chg, "fut_signal": new_sig}
                if n1 is not None:
                    upd["fut_oi_close_next"] = _f(n1.get("OpnIntrst"))
                    nn = _oi_pct(n1)
                    if nn is not None:
                        upd["fut_oi_chg_pct_next"] = nn
                updates.append((r["symbol"], upd))
        print(f"[fut_oi_final] {d}: matched={n} changed={ch} oi>1%={g1} >3%={g3} >10%={g10} "
              f"signal_flips={flips} stealth_in={s_in} stealth_out={s_out}")
        for e in examples:
            print(f"[fut_oi_final]    e.g. {e}")
        for k, v in (("rows", n), ("changed", ch), ("gt1", g1), ("gt3", g3), ("gt10", g10),
                     ("sig_flips", flips), ("stealth_in", s_in), ("stealth_out", s_out)):
            tot[k] += v
        if not dry_run:
            for sym, upd in updates:
                supabase.from_("daily_oi_summary").update(upd).eq("trade_date", d.isoformat()).eq("symbol", sym).execute()
        d += timedelta(days=1)
    print(f"[fut_oi_final] {mode} TOTAL: {tot}")
    return tot


def run_fut_oi_final_today(supabase):
    """Nightly job: write NSE's final OI for today (IST).
    Returns today's date string once written, "" on a non-trading day, None if NSE's file is not out yet."""
    from datetime import timezone
    today_d = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    from utils.market_calendar import is_trading_day
    if not is_trading_day(today_d):
        return ""
    if _fo_rows(today_d) is None:
        print(f"[fut_oi_final] nightly: NSE file for {today_d} not out yet")
        return None
    run_fut_oi_final(supabase, start=today_d.isoformat(), end=today_d.isoformat(), dry_run=False)
    return today_d.isoformat()


def refresh_eod_caches(supabase):
    """After final OI is written, drop every saved or in-memory copy of the old numbers so screens rebuild from the
    corrected daily_oi_summary rows. Each step is independent; one failing never blocks the others."""
    import pytz
    done = {}
    try:
        from api.positional_intelligence import clear_positional_intelligence_cache
        clear_positional_intelligence_cache(); done["positional_intelligence"] = "cleared"
    except Exception as e:
        done["positional_intelligence"] = f"error: {e}"
    try:
        from api.positional_radar import clear_radar_cache
        clear_radar_cache(); done["radar"] = "cleared"
    except Exception as e:
        done["radar"] = f"error: {e}"
    try:
        import api.delivery_confluence as dc
        dc._confluence_cache = {}; dc._confluence_cache_time = 0
        done["delivery_confluence"] = "cleared"
    except Exception as e:
        done["delivery_confluence"] = f"error: {e}"
    try:
        # Vol+OI Breakout saves its closing snapshot to the database after the first request post-close, so it
        # would keep the provisional numbers. Rebuild it from the corrected rows and save over the old snapshot.
        import api.vol_oi_breakout as vb
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        vb._breakout_cache = {}; vb._breakout_cache_time = 0.0
        fresh = vb._get_eod_from_summary(supabase, now_ist)
        if fresh.get("signals") is not None and fresh.get("date") == vb._resolve_eod_trade_date(now_ist):
            vb._save_to_supabase(supabase, fresh["signals"], fresh["total"], fresh["date"])
            vb._breakout_cache = fresh
            vb._breakout_cache_time = __import__("time").time()
            done["vol_oi_breakout"] = f"rebuilt {fresh['total']} signals for {fresh['date']}"
        else:
            done["vol_oi_breakout"] = "skipped (date mismatch)"
    except Exception as e:
        done["vol_oi_breakout"] = f"error: {e}"
    print(f"[fut_oi_final] cache refresh: {done}")
    return done
