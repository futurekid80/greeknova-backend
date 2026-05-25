import logging
from datetime import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# MCX market hours IST
MCX_OPEN_HOUR   = 9
MCX_OPEN_MIN    = 0
MCX_CLOSE_HOUR  = 23
MCX_CLOSE_MIN   = 30


def is_mcx_market_open() -> bool:
    """
    Returns True if current IST time is within MCX trading hours.
    MCX trades 9:00 AM – 11:30 PM IST, Monday to Friday.
    """
    now = datetime.now(IST)

    # Skip weekends
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False

    open_minutes  = MCX_OPEN_HOUR  * 60 + MCX_OPEN_MIN
    close_minutes = MCX_CLOSE_HOUR * 60 + MCX_CLOSE_MIN
    now_minutes   = now.hour * 60 + now.minute

    return open_minutes <= now_minutes <= close_minutes


def run_seed_job(supabase):
    from services.kite_auth import get_kite_client
    from commoditynova.mcx_instruments import seed_mcx_instruments
    try:
        kite = get_kite_client()  # fresh client
        logger.info("MCX morning seed starting...")
        seed_mcx_instruments(kite, supabase)
    except Exception as e:
        logger.error(f"MCX seed job failed: {e}")


def run_scan_job(supabase, candles_cache: dict, prev_oi: dict):
    if not is_mcx_market_open():
        return

    from services.kite_auth import get_kite_client
    from commoditynova.mcx_instruments import get_cached_instruments
    from commoditynova.mcx_historical import fetch_all_candles
    from commoditynova.mcx_ignition_scanner import run_ignition_scan

    try:
        kite = get_kite_client()  # fresh client every cycle
        instruments = get_cached_instruments(supabase)
        if not instruments:
            logger.warning("MCX scan skipped — no instruments cached yet")
            return
        fresh_candles = fetch_all_candles(instruments, kite)
        candles_cache.update(fresh_candles)
        run_ignition_scan(kite, supabase, candles_cache, prev_oi)
    except Exception as e:
        logger.error(f"MCX scan job failed: {e}")


def start_mcx_scheduler(kite, supabase) -> BackgroundScheduler:
    """
    Creates and starts the MCX APScheduler.
    Call this from main.py on app startup.
    Returns the scheduler instance so it can be shut down cleanly.
    """
    # Shared state passed into jobs — persists across scan cycles
    candles_cache: dict = {}
    prev_oi: dict       = {}

    scheduler = BackgroundScheduler(timezone=IST)

    # Job 1 — Morning seed at 9:00 AM IST sharp, Mon–Fri
    scheduler.add_job(
    func=run_seed_job,
    ...
    kwargs={"supabase": supabase},  # remove kite
    ...
)

scheduler.add_job(
    func=run_scan_job,
    ...
    kwargs={"supabase": supabase, "candles_cache": candles_cache, "prev_oi": prev_oi},  # remove kite
    ...
)

    # Job 2 — 5-minute scan, every day
    # Market hours guard inside run_scan_job handles the off-hours silencing
    scheduler.add_job(
        func=run_scan_job,
        trigger=IntervalTrigger(minutes=5, timezone=IST),
        kwargs={
            "kite": kite,
            "supabase": supabase,
            "candles_cache": candles_cache,
            "prev_oi": prev_oi,
        },
        id="mcx_ignition_scan",
        name="MCX trend ignition 5-min scan",
        replace_existing=True,
        misfire_grace_time=60,
    )

    scheduler.start()
    logger.info("MCX scheduler started — seed at 9:00 AM, scan every 5 min")
    return scheduler
