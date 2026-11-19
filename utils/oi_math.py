"""Nov 2 2026: OI-weighted combined (near + next month) futures OI change.

The old "combined" figure added the two PERCENTAGES (near +4.4% + next +22% = 26.5%), which is
meaningless because the two contracts have very different sizes (PFC: 96M vs 21M shares).
The right number is the change in TOTAL OI: (now_near + now_next) / (prev_near + prev_next) - 1.
Previous OI is recovered from today's OI and its % change."""


def combined_oi_pct(now_near, pct_near, now_next, pct_next):
    try:
        if now_near is None or now_next is None or pct_near is None or pct_next is None:
            return None
        now_near, now_next = float(now_near), float(now_next)
        if now_near <= 0 or now_next <= 0:
            return None
        pn, px = float(pct_near), float(pct_next)
        if 1 + pn / 100 <= 0 or 1 + px / 100 <= 0:
            return None
        prev = now_near / (1 + pn / 100) + now_next / (1 + px / 100)
        if prev <= 0:
            return None
        return round((now_near + now_next) / prev * 100 - 100, 2)
    except Exception:
        return None


def pct_vs_base(now, base):
    """% change of `now` against `base`; None when either is missing or base is not positive."""
    try:
        if now is None or base is None or float(base) <= 0:
            return None
        return round((float(now) - float(base)) / float(base) * 100, 2)
    except Exception:
        return None


def prev_close_baselines(prev_row, rolled):
    """(near_base, next_base) = yesterday's closing OI for the series that is the near / next month TODAY.
    prev_row: yesterday's daily_oi_summary row (fut_oi_close, fut_oi_close_next) or None.
    rolled: True when yesterday's near-month contract expired, so yesterday's next-month contract is today's
    near month and today's next month has no stored baseline (returns None for it)."""
    if not prev_row:
        return None, None
    near, nxt = prev_row.get("fut_oi_close"), prev_row.get("fut_oi_close_next")
    if rolled:
        return nxt, None
    return near, nxt
