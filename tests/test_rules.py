"""Pure unit tests for rules R1-R5 and rounding. No database needed."""
from datetime import datetime, timezone

import pytest

from tests.conftest import m

UTC = timezone.utc


def ist(y, mo, d, h, mi, s=0):
    """An IST wall-clock time as an aware UTC datetime."""
    return datetime(y, mo, d, h, mi, s, tzinfo=m.IST).astimezone(UTC)


# ---- R2 late (shift 09:30, grace 10:00 strict) ----
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 30, 0, 0), (9, 0, 0, 0), (9, 39, 59, 0),
])
def test_late(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30") == expected


# ---- R1 attendance date ----
def test_date_normal_shift_uses_ist_not_utc():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th -> must be the 7th
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"

def test_date_overnight_after_midnight_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 7, 1, 0), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"

def test_late_for_overnight_shift_after_midnight():
    # shift starts 22:00 on the 6th; punch-in 22:30 is 30 late, 01:00 next day is 180 late
    assert m.compute_late_minutes(ist(2026, 7, 6, 22, 30), "2026-07-06", "22:00") == 30
    assert m.compute_late_minutes(ist(2026, 7, 7, 1, 0), "2026-07-06", "22:00") == 180


# ---- R3 overtime ----
def test_overtime():
    f = lambda h, mi, s=0: m.compute_overtime(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30")
    assert f(18, 59, 59) == 0      # 29 min -> below minimum
    assert f(19, 0) == 30
    assert f(19, 10) == 40
    assert f(17, 0) == 0           # left early
    assert f(19, 0, 59) == 30      # floor

def test_overtime_overnight_shift_ends_next_day():
    out = ist(2026, 7, 7, 6, 40)
    assert m.compute_overtime(out, "2026-07-06", "22:00", "06:00") == 40


# ---- R4 / R5 ----
def test_work_hours_half_up():
    a = ist(2026, 7, 6, 9, 0)
    # 8h 7m 30s = 29250 s = 8.125 h -> half-up 8.13 (banker's/float round would give 8.12)
    b = ist(2026, 7, 6, 17, 7, 30)
    assert m.compute_work_hours(a, b) == 8.13

def test_work_hours_sample():
    assert m.compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12

def test_half_day_uses_rounded_value():
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.5) is False
    assert m.compute_half_day(None) is False
    a = ist(2026, 7, 6, 9, 0)
    # 4h29m50s = 4.49722 h -> rounds to 4.50 -> NOT a half day
    wh = m.compute_work_hours(a, ist(2026, 7, 6, 13, 29, 50))
    assert wh == 4.5 and m.compute_half_day(wh) is False


def test_half_up_helper():
    assert m.half_up(0.03125, 4) == 0.0313
    assert m.half_up(2.675, 2) == 2.68      # float round() gives 2.67
    assert m.half_up(8.124999999999999, 2) == 8.13  # float noise is cleaned, so a real tie rounds up


def test_ms_conversion_truncates_to_seconds():
    dt = m.ms_to_dt(1783312500900)
    assert dt.microsecond == 0 and m.dt_to_ms(dt) == 1783312500000


def test_month_and_weekdays():
    assert m.month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    from datetime import date
    assert m.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15
