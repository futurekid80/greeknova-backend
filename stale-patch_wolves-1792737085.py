path = "services/wolves_scanner.py"
with open(path, "r") as f:
    content = f.read()
orig = content

# ---------- 1. Add the cmp_prices-based candle reconstruction helper ----------
anchor = '''def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]'''

assert anchor in content
addition = anchor + '''


def _cmp_prices_candles(supabase, symbol: str, from_date: str, to_date: str, freq: str) -> list[dict]:
    """BUG FIX (Oct 23 2026): kite.historical_data() has no real permission
    on this API key (Manish's Kite Connect app is personal/multiuser, not
    the public Connect API with the Historical Data add-on) -- it started
    rejecting every symbol with "Insufficient permission for that call" on
    2026-10-22, silently zeroing out both the daily/weekly/monthly cache
    job and the hourly live tick (217 errored, 0 scanned). Same root cause
    and same fix shape as api/cpr.py's prev-close fix: reconstruct OHLC
    candles from cmp_prices, which is populated independently every ~5 min
    during market hours via kite.quote() (no Historical Data permission
    needed). `freq` is a pandas resample alias -- "D" for daily, "h" for
    hourly. Returns the same shape _candles_to_df()/kite.historical_data()
    produce (list of {date, open, high, low, close, volume} dicts) so every
    caller downstream is unchanged.
    """
    try:
        to_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
        rows = supabase.from_("cmp_prices")\\
            .select("cmp, timestamp")\\
            .eq("symbol", symbol)\\
            .gte("timestamp", f"{from_date}T00:00:00+00:00")\\
            .lt("timestamp", to_dt.strftime("%Y-%m-%dT00:00:00+00:00"))\\
            .order("timestamp")\\
            .limit(50000)\\
            .execute()
    except Exception as e:
        print(f"[Wolves] cmp_prices fetch {symbol}: {e}")
        return []

    data = rows.data or []
    if not data:
        return []

    df = pd.DataFrame(data)
    df["cmp"] = pd.to_numeric(df["cmp"], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.dropna(subset=["cmp"]).set_index("timestamp").sort_index()
    if df.empty:
        return []

    ohlc = df["cmp"].resample(freq).ohlc().dropna(how="all")
    ohlc = ohlc.dropna(subset=["open", "high", "low", "close"])
    if ohlc.empty:
        return []
    ohlc["volume"] = 0
    ohlc = ohlc.reset_index().rename(columns={"timestamp": "date"})
    return ohlc.to_dict("records")'''

content = content.replace(anchor, addition, 1)

# ---------- 2. refresh_tf_cache(): daily candles via cmp_prices instead of kite.historical_data() ----------
old_daily_fetch = '''        try:
            daily_candles = kite.historical_data(instrument_token=token, from_date=daily_from, to_date=to_date, interval="day", continuous=False, oi=False)
            time.sleep(0.34)
        except Exception as e:
            print(f"[Wolves] {symbol}: daily fetch failed -- {e}")
            errored += 1
            continue

        daily_df = _candles_to_df(daily_candles)
        if daily_df.empty:
            skipped += 1
            continue

        daily_ind = add_indicators(daily_df)
        weekly_ind = add_indicators(resample_ohlcv(daily_df, "W-FRI"))
        monthly_ind = add_indicators(resample_ohlcv(daily_df, "ME"))

        row = {"symbol": symbol, "computed_date": today_ist.isoformat()}
        for tf_name, tf_df in [("daily", daily_ind), ("weekly", weekly_ind), ("monthly", monthly_ind)]:
            e = evaluate_timeframe(tf_df, tf_name.capitalize())
            row[f"{tf_name}_adx_cross_bull"] = e["adx_cross_bull"]
            row[f"{tf_name}_adx_cross_bear"] = e["adx_cross_bear"]
            row[f"{tf_name}_close"] = round(e["close"], 2) if not pd.isna(e["close"]) else None
            row[f"{tf_name}_adx"] = round(e["adx"], 2) if not pd.isna(e["adx"]) else None
            row[f"{tf_name}_rsi"] = round(e["rsi"], 2) if not pd.isna(e["rsi"]) else None
            row[f"{tf_name}_rsi_cross_bull"] = e["rsi_cross_bull"]
            row[f"{tf_name}_rsi_cross_bear"] = e["rsi_cross_bear"]
            row[f"{tf_name}_adx_bull"] = e["adx_bull"]
            row[f"{tf_name}_adx_bear"] = e["adx_bear"]
            row[f"{tf_name}_bb_bull"] = e["bb_bull"]
            row[f"{tf_name}_bb_bear"] = e["bb_bear"]
            row[f"{tf_name}_bb_upper_cross"] = e["bb_upper_cross"]
            row[f"{tf_name}_bb_lower_cross"] = e["bb_lower_cross"]
            row[f"{tf_name}_bb_compression_breakout"] = e["bb_compression_breakout"]
            row[f"{tf_name}_bb_compression_breakdown"] = e["bb_compression_breakdown"]
        rows.append(row)
        cached += 1'''

