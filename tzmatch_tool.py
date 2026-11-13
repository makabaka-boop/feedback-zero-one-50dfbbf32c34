#!/usr/bin/env python3
"""Top-level entry script so the job can also be run as ``python tzmatch_tool.py ...``.

The path bootstrap is deliberate: test data directories may contain planted
modules, and the acceptance suite runs the interpreter with ``-I`` (which
removes the script directory from ``sys.path`` in some configurations), so
the directory of this file is added explicitly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tzmatch.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
