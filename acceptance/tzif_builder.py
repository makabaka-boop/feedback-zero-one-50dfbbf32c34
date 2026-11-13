"""TZif file builder used by the acceptance suite.

It constructs RFC 8536 version-2/3 files from explicit components, with
optional corruption hooks so rejection paths can be exercised.  This is
test-support code only; the parser under test never imports it.
"""

import struct

MAGIC = b"TZif"


def _abbr_block(names):
    """names: list[str] -> (block bytes, index per name)."""
    block = b""
    indices = []
    for name in names:
        indices.append(len(block))
        block += name.encode("ascii") + b"\x00"
    return block, indices


def make_tzif(
    *,
    version="2",
    transitions=(),
    type_index=(),
    types,
    abbrs,
    footer=b"",
    isstd=None,
    isut=None,
    leapcnt=0,
    reserved=b"\x00" * 15,
    corrupt_counts=None,
    trailing=b"",
):
    """Build a complete two-header TZif file.

    ``types`` is a list of ``(utoff, isdst, abbr_name)``; abbreviation
    indices are derived automatically.  ``isstd``/``isut`` are the
    ttisstd/ttisut indicator arrays (default all-zero, hence zero counts).
    ``corrupt_counts`` optionally maps a header field name to a fabricated
    value written into both headers.
    """
    if isinstance(version, str):
        version = ord(version)
    timecnt = len(transitions)
    typecnt = len(types)
    abbr_block, abbr_idx = _abbr_block(abbrs)
    charcnt = len(abbr_block)
    isstd = [0] * typecnt if isstd is None else list(isstd)
    isut = [0] * typecnt if isut is None else list(isut)
    # Indicator counts are zero when every byte is zero, else equal typecnt.
    ttisstdcnt = typecnt if any(isstd) else 0
    ttisutcnt = typecnt if any(isut) else 0

    header_counts = {
        "ttisstdcnt": ttisstdcnt,
        "ttisutcnt": ttisutcnt,
        "leapcnt": leapcnt,
        "timecnt": timecnt,
        "typecnt": typecnt,
        "charcnt": charcnt,
    }
    if corrupt_counts:
        header_counts.update(corrupt_counts)

    def header():
        return (
            MAGIC
            + bytes([version])
            + reserved
            + struct.pack(
                ">6L",
                header_counts["ttisstdcnt"],
                header_counts["ttisutcnt"],
                header_counts["leapcnt"],
                header_counts["timecnt"],
                header_counts["typecnt"],
                header_counts["charcnt"],
            )
        )

    def block(wide):
        pack = ">q" if wide else ">l"
        out = b"".join(struct.pack(pack, t) for t in transitions)
        out += bytes(type_index)
        for (utoff, isdst, abbr_name) in types:
            out += struct.pack(">l", utoff) + bytes(
                [isdst, abbr_idx[abbrs.index(abbr_name)]]
            )
        out += abbr_block
        # leap records omitted: test files always use leapcnt 0.
        if ttisstdcnt:
            out += bytes(isstd)
        if ttisutcnt:
            out += bytes(isut)
        return out

    h = header()
    return (
        h
        + block(False)
        + h
        + block(True)
        + b"\n"
        + footer
        + b"\n"
        + trailing
    )
