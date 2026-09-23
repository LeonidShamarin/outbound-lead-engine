"""Vercel entrypoint: the ASGI app lives in src/leadengine/web.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from leadengine.web import app  # noqa: E402,F401
