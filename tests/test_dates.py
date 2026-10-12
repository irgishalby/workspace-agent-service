# tests/test_dates.py
from datetime import date
from core.dates import format_due, format_due_full


def test_all():
    now = date(2026, 10, 12)  # Monday
    cases = [
        # (due, expected_label)
        (date(2026, 10, 12), "Today"),
        (date(2026, 10, 13), "Tomorrow"),
        # 2-6 days: weekday name
        (date(2026, 10, 14), "Wed"),      # +2
        (date(2026, 10, 18), "Sun"),      # +6
        # 7 days boundary -> "in 1 week"
        (date(2026, 10, 19), "in 1 week"),  # +7
        (date(2026, 10, 25), "in 1 week"),  # +13 -> 13//7=1
        (date(2026, 10, 26), "in 2 weeks"), # +14
        (date(2026, 11, 10), "in 4 weeks"), # +29
        # 30+ days, same year
        (date(2026, 11, 11), "11 Nov"),     # +30
        (date(2026, 12, 25), "25 Dec"),
        # 30+ days, next year
        (date(2027, 1, 3), "3 Jan 2027"),
        (date(2027, 6, 15), "15 Jun 2027"),
        # Overdue
        (date(2026, 10, 11), "1 day overdue"),
        (date(2026, 10, 10), "2 days overdue"),
        (date(2026, 9, 12), "30 days overdue"),
    ]
    failed = []
    for due, expected in cases:
        got = format_due(due, now)
        if got != expected:
            failed.append(f"  due={due}, expected={expected!r}, got={got!r}")

    # format_due_full with time
    assert format_due_full(date(2026, 10, 12), "14:00", now) == "Today 14:00"
    assert format_due_full(date(2026, 10, 14), "09:00", now) == "Wed 09:00"
    # no time
    assert format_due_full(date(2026, 10, 12), None, now) == "Today"
    assert format_due_full(date(2026, 10, 12), "23:59", now, has_time=False) == "Today"
    # None due
    assert format_due(None, now) == "no date"
    # String input
    assert format_due("2026-10-12", now) == "Today"

    # Month-end boundary: Oct 31 from Oct 12 = +19 days -> in 2 weeks
    assert format_due(date(2026, 10, 31), now) == "in 2 weeks"
    # Year-end: Dec 31 from Oct 12 = +80 days, same year
    assert format_due(date(2026, 12, 31), now) == "31 Dec"

    if failed:
        print("FAILED:")
        print("\n".join(failed))
    else:
        print("ALL PASSED")
    return len(failed) == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if test_all() else 1)
