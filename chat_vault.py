#!/usr/bin/env python3
"""
AI Chat Vault: back up, restore, export and repair your local
Antigravity, Codex and Claude Code chats.

Run with no arguments for the window, "menu" for the text menu, or see --help.
Requires Python 3.8+ and no external packages.
"""
import sys

# Kept free of f-strings and other 3.x-only syntax so very old interpreters
# reach this check instead of failing with a SyntaxError.
if sys.version_info < (3, 8):
    sys.stderr.write(
        "ERROR: AI Chat Vault needs Python 3.8 or newer (this is %d.%d).\n"
        "       Install it from https://www.python.org/downloads/\n"
        % (sys.version_info[0], sys.version_info[1]))
    sys.exit(1)

from chatvault.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run(default="gui"))
