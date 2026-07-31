"""The proxy against the real SEC, for the opt-in smoke test.

Separate from `fake_sec.py` and never started by the default suite. Running this
sends real requests to SEC under the contact address the browser supplies, and
SEC's ceiling is shared by everyone on that IP — so it is opt-in, paced well
below the limit, and caches everything it fetches.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
REPO = WEB.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(WEB / "proxy"))

from sec_proxy.server import main  # noqa: E402

if __name__ == "__main__":
    cache = os.environ.get("SEC_LIVE_CACHE", str(Path.home() / ".cache" / "sec-tables-web-live"))
    raise SystemExit(main([
        "--port", os.environ.get("PROXY_PORT", "5310"),
        "--cache-dir", cache,
        "--rate", os.environ.get("SEC_RATE", "2"),
        "--quiet",
    ]))