new_daily_fetch = '''        daily_candles = _cmp_prices_candles(supabase, symbol, daily_from, to_date, "D")
        if not daily_candles:
            skipped += 1
            continue

        daily_df = _candles_to_df(daily_candles)
        if daily_df.empty:
            skipped += 1
            continue

        daily_ind = add_indicators(daily_df)
        weekly_ind = add_indicators(resample_ohlcv(daily_df, "W-FRI"))
        monthly_ind = add_indicators(resample_ohlcv(daily_df, "ME"))

        # BUG FIX (Oct 23 2026) cont'd: cmp_prices only goes back to
        # 2026-05 (~6 months), plenty of daily bars for Daily ADX/RSI/BB,
        # but nowhere near enough for a real Weekly/Monthly ADX(14) --
        # that needs ~28+ bars of its own timeframe to stabilize, i.e.
        # ~28 weeks / ~28 months of history. Rather than write a noisy or
        # flat-out invalid Apex Wolf (weekly/monthly) reading over a real
        # one, only overwrite weekly_*/monthly_* once there's genuinely
        # enough resampled history (same >=30-bar bar BB Width Pct already
        # uses) -- omitted keys are left untouched by the upsert, so the
        # last real cached value (from before Historical Data broke) stays
        # until there's enough cmp_prices history to trust a fresh one.
        # Weekly should self-heal in a month or two; Monthly needs a
        # dedicated properly-subscribed Kite app to ever be timely again.
        row = {"symbol": symbol, "computed_date": today_ist.isoformat()}
        tf_sources = [("daily", daily_ind, True)]
        tf_sources.append(("weekly", weekly_ind, len(weekly_ind) >= 30))
        tf_sources.append(("monthly", monthly_ind, len(monthly_ind) >= 30))
        for tf_name, tf_df, trust_it in tf_sources:
            if not trust_it:
                continue
            e = evaluate_timeframe(tf_df, tf_name.capitalize())
            row[f"{tf_name}_adx_cross_bull"] = e["adx_cross_bull"]
            row[f"{tf_name}_adx_cross_bear"] = e["adx_cross_bear"]
            row[f"{tf_name}_close"] = round(e["close"], 2) if not pd.isna(e["close"]) else None
            row[f"{tf_name}_adx"] = round(e["adx"], 2) if not pd.isna(e["adx"]) else None
            row[f"{tf_name}_rsi"] = round(e["rsi"], 2) if not pd.isna(e["rsi"]) else None
            row[f"{tf_name}_rsi_cross_bull"] = e["rsi_cross_bull"]
            row[f"{tf_name}_rsi_cross_bear"] = e["rsi_cross_bear"]
            row[f"{tf_name}_adx_bull"] = e["adx_bull"]
            row[f"{tf_name}_adx_bear"] = e["adx_bear"]
            row[f"{tf_name}_bb_bull"] = e["bb_bull"]
            row[f"{tf_name}_bb_bear"] = e["bb_bear"]
            row[f"{tf_name}_bb_upper_cross"] = e["bb_upper_cross"]
            row[f"{tf_name}_bb_lower_cross"] = e["bb_lower_cross"]
            row[f"{tf_name}_bb_compression_breakout"] = e["bb_compression_breakout"]
            row[f"{tf_name}_bb_compression_breakdown"] = e["bb_compression_breakdown"]
        rows.append(row)
        cached += 1'''

assert old_daily_fetch in content, "old_daily_fetch not found"
content = content.replace(old_daily_fetch, new_daily_fetch, 1)

# ---------- 3. run_live_tick(): hourly candles via cmp_prices instead of kite.historical_data() ----------
old_hourly_fetch = '''        try:
            hourly_candles = kite.historical_data(instrument_token=token, from_date=hourly_from, to_date=to_date, interval="60minute", continuous=False, oi=False)
            time.sleep(0.34)
        except Exception as e:
            print(f"[Wolves] {symbol}: hourly fetch failed -- {e}")
            errored += 1
            continue

        hourly_df = _candles_to_df(hourly_candles)'''

new_hourly_fetch = '''        hourly_candles = _cmp_prices_candles(supabase, symbol, hourly_from, to_date, "h")
        if not hourly_candles:
            skipped += 1
            continue

        hourly_df = _candles_to_df(hourly_candles)'''

assert old_hourly_fetch in content, "old_hourly_fetch not found"
content = content.replace(old_hourly_fetch, new_hourly_fetch, 1)

assert content != orig
with open("services/wolves_scanner_new.py", "w") as f:
    f.write(content)

import ast
ast.parse(content)
print("OK - patched and syntax-valid, written to services/wolves_scanner_new.py")
