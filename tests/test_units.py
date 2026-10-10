from datetime import date, datetime

import pytest

from scheduler.units import (
    WEEKDAYS, clock_range, format_hours, hours_to_slots, next_slot, parse_date, parse_due_time, parse_time, parse_weekday,
    slot_to_time, slots_to_hours, time_to_minutes, time_to_slot, weekday_name,
)


def test_time_to_slot():
    assert time_to_slot("00:00") == 0
    assert time_to_slot("09:30") == 38
    assert time_to_slot("23:45") == 95
    assert time_to_slot("09:10") == 36  # rounded down to its slot


def test_slot_to_time_roundtrip():
    assert slot_to_time(38) == "09:30"


def test_time_to_minutes():
    assert time_to_minutes("07:10") == 430


def test_parse_time_pads_and_only_allows_24_00_as_an_end():
    assert parse_time("9:30") == "09:30"
    assert parse_time("24:00", end=True) == "24:00"
    with pytest.raises(ValueError, match="not a real time"):
        parse_time("24:00")
    with pytest.raises(ValueError, match="not a time like"):
        parse_time("9.30")


def test_parse_due_time_rounds_down_to_its_slot():
    assert parse_due_time("09:10") == "09:00"


def test_parse_date_normalises_and_rejects_impossible_dates():
    assert parse_date("20261005") == "2026-10-05"
    with pytest.raises(ValueError, match="not a real date"):
        parse_date("2026-02-30")


def test_weekdays_follow_date_weekday_order():
    assert weekday_name(date(2026, 9, 28)) == "Mon"  # a Monday
    assert weekday_name(date(2026, 10, 3)) == "Sat"
    assert WEEKDAYS[date(2026, 10, 4).weekday()] == "Sun"


def test_parse_weekday():
    assert parse_weekday(" monday") == "Mon"
    assert parse_weekday("Thu") == "Thu"
    with pytest.raises(ValueError, match="day must be one of"):
        parse_weekday("funday")


def test_clock_range_wraps_past_midnight():
    assert clock_range(36, 42) == "09:00-10:30"
    assert clock_range(88, 104) == "22:00-02:00"


def test_hours():
    assert slots_to_hours(6) == 1.5
    assert [format_hours(s) for s in (7, 4, 2, 0)] == ["1h 45m", "1h", "30m", "0m"]


def test_next_slot_is_the_first_slot_not_yet_started():
    assert next_slot(datetime(2026, 10, 5, 9, 0)) == 36
    assert next_slot(datetime(2026, 10, 5, 9, 1)) == 37
    assert next_slot(datetime(2026, 10, 5, 23, 50)) == 96  # rolls into tomorrow


@pytest.mark.parametrize("hours, slots", [(1, 4), (1.5, 6), (0.25, 1), (0.375, 2), (2.1, 8)])
def test_hours_to_slots(hours, slots):
    assert hours_to_slots(hours) == slots


@pytest.mark.parametrize("hours", [0, 0.1, -1])
def test_hours_to_slots_rejects_under_one_slot(hours):
    with pytest.raises(ValueError):
        hours_to_slots(hours)
