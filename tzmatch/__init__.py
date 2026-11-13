"""tzmatch: TZif (RFC 8536) based wall-clock to UTC-integer-second matcher.

The package deliberately implements TZif parsing, the restricted POSIX TZ
footer grammar and all calendar math itself; no third-party timezone code
is used.  Only the Python standard library is required.
"""

from .errors import FileFormatError, InputError, TZMatchError
from .engine import run

__all__ = ["run", "TZMatchError", "FileFormatError", "InputError"]
