"""RFC 8536 TZif parser (versions 2 and 3 only), strict and self-contained.

The 32-bit block is only length-checked then skipped; the 64-bit block is
the authoritative one.  Every field mentioned in the task is validated:
magic/version, reserved zero bytes, counts, leap counts, strictly
increasing transitions, type indices, ttinfo fields, abbreviation
indices, indicator bytes, footer framing and absence of trailing bytes.
"""

import struct
from dataclasses import dataclass, field

from .errors import FileFormatError

MAGIC = b"TZif"
HEADER_LEN = 44
SUPPORTED_VERSIONS = (ord("2"), ord("3"))


@dataclass
class TType:
    utoff: int          # signed seconds east of UTC
    isdst: int          # 0 standard, 1 DST
    isstd: int          # ttisstd indicator
    isut: int           # ttisut indicator
    abbr: str


@dataclass
class TZData:
    version: int
    transitions: list = field(default_factory=list)   # int UTC seconds, strictly increasing
    type_index: list = field(default_factory=list)    # per-transition type index
    types: list = field(default_factory=list)         # TType list
    footer: bytes = b""                                # raw POSIX TZ string (may be empty)
    footer_start: int = 0                              # absolute byte offset of footer[0]


class _Reader:
    def __init__(self, data):
        self.data = data
        self.n = len(data)
        self.pos = 0

    def require(self, length):
        if self.pos + length > self.n:
            # Truncation is reported at the current file length.
            raise FileFormatError(
                "truncated TZif file: need %d more byte(s)" % length, byte=self.n
            )

    def take(self, length):
        self.require(length)
        chunk = self.data[self.pos:self.pos + length]
        self.pos += length
        return chunk

    def u8(self):
        return self.take(1)[0]

    def s32(self):
        return struct.unpack(">l", self.take(4))[0]

    def s64(self):
        return struct.unpack(">q", self.take(8))[0]


def _parse_header(data, off):
    """Return (version, counts dict) for one header at byte offset ``off``."""
    if len(data) < off + HEADER_LEN:
        raise FileFormatError("truncated TZif header", byte=len(data))
    if data[off:off + 4] != MAGIC:
        raise FileFormatError("bad magic", byte=off)
    version = data[off + 4]
    if version not in SUPPORTED_VERSIONS:
        raise FileFormatError("unsupported TZif version (only 2 and 3)", byte=off + 4)
    if any(b != 0 for b in data[off + 5:off + 20]):
        raise FileFormatError("reserved header bytes must be zero", byte=off + 5)
    # RFC 8536 on-the-wire order.
    ttisstdcnt, ttisutcnt, leapcnt, timecnt, typecnt, charcnt = struct.unpack(
        ">6L", data[off + 20:off + 44]
    )
    counts = {
        "ttisstdcnt": ttisstdcnt,
        "ttisutcnt": ttisutcnt,
        "leapcnt": leapcnt,
        "timecnt": timecnt,
        "typecnt": typecnt,
        "charcnt": charcnt,
    }
    return version, counts


def _block_size(c, wide):
    time_field = 8 if wide else 4
    leap_field = 12 if wide else 8
    # transition times + transition type indices + ttinfo array +
    # abbreviation block + leap records + two indicator arrays.
    return (
        time_field * c["timecnt"]
        + c["timecnt"]
        + 6 * c["typecnt"]
        + c["charcnt"]
        + leap_field * c["leapcnt"]
        + c["ttisstdcnt"]
        + c["ttisutcnt"]
    )


def _check_counts(c, base_off):
    if c["leapcnt"] != 0:
        raise FileFormatError(
            "leap seconds are not supported (leapcnt must be 0)", byte=base_off + 28
        )
    if c["typecnt"] < 1:
        raise FileFormatError(
            "at least one local time type is required", byte=base_off + 40
        )
    if c["ttisstdcnt"] not in (0, c["typecnt"]) or c["ttisutcnt"] not in (0, c["typecnt"]):
        raise FileFormatError(
            "indicator counts must be 0 or equal to typecnt", byte=base_off + 20
        )


