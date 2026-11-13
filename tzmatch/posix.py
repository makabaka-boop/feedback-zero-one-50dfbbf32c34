"""Restricted POSIX TZ footer parser (RFC 8536 section 3.3.1, strict subset).

Accepted footers are limited to:

* ``std offset``                          -- fixed-offset type
* ``std offset dst offset , start , end`` -- seasonal type, where both
  transitions use explicit ``Mm.w.d[/time]`` rules and explicit times.

Names must be 3..6 ASCII letters (angle-bracket names are rejected).  Rule
times are unsigned 0..24 for v2 files and signed -167..167 for v3 files.
Transition instants must be distinct and alternate strictly between
standard->DST and DST->standard, including southern-hemisphere cross-year
orderings.
"""

from dataclasses import dataclass

from .cal import civil_to_epoch, mwd_day
from .errors import FileFormatError

# Years spanned by every possible query (1900..9998), plus one on each side
# so cross-year southern-hemisphere rule ordering can be fully checked.
RULE_YEAR_MIN = 1899
RULE_YEAR_MAX = 9999


@dataclass
class Rule:
    month: int
    week: int
    day_of_week: int
    hour: int
    minute: int
    second: int


@dataclass
class PosixTZ:
    std_name: str
    std_offset: int            # seconds east of UTC (POSIX sign already flipped)
    dst_name: str = ""
    dst_offset: int = 0
    start: Rule = None         # standard -> DST rule (seasonal only)
    end: Rule = None           # DST -> standard rule (seasonal only)

    @property
    def fixed(self):
        return self.start is None


class _Cursor:
    def __init__(self, text, base):
        self.text = text
        self.i = 0
        self.base = base       # absolute file byte of footer[0]

    def err(self, msg):
        raise FileFormatError(msg, byte=self.base + self.i)

    def eof(self):
        return self.i >= len(self.text)

    def peek(self):
        return self.text[self.i] if not self.eof() else ""

    def expect(self, ch):
        if self.eof() or self.text[self.i] != ch:
            self.err("expected %r in POSIX TZ string" % ch)
        self.i += 1

    def consume_name(self):
        start = self.i
        while not self.eof() and self.text[self.i].isascii() and self.text[self.i].isalpha():
            self.i += 1
        length = self.i - start
        if not 3 <= length <= 6:
            self.err("zone name must be 3 to 6 ASCII letters")
        return self.text[start:self.i]

    def consume_digits(self, max_digits=2):
        start = self.i
        while not self.eof() and self.text[self.i].isdigit():
            self.i += 1
        count = self.i - start
        if count == 0 or count > max_digits:
            self.err("expected 1..%d decimal digits" % max_digits)
        return int(self.text[start:self.i])

    def _hhmmss(self, allow_sign, max_hour):
        sign = 1
        if self.peek() in ("+", "-"):
            if not allow_sign:
                self.err("rule time may not carry a sign in a v2 file")
            if self.text[self.i] == "-":
                sign = -1
            self.i += 1
        hour_digits = 3 if max_hour > 99 else 2
        hour = self.consume_digits(hour_digits)
        minute = second = 0
        if self.peek() == ":":
            self.i += 1
            minute = self.consume_digits(2)
            if not 0 <= minute <= 59:
                self.err("minutes must be in 0..59")
            if self.peek() == ":":
                self.i += 1
                second = self.consume_digits(2)
                if not 0 <= second <= 59:
                    self.err("seconds must be in 0..59")
        if not -max_hour <= hour <= max_hour:
            self.err("hour field out of range (-%d..%d)" % (max_hour, max_hour))
        if abs(hour) == max_hour and (minute or second):
            self.err("a %d-hour value may not carry minutes or seconds" % max_hour)
        return sign * (hour * 3600 + minute * 60 + second)

    def consume_offset(self):
        # POSIX offsets are signed, magnitude at most 24:00:00.
        value = self._hhmmss(allow_sign=True, max_hour=24)
        # POSIX sign: local standard time = UTC + value-with-sign flipped.
        return -value

    def consume_rule_time(self, version):
        # The grammar requires an explicit "/time" on every rule.
        self.expect("/")
        if version == ord("2"):
            return self._hhmmss(allow_sign=False, max_hour=24)
        return self._hhmmss(allow_sign=True, max_hour=167)

    def consume_rule(self, version):
        self.expect("M")
        month = self.consume_digits(2)
        if not 1 <= month <= 12:
            self.err("rule month must be in 1..12")
        self.expect(".")
        week = self.consume_digits(1)
        if not 1 <= week <= 5:
            self.err("rule week must be in 1..5")
        self.expect(".")
        dow = self.consume_digits(1)
        if not 0 <= dow <= 6:
            self.err("rule weekday must be in 0..6 (0 = Sunday)")
        seconds = self.consume_rule_time(version)
        hour, rem = divmod(abs(seconds), 3600)
        minute, second = divmod(rem, 60)
        if seconds < 0:
            hour = -hour
        return Rule(month, week, dow, hour, minute, second)


def rule_event_epoch(year, rule, utoff):
    day = mwd_day(year, rule.month, rule.week, rule.day_of_week)
    local_epoch = civil_to_epoch(year, rule.month, day, rule.hour, rule.minute, rule.second)
    return local_epoch - utoff


def check_alternation(tz):
    """All rule events over the full query range must alternate and differ."""
    events = []
    for year in range(RULE_YEAR_MIN, RULE_YEAR_MAX + 1):
        # start: standard wall clock; end: DST wall clock.
        events.append((rule_event_epoch(year, tz.start, tz.std_offset), True))
        events.append((rule_event_epoch(year, tz.end, tz.dst_offset), False))
    events.sort(key=lambda e: e[0])
    previous_epoch = None
    previous_is_start = None
    for epoch, is_start in events:
        if previous_epoch is not None:
            if epoch == previous_epoch:
                raise FileFormatError(
                    "standard/DST rules must not fire at the same instant"
                )
            if is_start == previous_is_start:
                raise FileFormatError(
                    "rule transitions must strictly alternate between start and end"
                )
        previous_epoch = epoch
        previous_is_start = is_start


def parse(footer, version, footer_start):
    if not footer:
        return None
    text = footer.decode("ascii", errors="strict")
    c = _Cursor(text, footer_start)

    std_name = c.consume_name()
    if c.eof():
        c.err("a fixed-offset footer requires an explicit offset")
    std_offset = c.consume_offset()
    if c.eof():
        return PosixTZ(std_name=std_name, std_offset=std_offset)

    dst_name = c.consume_name()
    # RFC 8536: if the DST offset is omitted it defaults to one hour ahead
    # of the standard offset (in seconds-east-of-UTC terms).
    if c.eof() or c.peek() == ",":
        dst_offset = std_offset + 3600
    else:
        dst_offset = c.consume_offset()
    if c.eof():
        c.err("seasonal footer requires two explicit Mm.w.d/time rules")
    c.expect(",")
    start = c.consume_rule(version)
    c.expect(",")
    end = c.consume_rule(version)
    if not c.eof():
        c.err("trailing bytes after the two seasonal rules")

    tz = PosixTZ(
        std_name=std_name,
        std_offset=std_offset,
        dst_name=dst_name,
        dst_offset=dst_offset,
        start=start,
        end=end,
    )
    check_alternation(tz)
    return tz
