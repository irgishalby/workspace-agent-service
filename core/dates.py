# core/dates.py  -  single date formatter for every human-facing date in the bot
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jakarta")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def format_due(due_date, now=None, has_time=True):
    """Format a due date/deadline for display.

    due_date: 'YYYY-MM-DD' string or date object.
    now:      datetime or date; defaults to now in WIB.
    has_time: False suppresses any time part (caller should not append time).
    Returns the date label only (no time). Caller appends time when has_time is True.
    """
    if not due_date:
        return "no date"
    if isinstance(due_date, str):
        due_date = date.fromisoformat(due_date[:10])
    if now is None:
        now = datetime.now(TZ).date()
    elif isinstance(now, datetime):
        now = now.date()

    delta = (due_date - now).days

    if delta < 0:
        n = -delta
        return f"{n} day{'s' if n != 1 else ''} overdue"
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta <= 6:
        return _DAYS[due_date.weekday()]
    if delta <= 29:
        weeks = delta // 7
        return f"in {weeks} week{'s' if weeks != 1 else ''}"
    if due_date.year == now.year:
        return f"{due_date.day} {_MONTHS[due_date.month - 1]}"
    return f"{due_date.day} {_MONTHS[due_date.month - 1]} {due_date.year}"


def format_time(hhmm):
    """Pass-through for HH:MM. Returns '' if None."""
    return hhmm or ""


def format_due_full(due_date, time=None, now=None, has_time=True):
    """Convenience: 'Today 14:00' or 'Fri' (no time)."""
    label = format_due(due_date, now, has_time)
    if has_time and time:
        return f"{label} {time}"
    return label
