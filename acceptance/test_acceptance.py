"""Independent acceptance suite for the tzmatch job.

Run with the isolated interpreter, e.g.::

    python3 -I -m unittest acceptance.test_acceptance -v

The suite is self-contained (it only uses the standard library and the
local ``tzif_builder`` test helper) and covers:

* calendar math and strict input validation;
* the worked overlap example from the task;
* spring-forward gaps, fall-back overlaps and >2 candidate constructions;
* southern-hemisphere cross-year seasonal rules and week-5 rules;
* empty-footer continuation and no-transition files;
* strict rejection of malformed TZif binary and POSIX TZ footers, with
  zero-based byte offsets and zero-based array paths;
* atomic output replacement (a failed run keeps the previous result);
* a zoneinfo cross-check limited to the common legal subset.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tzif_builder import make_tzif  # noqa: E402

from tzmatch import cal, tzif  # noqa: E402
from tzmatch.engine import Engine, parse_wall_time, run  # noqa: E402
from tzmatch.errors import FileFormatError, InputError  # noqa: E402
from tzmatch.cal import civil_to_epoch  # noqa: E402


def wall_epoch(s):
    return parse_wall_time(s, 0)


class CalendarTests(unittest.TestCase):
    def test_roundtrip(self):
        for days in range(-700000, 700000, 997):
            y, m, d = cal.civil_from_days(days)
            self.assertEqual(cal.days_from_civil(y, m, d), days)

    def test_epoch_known_dates(self):
        self.assertEqual(civil_to_epoch(1970, 1, 1, 0, 0, 0), 0)
        self.assertEqual(civil_to_epoch(2000, 1, 1, 0, 0, 0), 946684800)
        self.assertEqual(civil_to_epoch(1969, 12, 31, 23, 59, 59), -1)
        # 2000 is a leap year; 1900 is not.
        self.assertEqual(cal.days_in_month(2000, 2), 29)
        self.assertEqual(cal.days_in_month(1900, 2), 28)
        self.assertEqual(cal.days_in_month(2024, 2), 29)
        self.assertEqual(cal.days_in_month(2023, 2), 28)

    def test_weekday_and_mwd(self):
        # 2024-03-01 was a Friday (weekday 5).
        self.assertEqual(cal.weekday(2024, 3, 1), 5)
        # Last Sunday (dow 0) of March 2024 is the 31st.
        self.assertEqual(cal.mwd_day(2024, 3, 5, 0), 31)
        # First Sunday is the 3rd.
        self.assertEqual(cal.mwd_day(2024, 3, 1, 0), 3)
        # February 2024 has no 5th Sunday; last Sunday is the 25th.
        self.assertEqual(cal.mwd_day(2024, 2, 5, 0), 25)


class InputValidationTests(unittest.TestCase):
    def _ok_file(self, footer=b""):
        return make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "AAA"), (0, 0, "BBB")],
            abbrs=["AAA", "BBB"], footer=footer,
        )

    def test_empty_input(self):
        self.assertEqual(run(self._ok_file(), []), [])

    def test_strict_format(self):
        good = ["1970-01-01T00:00:00", "9998-12-31T23:59:59", "1900-01-01T00:00:00"]
        for s in good:
            self.assertIsInstance(parse_wall_time(s, 0), int)
        bad = [
            "1970-1-1T00:00:00", "1970/01/01T00:00:00", "1970-01-01 00:00:00",
            "1970-01-01T00:00:00Z", "1970-01-01T24:00:00", "1970-01-01T00:00:60",
            "1899-12-31T23:59:59", "9999-01-01T00:00:00", "1970-02-29T00:00:00",
            "1900-02-29T00:00:00", "1900-04-31T00:00:00", "",
        ]
        for s in bad:
            with self.assertRaises(InputError) as ctx:
                parse_wall_time(s, 7)
            self.assertEqual(ctx.exception.path, [7])

    def test_non_string_and_non_array(self):
        with self.assertRaises(InputError):
            run(self._ok_file(), [123])
        with self.assertRaises(InputError) as ctx:
            run(self._ok_file(), "not-a-list")
        self.assertEqual(ctx.exception.path, [])

    def test_path_reports_index(self):
        with self.assertRaises(InputError) as ctx:
            run(self._ok_file(), ["1970-01-01T00:00:00", "bad", "1970-01-01T00:00:00"])
        self.assertEqual(ctx.exception.path, [1])


class WorkedExampleTests(unittest.TestCase):
    def test_overlap_example(self):
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "AAA"), (0, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )
        self.assertEqual(run(data, ["1970-01-01T00:30:00"]), [[-1800, 1800]])

    def test_outputs_sorted_deduped_in_input_order(self):
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "AAA"), (0, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )
        res = run(data, [
            "1970-01-01T00:30:00",
            "1970-01-01T00:00:00",
            "1970-01-01T00:30:00",
        ])
        # 00:00 wall under offsets +3600 (u=-3600, before transition) and
        # 0 (u=0, at/after transition) is reachable twice.
        self.assertEqual(res, [[-1800, 1800], [-3600, 0], [-1800, 1800]])


class TransitionTests(unittest.TestCase):
    def _spring_forward(self):
        # At UTC 7200 (02:00 UTC) local clocks jump 02:00 -> 03:00,
        # offset changes +3600 -> +7200. Local times 02:00..02:59:59 vanish.
        return make_tzif(
            transitions=[7200], type_index=[1],
            types=[(3600, 0, "STD"), (7200, 1, "DST")],
            abbrs=["STD", "DST"],
        )

    def test_spring_forward_gap(self):
        eng = Engine(tzif.parse(self._spring_forward()))
        # Transition at UTC 7200, offset +3600 -> +7200. Just before it the
        # local clock reads 02:59:59, then it jumps to 04:00:00, so every
        # local time 03:00:00..03:59:59 vanishes (the gap).
        # 02:30 local before the jump (offset 3600) -> UTC 01:30 = 5400.
        self.assertEqual(eng.candidates(wall_epoch("1970-01-01T02:30:00")), [5400])
        # 03:30 local is inside the gap: no integer UTC second maps to it.
        self.assertEqual(eng.candidates(wall_epoch("1970-01-01T03:30:00")), [])
        # 04:30 local after the jump (offset 7200) -> UTC 02:30 = 9000.
        self.assertEqual(eng.candidates(wall_epoch("1970-01-01T04:30:00")), [9000])

    def test_fall_back_overlap_two_candidates(self):
        # At UTC 7200 offset changes +7200 -> +3600; local 03:00 repeats.
        data = make_tzif(
            transitions=[7200], type_index=[1],
            types=[(7200, 1, "DST"), (3600, 0, "STD")],
            abbrs=["DST", "STD"],
        )
        eng = Engine(tzif.parse(data))
        # 03:30 local happens twice: UTC 01:30 (DST, +7200) and 02:30 (STD,+3600).
        self.assertEqual(
            eng.candidates(wall_epoch("1970-01-01T03:30:00")), [5400, 9000]
        )

    def test_three_candidate_construction(self):
        # Three descending-offset regions make one wall clock reachable at
        # three distinct UTC seconds, proving there is no two-candidate cap:
        #   u < 100        -> offset +7200
        #   100 <= u < 200 -> offset +3600
        #   u >= 200       -> offset 0
        # Wall clock W = 3700 is reached by:
        #   off 7200: u = -3500 (region A), off 3600: u = 100 (region B),
        #   off 0:    u = 3700  (region C).
        data = make_tzif(
            transitions=[100, 200], type_index=[1, 2],
            types=[(7200, 0, "O2"), (3600, 0, "O1"), (0, 0, "O0")],
            abbrs=["O0", "O1", "O2"],
        )
        eng = Engine(tzif.parse(data))
        self.assertEqual(eng.candidates(3700), [-3500, 100, 3700])
        for u in (-3500, 100, 3700):
            self.assertEqual(u + eng.offset_at(u), 3700)
        # Generic invariants hold across the surrounding window too.
        for w in range(-400, 4200):
            c = eng.candidates(w)
            self.assertEqual(c, sorted(set(c)))
            for u in c:
                self.assertEqual(u + eng.offset_at(u), w)

    def test_boundary_instant_uses_new_type(self):
        # Exactly at the transition the new type is already in effect.
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "AAA"), (0, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )
        eng = Engine(tzif.parse(data))
        self.assertEqual(eng.offset_at(-1), 3600)
        self.assertEqual(eng.offset_at(0), 0)
        self.assertEqual(eng.offset_at(1), 0)

    def test_first_type_before_any_transition(self):
        data = make_tzif(
            transitions=[100000], type_index=[1],
            types=[(1800, 0, "AAA"), (0, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )
        eng = Engine(tzif.parse(data))
        self.assertEqual(eng.offset_at(-10 ** 9), 1800)


class NoTransitionAndEmptyFooterTests(unittest.TestCase):
    def test_no_transition_uses_type_zero(self):
        data = make_tzif(types=[(19800, 0, "IST")], abbrs=["IST"])
        eng = Engine(tzif.parse(data))
        # 05:30 local under +19800 -> UTC = wall - 19800.
        w = wall_epoch("1970-01-01T05:30:00")
        self.assertEqual(eng.candidates(w), [w - 19800])

    def test_empty_footer_continues_last_type(self):
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "AAA"), (1800, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )  # empty footer
        eng = Engine(tzif.parse(data))
        self.assertIsNone(eng.posix)
        # Far after the last transition the final offset (+1800) persists.
        w = wall_epoch("2030-06-15T12:00:00")
        self.assertEqual(eng.candidates(w), [w - 1800])


class PosixFooterTests(unittest.TestCase):
    def test_fixed_offset_footer(self):
        data = make_tzif(types=[(0, 0, "UTC")], abbrs=["UTC"], footer=b"AAA0")
        eng = Engine(tzif.parse(data))
        self.assertTrue(eng.posix.fixed)
        self.assertEqual(eng.posix.std_offset, 0)

    def test_fixed_offset_footer_must_match_type_zero(self):
        data = make_tzif(types=[(3600, 0, "AAA")], abbrs=["AAA"], footer=b"AAA2")
        with self.assertRaises(FileFormatError):
            Engine(tzif.parse(data))

    def test_seasonal_southern_hemisphere_cross_year(self):
        # Standard AEST +10000, DST AEDT +11000. DST starts in October,
        # ends in April: within one calendar year the END rule fires
        # before the START rule (cross-year DST interval).
        footer = b"AEST-10AEDT-11,M10.1.0/2,M4.1.0/3"
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(36000, 0, "AEST"), (39600, 1, "AEDT")],
            abbrs=["AEST", "AEDT"], footer=footer,
        )
        eng = Engine(tzif.parse(data))
        self.assertFalse(eng.posix.fixed)
        # January 2021 (southern summer) is DST: +39600.
        w = wall_epoch("2021-01-15T12:00:00")
        self.assertEqual(eng.offset_at(w - 39600), 39600)
        # July 2021 is standard: +36000.
        w2 = wall_epoch("2021-07-15T12:00:00")
        self.assertEqual(eng.offset_at(w2 - 36000), 36000)

    def test_week_five_rule_and_last_weekday(self):
        footer = b"STD-1DST-2,M3.5.0/2,M10.5.0/2"
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "STD"), (7200, 1, "DST")],
            abbrs=["STD", "DST"], footer=footer,
        )
        eng = Engine(tzif.parse(data))
        # 2024 last Sunday of March is the 31st at 02:00 standard local.
        start_local = civil_to_epoch(2024, 3, 31, 2, 0, 0)
        self.assertEqual(
            eng._footer_offset(start_local - 3600 - 1), 3600
        )
        self.assertEqual(
            eng._footer_offset(start_local - 3600), 7200
        )

    def test_v3_signed_rule_time_accepted(self):
        footer = b"STD-1DST-2,M3.2.0/-2,M11.1.0/167"
        data = make_tzif(
            version="3",
            transitions=[0], type_index=[1],
            types=[(3600, 0, "STD"), (7200, 1, "DST")],
            abbrs=["STD", "DST"], footer=footer,
        )
        eng = Engine(tzif.parse(data))
        self.assertFalse(eng.posix.fixed)
        self.assertEqual(eng.posix.start.hour, -2)
        self.assertEqual(eng.posix.end.hour, 167)

    def test_v2_rejects_signed_rule_time(self):
        footer = b"STD-1DST-2,M3.2.0/-2,M11.1.0/2"
        data = make_tzif(
            version="2",
            transitions=[0], type_index=[1],
            types=[(3600, 0, "STD"), (7200, 1, "DST")],
            abbrs=["STD", "DST"], footer=footer,
        )
        with self.assertRaises(FileFormatError):
            Engine(tzif.parse(data))

    def test_explicit_rule_time_required(self):
        # Omitting /time must be rejected even though POSIX allows default.
        footer = b"STD-1DST-2,M3.2.0,M11.1.0/2"
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "STD"), (7200, 1, "DST")],
            abbrs=["STD", "DST"], footer=footer,
        )
        with self.assertRaises(FileFormatError):
            Engine(tzif.parse(data))

    def test_name_must_be_three_to_six_letters(self):
        for bad in [b"AB0-1", b"AB-1", b"ABCDEFG-1", b"<ABC>-1"]:
            data = make_tzif(types=[(3600, 0, "AAA")], abbrs=["AAA"], footer=bad)
            with self.assertRaises(FileFormatError):
                Engine(tzif.parse(data))

    def test_non_alternating_events_rejected(self):
        from tzmatch.posix import PosixTZ, Rule, check_alternation
        import tzmatch.posix as px
        # Two hand-built seasonal objects whose event stream is forced to
        # contain adjacent start events, exercising the alternation guard
        # even though distinct well-formed M-rules cannot normally do so.
        rule_a = Rule(1, 1, 0, 2, 0, 0)
        rule_b = Rule(2, 1, 0, 2, 0, 0)
        tz = PosixTZ(
            std_name="STD", std_offset=3600, dst_name="DST", dst_offset=7200,
            start=rule_a, end=rule_b,
        )
        original = px.rule_event_epoch
        def fake(year, rule, utoff):
            # 1900: start then end; 1901: a start arrives before any 1901
            # end and directly follows the 1900 end -> start,start adjacency
            # is avoided here; instead make 1900 itself start,start,end.
            if year == 1900 and rule is rule_a:
                return 10
            if year == 1900 and rule is rule_b:
                return 30
            if year == 1901 and rule is rule_a:
                return 20   # sorts between the two 1900 events -> start adjacency
            return original(year, rule, utoff)
        px.rule_event_epoch = fake
        try:
            with self.assertRaises(FileFormatError):
                check_alternation(tz)
        finally:
            px.rule_event_epoch = original

    def test_simultaneous_events_rejected(self):
        from tzmatch.posix import PosixTZ, Rule, check_alternation
        import tzmatch.posix as px
        rule_a = Rule(1, 1, 0, 2, 0, 0)
        rule_b = Rule(2, 1, 0, 2, 0, 0)
        tz = PosixTZ(
            std_name="STD", std_offset=3600, dst_name="DST", dst_offset=7200,
            start=rule_a, end=rule_b,
        )
        original = px.rule_event_epoch
        def fake(year, rule, utoff):
            # Collapse the start and end of year 1900 onto the same instant.
            if year == 1900:
                return 100
            return original(year, rule, utoff)
        px.rule_event_epoch = fake
        try:
            with self.assertRaises(FileFormatError):
                check_alternation(tz)
        finally:
            px.rule_event_epoch = original

    def test_well_formed_distinct_rules_alternate_and_accept(self):
        # Two different M-rules in the same year always produce distinct,
        # alternating events; a normal northern-hemisphere footer is accepted.
        footer = b"STD-1DST-2,M3.5.0/2,M10.5.0/2"
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "STD"), (7200, 1, "DST")],
            abbrs=["STD", "DST"], footer=footer,
        )
        eng = Engine(tzif.parse(data))
        self.assertFalse(eng.posix.fixed)

    def test_footer_must_agree_with_last_type(self):
        # Last transition type is +1800 but footer standard offset is +3600.
        footer = b"STD-1DST-2,M3.5.0/2,M10.5.0/2"
        data = make_tzif(
            transitions=[0], type_index=[1],
            types=[(3600, 0, "STD"), (1800, 1, "DST")],
            abbrs=["STD", "DST"], footer=footer,
        )
        with self.assertRaises(FileFormatError):
            Engine(tzif.parse(data))


class BinaryRejectionTests(unittest.TestCase):
    def _base(self):
        return make_tzif(
            transitions=[10, 20], type_index=[0, 1],
            types=[(0, 0, "AAA"), (3600, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )

    def test_bad_magic_offset_zero(self):
        data = bytearray(self._base())
        data[0] = ord("X")
        with self.assertRaises(FileFormatError) as ctx:
            tzif.parse(bytes(data))
        self.assertEqual(ctx.exception.byte, 0)

    def test_bad_version(self):
        data = bytearray(self._base())
        data[4] = ord("1")
        with self.assertRaises(FileFormatError) as ctx:
            tzif.parse(bytes(data))
        self.assertEqual(ctx.exception.byte, 4)

    def test_reserved_bytes_nonzero(self):
        data = bytearray(self._base())
        data[5] = 1
        with self.assertRaises(FileFormatError) as ctx:
            tzif.parse(bytes(data))
        self.assertEqual(ctx.exception.byte, 5)

    def test_nonzero_leapcnt_rejected(self):
        data = make_tzif(
            types=[(0, 0, "AAA")], abbrs=["AAA"],
            corrupt_counts={"leapcnt": 1},
        )
        with self.assertRaises(FileFormatError) as ctx:
            tzif.parse(data)
        self.assertEqual(ctx.exception.byte, 28)

    def test_truncation_reports_file_length(self):
        data = self._base()
        cut = len(data) - 10
        with self.assertRaises(FileFormatError) as ctx:
            tzif.parse(data[:cut])
        self.assertEqual(ctx.exception.byte, cut)

    def test_trailing_bytes_rejected(self):
        data = make_tzif(
            types=[(0, 0, "AAA")], abbrs=["AAA"], trailing=b"x",
        )
        with self.assertRaises(FileFormatError) as ctx:
            tzif.parse(data)
        self.assertEqual(ctx.exception.byte, len(data) - 1)

    def test_non_increasing_transition_rejected(self):
        data = make_tzif(
            transitions=[20, 20], type_index=[0, 1],
            types=[(0, 0, "AAA"), (3600, 0, "BBB")],
            abbrs=["AAA", "BBB"],
        )
        with self.assertRaises(FileFormatError):
            tzif.parse(data)

    def test_bad_type_index_rejected(self):
        data = make_tzif(
            transitions=[10], type_index=[9],
            types=[(0, 0, "AAA")], abbrs=["AAA"],
        )
        with self.assertRaises(FileFormatError):
            tzif.parse(data)

    def test_bad_isdst_rejected(self):
        # Build a ttinfo with isdst=2 by hand.
        data = bytearray(self._base())
        # Locate the first ttinfo in the 64-bit block via the second header.
        second = data.find(b"TZif", 1)
        _, counts = tzif._parse_header(bytes(data), second)
        timecnt = counts["timecnt"]
        ttinfo = second + tzif.HEADER_LEN + 8 * timecnt + timecnt
        data[ttinfo + 4] = 2
        with self.assertRaises(FileFormatError):
            tzif.parse(bytes(data))

    def test_indicator_count_must_be_zero_or_typecnt(self):
        data = make_tzif(
            types=[(0, 0, "AAA")], abbrs=["AAA"],
            corrupt_counts={"ttisstdcnt": 1, "ttisutcnt": 1},
        )
        # typecnt is 1 here, so 1 is actually valid; force a bad value instead.
        data = make_tzif(
            types=[(0, 0, "AAA"), (3600, 0, "BBB")], abbrs=["AAA", "BBB"],
            corrupt_counts={"ttisstdcnt": 1},
        )
        with self.assertRaises(FileFormatError):
            tzif.parse(data)


class AtomicOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tzmatch-accept-")
        self.tz = os.path.join(self.tmp, "zone.tzif")
        self.times = os.path.join(self.tmp, "times.json")
        self.out = os.path.join(self.tmp, "out.json")
        with open(self.tz, "wb") as f:
            f.write(make_tzif(
                transitions=[0], type_index=[1],
                types=[(3600, 0, "AAA"), (0, 0, "BBB")],
                abbrs=["AAA", "BBB"],
            ))

    def _run_cli(self, times_obj):
        with open(self.times, "w") as f:
            json.dump(times_obj, f)
        tool = os.path.join(ROOT, "tzmatch_tool.py")
        return subprocess.run(
            [sys.executable, "-I", tool, self.tz, self.times, "-o", self.out],
            capture_output=True, text=True,
        )

    def test_success_replaces_output(self):
        r = self._run_cli(["1970-01-01T00:30:00"])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            self.assertEqual(json.load(f), [[-1800, 1800]])

    def test_failure_keeps_previous_result(self):
        r1 = self._run_cli(["1970-01-01T00:30:00"])
        self.assertEqual(r1.returncode, 0)
        with open(self.out) as f:
            previous = f.read()
        r2 = self._run_cli(["not-a-time"])
        self.assertNotEqual(r2.returncode, 0)
        err = json.loads(r2.stderr.strip())
        self.assertIn("path", err)
        self.assertEqual(err["path"], [0])
        with open(self.out) as f:
            self.assertEqual(f.read(), previous)

    def test_missing_file_reports_error(self):
        tool = os.path.join(ROOT, "tzmatch_tool.py")
        r = subprocess.run(
            [sys.executable, "-I", tool,
             os.path.join(self.tmp, "nope.tzif"), self.times, "-o", self.out],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stderr.strip())["byte"], 0)


class ZoneinfoCrossCheck(unittest.TestCase):
    """Limited to the common legal subset (see module docstring)."""

    ZONES = [
        "Europe/Berlin", "America/New_York", "Australia/Sydney",
        "Europe/London", "Europe/Paris", "America/Chicago",
        "Asia/Tokyo", "Pacific/Auckland", "UTC",
    ]

    @staticmethod
    def _normalize(footer):
        import re
        s = footer.decode()
        s = re.sub(
            r"M\d+\.\d+\.\d+(?:/[\d:+-]+)?",
            lambda m: m.group(0) if "/" in m.group(0) else m.group(0) + "/2",
            s,
        )
        return s.encode()

    def test_matches_zoneinfo(self):
        for name in self.ZONES:
            with self.subTest(zone=name):
                path = os.path.join("/usr/share/zoneinfo", name)
                with open(path, "rb") as fh:
                    parsed = tzif.parse(fh.read())
                parsed.footer = self._normalize(parsed.footer)
                eng = Engine(parsed)
                zi = ZoneInfo(name)
                offsets = {t.utoff for t in eng.d.types}
                if eng.posix and not eng.posix.fixed:
                    offsets |= {eng.posix.std_offset, eng.posix.dst_offset}
                probes = set()
                for t in eng.d.transitions:
                    for delta in (-7200, -3600, -1, 0, 1, 3600, 7200):
                        probes.add(t + delta)
                for u0 in sorted(probes):
                    dt = datetime.datetime.fromtimestamp(
                        u0, datetime.timezone.utc
                    ).astimezone(zi)
                    w = dt.replace(tzinfo=None)
                    if not (1990 <= w.year <= 2022):
                        continue
                    we = civil_to_epoch(
                        w.year, w.month, w.day, w.hour, w.minute, w.second
                    )
                    key = (w.year, w.month, w.day, w.hour, w.minute, w.second)
                    expected = set()
                    for off in offsets:
                        u = we - off
                        d2 = datetime.datetime.fromtimestamp(
                            u, datetime.timezone.utc
                        ).astimezone(zi).replace(tzinfo=None)
                        if (d2.year, d2.month, d2.day, d2.hour,
                                d2.minute, d2.second) == key:
                            expected.add(u)
                    self.assertEqual(
                        eng.candidates(we), sorted(expected), msg=name
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