def parse(data):
    if not isinstance(data, (bytes, bytearray)):
        raise FileFormatError("TZif data must be bytes", byte=0)
    data = bytes(data)

    version, c1 = _parse_header(data, 0)
    _check_counts(c1, 0)

    r = _Reader(data)
    r.pos = HEADER_LEN
    # The 32-bit block is validated for length then skipped wholesale.
    r.take(_block_size(c1, wide=False))

    second_off = r.pos
    version2, c2 = _parse_header(data, second_off)
    if version2 != version:
        raise FileFormatError(
            "TZif version markers differ between headers", byte=second_off + 4
        )
    if c2 != c1:
        raise FileFormatError(
            "header counts differ between the two TZif blocks", byte=second_off + 20
        )
    _check_counts(c2, second_off)
    # _parse_header does not advance the reader; position at the start of
    # the 64-bit block and explain its fields in place.
    r.pos = second_off + HEADER_LEN

    timecnt = c2["timecnt"]
    typecnt = c2["typecnt"]
    charcnt = c2["charcnt"]
    ttisstdcnt = c2["ttisstdcnt"]
    ttisutcnt = c2["ttisutcnt"]

    # --- 64-bit block, explained field by field ---
    block_start = r.pos
    trans_times = [r.s64() for _ in range(timecnt)]
    for i in range(1, timecnt):
        if trans_times[i] <= trans_times[i - 1]:
            raise FileFormatError(
                "transition times must be strictly increasing",
                byte=block_start + 8 * i,
            )

    type_index = [r.u8() for _ in range(timecnt)]
    type_index_off = block_start + 8 * timecnt
    for i, idx in enumerate(type_index):
        if idx >= typecnt:
            raise FileFormatError(
                "transition type index out of range", byte=type_index_off + i
            )

    ttinfo_off = r.pos
    raw_types = []
    for i in range(typecnt):
        utoff = r.s32()
        isdst = r.u8()
        abbridx = r.u8()
        if isdst not in (0, 1):
            raise FileFormatError(
                "isdst must be 0 or 1", byte=ttinfo_off + 6 * i + 4
            )
        if abbridx >= charcnt:
            raise FileFormatError(
                "abbreviation index out of range", byte=ttinfo_off + 6 * i + 5
            )
        raw_types.append((utoff, isdst, abbridx))

    abbr_off = r.pos
    abbr_block = r.take(charcnt)
    for j, b in enumerate(abbr_block):
        if b != 0 and (b < 0x20 or b > 0x7E):
            raise FileFormatError(
                "non-ASCII or control byte in abbreviation block",
                byte=abbr_off + j,
            )
    # RFC 8536 allows any printable-ASCII, NUL-terminated abbreviation in
    # the binary block (e.g. "GMT+5", "-03"); the 3..6 letters restriction
    # applies only to names inside the POSIX TZ footer.
    for utoff, isdst, abbridx in raw_types:
        end = abbr_block.find(b"\x00", abbridx)
        if end == -1:
            raise FileFormatError(
                "abbreviation is not NUL-terminated", byte=abbr_off + abbridx
            )
        name_bytes = abbr_block[abbridx:end]
        if len(name_bytes) == 0:
            raise FileFormatError(
                "empty time type abbreviation", byte=abbr_off + abbridx
            )

    # Indicator arrays (the leap block is absent because leapcnt is zero).
    isstd_array_off = r.pos
    isstd_bytes = r.take(ttisstdcnt)
    for i, b in enumerate(isstd_bytes):
        if b not in (0, 1):
            raise FileFormatError(
                "ttisstd indicator byte must be 0 or 1",
                byte=isstd_array_off + i,
            )
    isut_array_off = r.pos
    isut_bytes = r.take(ttisutcnt)
    for i, b in enumerate(isut_bytes):
        if b not in (0, 1):
            raise FileFormatError(
                "ttisut indicator byte must be 0 or 1",
                byte=isut_array_off + i,
            )

    # --- Footer: '\n' TZ-string '\n' and nothing afterwards ---
    if r.take(1) != b"\n":
        raise FileFormatError(
            "missing newline before POSIX TZ footer", byte=r.pos - 1
        )
    footer_start = r.pos
    nl = data.find(b"\n", footer_start)
    if nl == -1:
        raise FileFormatError("unterminated POSIX TZ footer", byte=footer_start)
    footer = data[footer_start:nl]
    for j, b in enumerate(footer):
        if b >= 0x80:
            raise FileFormatError(
                "POSIX TZ footer must be ASCII", byte=footer_start + j
            )
    r.pos = nl + 1
    if r.pos != r.n:
        raise FileFormatError(
            "no bytes are allowed after the footer newline", byte=r.pos
        )

    types = []
    for i, (utoff, isdst, abbridx) in enumerate(raw_types):
        end = abbr_block.find(b"\x00", abbridx)
        name = abbr_block[abbridx:end].decode("ascii")
        types.append(
            TType(
                utoff=utoff,
                isdst=isdst,
                isstd=isstd_bytes[i] if ttisstdcnt else 0,
                isut=isut_bytes[i] if ttisutcnt else 0,
                abbr=name,
            )
        )

    return TZData(
        version=version,
        transitions=trans_times,
        type_index=type_index,
        types=types,
        footer=footer,
        footer_start=footer_start,
    )
