#!/usr/bin/env python3
"""Source-checkout entry point; use only verified source code."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from academic_radar.sqlite_delta import main

if __name__ == "__main__":
    raise SystemExit(main())
