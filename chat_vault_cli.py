#!/usr/bin/env python3
"""AI Chat Vault command line: text menu with no arguments, or see --help."""
import sys

if sys.version_info < (3, 8):
    sys.stderr.write("ERROR: AI Chat Vault needs Python 3.8 or newer.\n")
    sys.exit(1)

from chatvault.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run(default="menu"))
