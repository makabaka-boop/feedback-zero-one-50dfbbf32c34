"""Command line entry point.

Usage:
    python -m tzmatch TZIF_FILE TIMES_JSON [-o OUTPUT_JSON]

On success the result array (one list per input clock face) is written
atomically: a temporary file in the destination directory is fsynced and
renamed over the target, so a failure at any earlier step leaves any
previously existing output untouched.  Without ``-o`` the result is
printed to standard output instead.
"""

import argparse
import json
import os
import sys
import tempfile

from .engine import run
from .errors import TZMatchError


def _fail(exc):
    payload = {"error": exc.message}
    if exc.byte is not None:
        payload["byte"] = exc.byte
    if exc.path is not None:
        payload["path"] = exc.path
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 1


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tzmatch-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tzmatch",
        description="Match local wall-clock times to UTC integer seconds via an RFC 8536 TZif file.",
    )
    parser.add_argument("tzif", help="path to a version-2/3 TZif file")
    parser.add_argument("times", help="path to a JSON array of YYYY-MM-DDTHH:MM:SS strings")
    parser.add_argument("-o", "--output", help="destination JSON file (default: stdout)")
    args = parser.parse_args(argv)

    try:
        try:
            with open(args.tzif, "rb") as f:
                tzif_bytes = f.read()
        except OSError as exc:
            raise TZMatchError("cannot read TZif file: %s" % exc.strerror, byte=0)
        try:
            with open(args.times, "r", encoding="utf-8") as f:
                times = json.load(f)
        except OSError as exc:
            raise TZMatchError("cannot read times JSON: %s" % exc.strerror, path=[])
        except json.JSONDecodeError as exc:
            raise TZMatchError("times JSON is not valid JSON: %s" % exc.msg, path=[])

        result = run(tzif_bytes, times)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
        if args.output:
            _atomic_write(args.output, text)
        else:
            sys.stdout.write(text)
        return 0
    except TZMatchError as exc:
        return _fail(exc)


if __name__ == "__main__":
    sys.exit(main())
