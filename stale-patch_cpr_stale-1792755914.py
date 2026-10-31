import ast

path = "api/cpr.py"
with open(path) as f:
    content = f.read()

old1 = '''    if not cpr_rows.data:
        # Query not found — get last available date from DB
        last_res = supabase.from_("cpr_levels")\\
            .select("trade_date")\\
            .order("trade_date", desc=True)\\
            .limit(1).execute()
        fallback_date = last_res.data[0]["trade_date"] if last_res.data else None
        print(f"[CPR] No data for {query_date}, falling back to {fallback_date}")
        if fallback_date:
            cpr_rows = supabase.from_("cpr_levels")\\
                .select("*")\\
                .eq("trade_date", fallback_date)\\
                .limit(500)\\
                .execute()
        if not cpr_rows.data:
            # Absolute last resort — should never happen in normal operation
            print(f"[CPR] No table data at all — falling back to live OHLC")
            return _get_cpr_live()'''

new1 = '''    # FIX (Oct 23 2026): `query_date` is only the INTENDED date -- when the
    # compute job silently fails (as it did Oct 22-23 for the historical_data
    # permission bug) and this falls back to older rows, the response used to
    # still label itself with `query_date`, so the page kept showing "Trade
    # date: today" over data that was actually 2 days stale, with nothing to
    # flag it. `actual_date` now tracks where the served rows really came
    # from, and the response carries `is_stale` so the frontend can warn.
    actual_date = query_date
    if not cpr_rows.data:
        # Query not found — get last available date from DB
        last_res = supabase.from_("cpr_levels")\\
            .select("trade_date")\\
            .order("trade_date", desc=True)\\
            .limit(1).execute()
        fallback_date = last_res.data[0]["trade_date"] if last_res.data else None
        print(f"[CPR] No data for {query_date}, falling back to {fallback_date}")
        if fallback_date:
            actual_date = fallback_date
            cpr_rows = supabase.from_("cpr_levels")\\
                .select("*")\\
                .eq("trade_date", fallback_date)\\
                .limit(500)\\
                .execute()
        if not cpr_rows.data:
            # Absolute last resort — should never happen in normal operation
            print(f"[CPR] No table data at all — falling back to live OHLC")
            return _get_cpr_live()'''

assert old1 in content, "fallback block not found"
content = content.replace(old1, new1)

old2 = '''    result = {
        "data":             results,
        "total":            len(results),
        "trade_date":       query_date,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "table",
    }'''

new2 = '''    result = {
        "data":             results,
        "total":            len(results),
        "trade_date":       actual_date,
        "expected_date":    query_date,
        "is_stale":         actual_date != query_date,
        "confluence_count": sum(1 for r in results if r["confluence"]),
        "narrow_count":     sum(1 for r in results if r["width_priority"] <= 2),
        "source":           "table",
    }'''

assert old2 in content, "result dict not found"
content = content.replace(old2, new2)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
