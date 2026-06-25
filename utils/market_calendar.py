"""
market_calendar.py - NSE Holiday Calendar 2026
Hardcoded from official NSE circular. Update annually.
"""
from datetime import date, timedelta

NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26): "Republic Day",
    date(2026, 2, 19): "Chhatrapati Shivaji Maharaj Jayanti",
    date(2026, 3, 25): "Holi",
    date(2026, 4, 2):  "Shri Ram Navami",
    date(2026, 4, 3):  "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1):  "Maharashtra Day",
    date(2026, 5, 27): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 4): "Diwali Laxmi Pujan",
    date(2026, 11, 5): "Diwali Balipratipada",
    date(2026, 11, 25): "Gurunanak Jayanti",
    date(2026, 12, 25): "Christmas",
}

def is_trading_day(d: date = None) -> bool:
    if d is None:
        d = date.today()
    if d.weekday() >= 5:
        return False
    return d not in NSE_HOLIDAYS_2026

def get_next_trading_day(from_date: date = None) -> date:
    if from_date is None:
        from_date = date.today()
    d = from_date + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d

def get_prev_trading_day(from_date: date = None) -> date:
    if from_date is None:
        from_date = date.today()
    d = from_date - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d

def get_market_status() -> dict:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    
    today_holiday = NSE_HOLIDAYS_2026.get(today)
    tomorrow_holiday = NSE_HOLIDAYS_2026.get(tomorrow)
    
    is_open_today = is_trading_day(today)
    next_trading = get_next_trading_day(today)
    prev_trading = get_prev_trading_day(today)
    
    # Check for long weekend
    days_to_next = (next_trading - today).days
    long_weekend = days_to_next >= 3
    
    # Upcoming holidays (next 30 days)
    upcoming = []
    for d, name in sorted(NSE_HOLIDAYS_2026.items()):
        if today < d <= today + timedelta(days=30):
            upcoming.append({
                "date": d.isoformat(),
                "name": name,
                "days_away": (d - today).days,
                "day_of_week": d.strftime("%A"),
            })
    
    return {
        "today": today.isoformat(),
        "today_holiday": today_holiday,
        "is_trading_day": is_open_today,
        "tomorrow": tomorrow.isoformat(),
        "tomorrow_holiday": tomorrow_holiday,
        "next_trading_day": next_trading.isoformat(),
        "prev_trading_day": prev_trading.isoformat(),
        "long_weekend": long_weekend,
        "days_to_next_trading": days_to_next,
        "upcoming_holidays": upcoming,
    }
