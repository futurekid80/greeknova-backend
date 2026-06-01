import logging
from datetime import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

MCX_OPEN_HOUR  = 9
MCX_OPEN_MIN   = 0
MCX_CLOSE_HOUR = 23
MCX_CLOSE_MIN  = 30


def is_mcx_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_minutes  = MCX_OPEN_HOUR  * 60 + MCX_OPEN_MIN
    close_minutes = MCX_CLOSE_HOUR * 60 + MCX_CLOSE_MIN
    now_minutes   = now.hour * 60 + now.minute
    return open_minutes <= now_minutes <= close_minutes


def run_seed_job(supabase, session_open_oi, session_peak_oi, session_open_price_dict=None):
    from services.kite_auth import get_kite_client
    from commoditynova.mcx_instruments import seed_mcx_instruments
    try:
        logger.info("MCX morning seed starting...")
        kite = get_kite_client()
        seed_mcx_instruments(kite, supabase)
        session_open_oi.clear()
        session_peak_oi.clear()
        session_open_price_dict.clear()
        logger.info("MCX session state reset for new trading day")
    except Exception as e:
        logger.error(f"MCX seed job failed: {e}")


def run_scan_job(supabase, candles_cache, prev_oi, session_open_oi, session_peak_oi, prev_strike_oi=None):
    if not is_mcx_market_open():
        return

    from services.kite_auth import get_kite_client
    from commoditynova.mcx_instruments import get_cached_instruments
    from commoditynova.mcx_historical import fetch_all_candles
    from commoditynova.mcx_ignition_scanner import run_ignition_scan

    try:
        kite = get_kite_client()
        instruments = get_cached_instruments(supabase)
        if not instruments:
            logger.warning("MCX scan skipped — no instruments cached yet")
            return
        fresh_candles = fetch_all_candles(instruments, kite)
        candles_cache.update(fresh_candles)
        run_ignition_scan(kite, supabase, candles_cache, prev_oi,
                          session_open_oi, session_peak_oi, prev_strike_oi)
    except Exception as e:
        logger.error(f"MCX scan job failed: {e}")


def start_mcx_scheduler(kite, supabase):
    candles_cache:          dict = {}
    prev_oi:                dict = {}
    session_open_oi:        dict = {}
    session_peak_oi:        dict = {}
    prev_strike_oi:         dict = {}
    session_open_price_dict: dict = {}  # persisted session open price per commodity

    scheduler = BackgroundScheduler(timezone=IST)

    scheduler.add_job(
        func=run_seed_job,
        trigger=CronTrigger(
            hour=MCX_OPEN_HOUR, minute=MCX_OPEN_MIN,
            day_of_week="mon-fri", timezone=IST,
        ),
        kwargs={"supabase": supabase, "session_open_oi": session_open_oi,
                "session_peak_oi": session_peak_oi,
                "session_open_price_dict": session_open_price_dict},
        id="mcx_morning_seed", name="MCX morning instrument seed",
        replace_existing=True, misfire_grace_time=120,
    )

    scheduler.add_job(
        func=run_scan_job,
        trigger=IntervalTrigger(minutes=5, timezone=IST),
        kwargs={"supabase": supabase, "candles_cache": candles_cache,
                "prev_oi": prev_oi, "session_open_oi": session_open_oi,
                "session_peak_oi": session_peak_oi,
                "prev_strike_oi": prev_strike_oi,
                "session_open_price_dict": session_open_price_dict},
        id="mcx_ignition_scan", name="MCX trend ignition 5-min scan",
        replace_existing=True, misfire_grace_time=60,
    )

    scheduler.start()
    logger.info("MCX scheduler started — seed at 9:00 AM, scan every 5 min")
    return scheduler
