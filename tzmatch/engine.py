"""Wall-clock -> UTC integer-second matching engine.

For every input clock face the engine enumerates every integer UTC second
``u`` whose active UTC offset ``off`` satisfies ``u + off == wall``; the
result is deduplicated and returned in strictly ascending order.  There is
no assumption that at most two candidates exist.
"""

import bisect
import re

from . import posix as posix_mod
from . import tzif as tzif_mod
from .cal import MIN_YEAR, MAX_YEAR, civil_to_epoch, days_in_month
from .errors import FileFormatError, InputError
from .posix import RULE_YEAR_MIN, RULE_YEAR_MAX, rule_event_epoch

_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$")


def parse_wall_time(value, index):
    if not isinstance(value, str):
        raise InputError("time elements must be JSON strings", path=[index])
    m = _TIME_RE.match(value)
    if not m:
        raise InputError(
            "time must match YYYY-MM-DDTHH:MM:SS exactly", path=[index]
        )
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise InputError("year must be in 1900..9998", path=[index])
    if not 1 <= month <= 12 or day < 1 or day > days_in_month(year, month):
        raise InputError("invalid calendar date", path=[index])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise InputError("time of day must have hour 0..23 and minute/second 0..59", path=[index])
    return civil_to_epoch(year, month, day, hour, minute, second)


class Engine:
    def __init__(self, tzdata):
        self.d = tzdata
        self.posix = posix_mod.parse(
            tzdata.footer, tzdata.version, tzdata.footer_start
        )
        self._check_footer_consistency()
        self._build_rule_events()

    # -- footer vs. 64-bit block consistency -------------------------------
    def _check_footer_consistency(self):
        d = self.d
        fstart = d.footer_start
        if self.posix is None:
            # Empty footer: nothing beyond the last transition must be checked.
            return
        if self.posix.fixed:
            if d.transitions:
                t = d.types[d.type_index[-1]]
                if t.utoff != self.posix.std_offset:
                    raise FileFormatError(
                        "fixed-offset footer disagrees with the last transition type",
                        byte=fstart,
                    )
            else:
                t = d.types[0]
                if t.utoff != self.posix.std_offset:
                    raise FileFormatError(
                        "fixed-offset footer disagrees with type 0",
                        byte=fstart,
                    )
            return

        # Seasonal footer.
        def matches(t, utoff, isdst, name):
            if t.utoff != utoff or t.isdst != isdst:
                return False
            if t.abbr != name:
                return False
            return True

        if d.transitions:
            t = d.types[d.type_index[-1]]
            if not matches(t, self.posix.std_offset, 0, self.posix.std_name) and not matches(
                t, self.posix.dst_offset, 1, self.posix.dst_name
            ):
                raise FileFormatError(
                    "seasonal footer disagrees with the last transition type",
                    byte=fstart,
                )
        else:
            t = d.types[0]
            if not matches(t, self.posix.std_offset, 0, self.posix.std_name):
                raise FileFormatError(
                    "seasonal footer must agree with type 0 (standard side)",
                    byte=fstart,
                )

    def _build_rule_events(self):
        self.events = []
        if self.posix is None or self.posix.fixed:
            return
        p = self.posix
        for year in range(RULE_YEAR_MIN, RULE_YEAR_MAX + 1):
            self.events.append(
                (rule_event_epoch(year, p.start, p.std_offset), True)
            )
            self.events.append(
                (rule_event_epoch(year, p.end, p.dst_offset), False)
            )
        self.events.sort(key=lambda e: e[0])
        self.event_epochs = [e[0] for e in self.events]

    # -- active offset at a UTC instant -------------------------------------
    def _block_offset(self, u):
        d = self.d
        if not d.transitions:
            return d.types[0].utoff
        k = bisect.bisect_right(d.transitions, u) - 1
        if k < 0:
            return d.types[0].utoff
        return d.types[d.type_index[k]].utoff

    def _footer_offset(self, u):
        p = self.posix
        if p.fixed:
            return p.std_offset
        k = bisect.bisect_right(self.event_epochs, u) - 1
        if k < 0:
            return p.std_offset
        return p.dst_offset if self.events[k][1] else p.std_offset

    def offset_at(self, u):
        d = self.d
        if self.posix is None:
            return self._block_offset(u)
        if not d.transitions:
            return self._footer_offset(u)
        last = d.transitions[-1]
        if u < last:
            return self._block_offset(u)
        return self._footer_offset(u)

    # -- candidates ----------------------------------------------------------
    def candidates(self, wall):
        d = self.d
        if self.posix is None:
            # Only the recorded transitions partition the line; every block
            # type contributes at most one candidate.
            offsets = {t.utoff for t in d.types}
            return sorted({wall - off for off in offsets if self.offset_at(wall - off) == off})

        p = self.posix
        if p.fixed:
            offsets = {t.utoff for t in d.types} | {p.std_offset}
            return sorted({wall - off for off in offsets if self.offset_at(wall - off) == off})

        offsets = {t.utoff for t in d.types} | {p.std_offset, p.dst_offset}
        return sorted({wall - off for off in offsets if self.offset_at(wall - off) == off})


def run(tzif_bytes, times):
    """Parse + validate both inputs and return the result array.

    Any invalid input raises :class:`FileFormatError` or :class:`InputError`;
    callers must then keep any previous output untouched.
    """
    tzdata = tzif_mod.parse(tzif_bytes)
    engine = Engine(tzdata)

    if not isinstance(times, list):
        raise InputError("the time input must be a JSON array", path=[])
    walls = [parse_wall_time(v, i) for i, v in enumerate(times)]
    if not walls:
        return []
    return [engine.candidates(w) for w in walls]
