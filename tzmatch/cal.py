"""Proleptic Gregorian calendar math, independent of any timezone database.

All conversions count UTC seconds from 1970-01-01T00:00:00Z on a uniform
86400-second day: leap seconds are not counted, exactly as required.
The civil-date algorithms are Howard Hinnant's public-domain formulas.
"""

MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
MIN_YEAR = 1900
MAX_YEAR = 9998


def is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year, month):
    if month == 2 and is_leap_year(year):
        return 29
    return MONTH_DAYS[month - 1]


def days_from_civil(year, month, day):
    """Days since 1970-01-01 (negative for earlier dates)."""
    y = year - (1 if month <= 2 else 0)
    era = y // 400 if y >= 0 else (y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(days):
    """Inverse of :func:`days_from_civil`."""
    z = days + 719468
    era = z // 146097 if z >= 0 else (z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    if month <= 2:
        year += 1
    return year, month, day


def weekday(year, month, day):
    """Day of week with Sunday == 0."""
    return (days_from_civil(year, month, day) + 4) % 7


def mwd_day(year, month, week, dow):
    """Day of month for an POSIX Mm.w.d rule (week 1..5, dow 0=Sun..6=Sat).

    ``week == 5`` means the last occurrence of ``dow`` in the month.
    """
    first_weekday = weekday(year, month, 1)
    first_occurrence = 1 + (dow - first_weekday) % 7
    if week <= 4:
        result = first_occurrence + (week - 1) * 7
    else:
        result = first_occurrence + 4 * 7
        if result > days_in_month(year, month):
            result -= 7
    return result


def civil_to_epoch(year, month, day, hour, minute, second):
    """Uniform UTC seconds since the epoch for a validated civil time."""
    return days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second


def epoch_to_civil(epoch):
    """Inverse of :func:`civil_to_epoch` for non-negative or negative epochs."""
    days, rem = divmod(epoch, 86400)
    year, month, day = civil_from_days(days)
    hour, rem = divmod(rem, 3600)
    minute, second = divmod(rem, 60)
    return year, month, day, hour, minute, second
