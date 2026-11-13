"""Error types carrying zero-based byte offsets or array paths."""


class TZMatchError(Exception):
    """Base error.

    ``byte`` is the zero-based file byte where the offending field starts
    (for truncation it equals the current file length).  ``path`` is the
    zero-based JSON array path of an offending time element.
    """

    def __init__(self, message, *, byte=None, path=None):
        super().__init__(message)
        self.message = message
        self.byte = byte
        self.path = path


class FileFormatError(TZMatchError):
    """The TZif file or its POSIX TZ footer is invalid."""


class InputError(TZMatchError):
    """The JSON time array is invalid."""
