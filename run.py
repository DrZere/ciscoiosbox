#!/usr/bin/env python3
"""Development entry point for CiscoIOSBox.

Keeps ``src/`` off the installed-package path so the app can be launched
straight from a checkout with ``python run.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ciscoiosbox.app import main  # noqa: E402  (import must follow path setup)

if __name__ == "__main__":
    sys.exit(main())
