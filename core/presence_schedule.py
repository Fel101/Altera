"""Pure UTC/IANA scheduling rules for companion presence.

Quiet intervals include their start and exclude their end. Equal endpoints mean
all-day quiet while enabled. Ritual wall times use the first repeated occurrence;
nonexistent times advance to the next valid local minute, including skipped days.
"""
from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = UTC


class ScheduleError(ValueError):
    status = 400


def timezone(name):
    """Resolve an IANA name, never substituting the computer's local timezone."""
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ScheduleError('Choose a valid IANA timezone, such as America/New_York or UTC.')
    if name == 'UTC':
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleError('The IANA timezone is unknown or its timezone data is unavailable. Install tzdata before enabling this schedule.') from exc


def _minute(value):
    if type(value) is not int or not 0 <= value < 1440:
        raise ScheduleError('A schedule time must be an integer minute from 0 through 1439.')
    return value


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError('A schedule instant must include its UTC offset.')
    return value.astimezone(UTC)


def in_quiet(now_utc, tz_name, start_minute, end_minute, enabled=True):
    """Whether an aware instant is in the configured local quiet interval."""
    if type(enabled) is not bool:
        raise ScheduleError('Quiet-hours enabled must be true or false.')
    start, end = _minute(start_minute), _minute(end_minute)
    local = _utc(now_utc).astimezone(timezone(tz_name))
    if not enabled:
        return False
    if start == end:
        return True
    minute = local.hour * 60 + local.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def local_day(now_utc, tz_name):
    """The local calendar date used for daily caps and deterministic occurrences."""
    return _utc(now_utc).astimezone(timezone(tz_name)).date()


def occurrence(day, tz_name, minute):
    """One daily ritual occurrence as an aware UTC datetime.

    A repeated time uses fold=0 only. A missing wall minute moves forward to the
    first real minute, rather than guessing a UTC offset or running twice.
    """
    if type(day) is not date:
        raise ScheduleError('A ritual occurrence requires a calendar date.')
    minute = _minute(minute)
    zone = timezone(tz_name)
    wall = datetime.combine(day, time(minute // 60, minute % 60))
    for _ in range(48 * 60 + 1):
        candidate = wall.replace(tzinfo=zone, fold=0)
        utc = candidate.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == wall:
            return utc
        wall += timedelta(minutes=1)
    raise ScheduleError('This timezone has no valid ritual time within the next two days.')


def next_occurrence(after_utc, tz_name, minute, weekday=None):
    """Next strictly later daily/weekly occurrence; weekday uses Monday=0."""
    after = _utc(after_utc)
    zone = timezone(tz_name)
    minute = _minute(minute)
    if weekday is not None and (type(weekday) is not int or not 0 <= weekday <= 6):
        raise ScheduleError('A ritual weekday must be an integer from 0 (Monday) through 6 (Sunday).')
    day = after.astimezone(zone).date()
    for offset in range(15):
        target = day + timedelta(days=offset)
        if weekday is not None and target.weekday() != weekday:
            continue
        result = occurrence(target, tz_name, minute)
        if result > after:
            return result
    raise ScheduleError('No ritual occurrence could be found in the next two weeks.')
