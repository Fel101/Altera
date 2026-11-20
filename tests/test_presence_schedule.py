"""IANA scheduling at wall-clock discontinuities and quiet-hour boundaries."""
from datetime import date, datetime, UTC

import pytest

from core.presence_schedule import ScheduleError, in_quiet, local_day, next_occurrence, occurrence

UTC = UTC


@pytest.mark.parametrize('hour,minute,expected', [(21, 59, False), (22, 0, True), (23, 59, True),
                                                (0, 0, True), (7, 59, True), (8, 0, False)])
def test_cross_midnight_quiet_includes_start_excludes_end(hour, minute, expected):
    assert in_quiet(datetime(2026, 1, 1, hour, minute, tzinfo=UTC), 'UTC', 1320, 480) == expected


def test_equal_endpoints_are_all_day_quiet_but_off_is_off():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert in_quiet(now, 'America/New_York', 300, 300)
    assert not in_quiet(now, 'America/New_York', 300, 300, False)


def test_missing_spring_time_moves_to_first_valid_minute():
    assert occurrence(date(2026, 3, 8), 'America/New_York', 150) == datetime(2026, 3, 8, 7, tzinfo=UTC)


def test_repeated_fall_time_uses_first_occurrence_and_does_not_run_twice():
    first = occurrence(date(2026, 11, 1), 'America/New_York', 90)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence(first, 'America/New_York', 90) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
    assert next_occurrence(datetime(2026, 11, 1, 6, tzinfo=UTC), 'America/New_York', 90).date() == date(2026, 11, 2)


def test_local_daily_cap_date_and_weekly_occurrence_use_saved_zone():
    now = datetime(2026, 10, 27, 1, tzinfo=UTC)
    assert local_day(now, 'America/New_York') == date(2026, 10, 26)
    assert next_occurrence(now, 'America/New_York', 9 * 60, weekday=0) == datetime(2026, 11, 2, 14, tzinfo=UTC)


@pytest.mark.parametrize('zone,minute', [('Invalid/Timezone', 30), ('UTC', True), ('UTC', -1), ('UTC', 1440)])
def test_invalid_zone_or_wall_time_is_rejected(zone, minute):
    with pytest.raises(ScheduleError): occurrence(date(2026, 10, 27), zone, minute)


def test_naive_clock_is_never_interpreted_as_the_machine_timezone():
    with pytest.raises(ScheduleError): next_occurrence(datetime(2026, 10, 27), 'UTC', 540)
