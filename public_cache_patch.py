import os

path = os.path.expanduser("~/mnt/optionspulse/main.py")
with open(path) as f:
    src = f.read()

edits = [
    (
        '''@app.get("/public/landing-highlights")
def public_landing_highlights(response: Response):
    """Curated, public-safe summary of real recent GreekNova signals, built
    for external showcase use (e.g. the Claude Design landing page) --
    Aug 9 2026. Deliberately NOT the same as opening up the full API:
    - Only a small, curated subset of fields (no raw internal data)
    - Its own permissive CORS header, set manually on just this route,
      so the main API's origin allowlist (protecting authenticated
      production traffic) is completely untouched
    - Framed with plain-language, SEBI-safe wording baked into the
      response itself, not left to whoever consumes it to get right
    """
    response.headers["Access-Control-Allow-Origin"] = "*"
    try:''',
        '''@app.get("/public/landing-highlights")
def public_landing_highlights(response: Response):
    """Curated, public-safe summary of real recent GreekNova signals, built
    for external showcase use (e.g. the Claude Design landing page) --
    Aug 9 2026. Deliberately NOT the same as opening up the full API:
    - Only a small, curated subset of fields (no raw internal data)
    - Its own permissive CORS header, set manually on just this route,
      so the main API's origin allowlist (protecting authenticated
      production traffic) is completely untouched
    - Framed with plain-language, SEBI-safe wording baked into the
      response itself, not left to whoever consumes it to get right

    PERF FIX (Oct 21 2026, launch-day incident): this ran 4 full
    whole-universe scans (volume breakout, first-hour breakout, weekly
    OI buildup, fresh-build winners) from scratch on every single call,
    completely uncached and unauthenticated -- once get.greeknova.com
    started getting real traffic from the trial/launch push, concurrent
    visitors hammering this one public route stacked up DB connections
    and dragged down latency platform-wide (traced via Railway's HTTP
    metrics: one call measured 80+ seconds). Added the same
    function-attribute TTL cache already used by /index-data -- 2 min is
    safe since underlying data only refreshes every 5 min anyway.
    """
    import time
    cache = getattr(public_landing_highlights, '_cache', None)
    cache_time = getattr(public_landing_highlights, '_cache_time', 0)
    if cache and (time.time() - cache_time) < 120:
        response.headers["Access-Control-Allow-Origin"] = "*"
        return cache
    response.headers["Access-Control-Allow-Origin"] = "*"
    try:'''
    ),
    (
        '''            "disclaimer": "For informational and educational purposes only. Not SEBI registered. Not investment advice. This is a curated selection of positive outcomes for illustration, not an exhaustive or representative sample of all signals, and does not reflect overall win rate. Past signals do not guarantee future results.",
        }
    except Exception as e:
        return {"highlights": [], "count": 0, "error": str(e)}

@app.get("/public/live-signals")
def public_live_signals(response: Response):
    """Public, curated live signal ticker for external showcase use (the
    Claude Design landing page's "Top Signals" strip) -- Aug 10 2026.
    Blends the real signal categories from Positional Intelligence
    (STEALTH, LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING,
    LONG_UNWINDING) into one ranked list. Uses the genuine vocabulary
    already used throughout the app, not placeholder tag names."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    try:
        from api.positional_intelligence import get_positional_intelligence
        pi = get_positional_intelligence(min_consec=0)''',
        '''            "disclaimer": "For informational and educational purposes only. Not SEBI registered. Not investment advice. This is a curated selection of positive outcomes for illustration, not an exhaustive or representative sample of all signals, and does not reflect overall win rate. Past signals do not guarantee future results.",
        }
        public_landing_highlights._cache = result
        public_landing_highlights._cache_time = time.time()
        return result
    except Exception as e:
        return {"highlights": [], "count": 0, "error": str(e)}

@app.get("/public/live-signals")
def public_live_signals(response: Response):
    """Public, curated live signal ticker for external showcase use (the
    Claude Design landing page's "Top Signals" strip) -- Aug 10 2026.
    Blends the real signal categories from Positional Intelligence
    (STEALTH, LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING,
    LONG_UNWINDING) into one ranked list. Uses the genuine vocabulary
    already used throughout the app, not placeholder tag names.

    PERF FIX (Oct 21 2026): same launch-day incident as
    /public/landing-highlights -- added the same 2-min TTL cache so
    concurrent landing-page visitors don't each trigger a fresh
    get_positional_intelligence() scan."""
    import time
    cache = getattr(public_live_signals, '_cache', None)
    cache_time = getattr(public_live_signals, '_cache_time', 0)
    if cache and (time.time() - cache_time) < 120:
        response.headers["Access-Control-Allow-Origin"] = "*"
        return cache
    response.headers["Access-Control-Allow-Origin"] = "*"
    try:
        from api.positional_intelligence import get_positional_intelligence
        pi = get_positional_intelligence(min_consec=0)'''
    ),
    (
        '''        return {"signals": deduped[:8], "count": len(deduped[:8])}
    except Exception as e:
        return {"signals": [], "count": 0, "error": str(e)}

@app.get("/public/stealth-featured")
def public_stealth_featured(response: Response):
    """Public, single best current Stealth Buildup detection for the
    landing page's flagship-feature visual -- Aug 10 2026. Returns the
    highest-tier, most-established one (ELITE beats STRONG beats WATCH,
    more sessions beats fewer) with its real oi_history for the curve."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    try:
        from api.positional_intelligence import get_positional_intelligence
        pi = get_positional_intelligence(min_consec=0)
        stealth = pi.get("stealth_buildup") or []''',
        '''        result = {"signals": deduped[:8], "count": len(deduped[:8])}
        public_live_signals._cache = result
        public_live_signals._cache_time = time.time()
        return result
    except Exception as e:
        return {"signals": [], "count": 0, "error": str(e)}

@app.get("/public/stealth-featured")
def public_stealth_featured(response: Response):
    """Public, single best current Stealth Buildup detection for the
    landing page's flagship-feature visual -- Aug 10 2026. Returns the
    highest-tier, most-established one (ELITE beats STRONG beats WATCH,
    more sessions beats fewer) with its real oi_history for the curve.

    PERF FIX (Oct 21 2026): same launch-day incident -- added the same
    2-min TTL cache."""
    import time
    cache = getattr(public_stealth_featured, '_cache', None)
    cache_time = getattr(public_stealth_featured, '_cache_time', 0)
    if cache and (time.time() - cache_time) < 120:
        response.headers["Access-Control-Allow-Origin"] = "*"
        return cache
    response.headers["Access-Control-Allow-Origin"] = "*"
    try:
        from api.positional_intelligence import get_positional_intelligence
        pi = get_positional_intelligence(min_consec=0)
        stealth = pi.get("stealth_buildup") or []'''
    ),
    (
        '''        return {
            "featured": {
                "symbol": best["symbol"],
                "cmp": best["cmp"],
                "price_chg_pct": best.get("price_chg", 0),
                "tier": best.get("tier"),
                "tier_label": best.get("tier_label"),
                "fut_oi_chg_pct": best.get("today_oi_chg", 0),
                "oi_history": best.get("oi_history") or [],
                "sessions": len(best.get("oi_history") or []),
            }
        }
    except Exception as e:
        return {"featured": None, "error": str(e)}''',
        '''        result = {
            "featured": {
                "symbol": best["symbol"],
                "cmp": best["cmp"],
                "price_chg_pct": best.get("price_chg", 0),
                "tier": best.get("tier"),
                "tier_label": best.get("tier_label"),
                "fut_oi_chg_pct": best.get("today_oi_chg", 0),
                "oi_history": best.get("oi_history") or [],
                "sessions": len(best.get("oi_history") or []),
            }
        }
        public_stealth_featured._cache = result
        public_stealth_featured._cache_time = time.time()
        return result
    except Exception as e:
        return {"featured": None, "error": str(e)}'''
    ),
]

missing = []
for old, new in edits:
    if old not in src:
        missing.append(old[:80])
    else:
        src = src.replace(old, new, 1)

if missing:
    print("MISSING ANCHORS:", missing)
else:
    with open(os.path.expanduser("~/mnt/optionspulse/main_new.py"), "w") as f:
        f.write(src)
    print("OK, wrote main_new.py, len", len(src))
