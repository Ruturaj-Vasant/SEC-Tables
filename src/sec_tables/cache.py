"""On-disk cache for fetched filings.

Filings are **immutable once filed** — the 1997 Delta proxy will never change. So
a fetched document can be kept forever, which matters for two reasons beyond
speed: it keeps repeat runs off SEC's servers entirely, and it makes a result
reproducible against the exact bytes that produced it.

Layout mirrors the source so a cache directory is browsable and can be handed
straight to `LocalSource`:

    <cache>/<TICKER>/<FORM_FS>/<YYYY-MM-DD>_<FORM_FS><ext>

Keyed on identity (ticker, form, date), not on a content hash: the point is to
avoid re-fetching, and a hash cannot be computed without first fetching.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .sources import FilingRef, form_to_fs

DEFAULT_DIR_ENV = "SEC_TABLES_CACHE"


def default_cache_dir() -> Path:
    """Cache location: $SEC_TABLES_CACHE, else an XDG-ish user cache path."""
    if env := os.environ.get(DEFAULT_DIR_ENV):
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "sec-tables"


@dataclass
class FilingCache:
    root: Path
    enabled: bool = True

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    def path_for(self, ref: FilingRef, suffix: str = ".html") -> Path:
        fs_form = form_to_fs(ref.form)
        name = f"{ref.filing_date.isoformat()}_{fs_form}{suffix}"
        return self.root / ref.ticker.upper() / fs_form / name

    def find(self, ref: FilingRef) -> Optional[Path]:
        """An existing cached copy, whatever extension it was stored under."""
        if not self.enabled:
            return None
        for suffix in (".html", ".htm", ".txt"):
            p = self.path_for(ref, suffix)
            if p.is_file() and p.stat().st_size > 0:
                return p
        return None

    def get(self, ref: FilingRef) -> Optional[bytes]:
        p = self.find(ref)
        if p is None:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def put(self, ref: FilingRef, data: bytes, suffix: str = ".html") -> Optional[Path]:
        """Store bytes. Returns the path, or None when caching is disabled.

        Writes to a temporary file and renames, so an interrupted run cannot leave
        a truncated document that a later run would treat as a valid cache hit.
        """
        if not self.enabled or not data:
            return None
        p = self.path_for(ref, suffix)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        try:
            tmp.write_bytes(data)
            tmp.replace(p)
        except OSError:
            tmp.unlink(missing_ok=True)
            return None
        return p

    def stats(self) -> dict[str, int]:
        if not self.root.is_dir():
            return {"filings": 0, "bytes": 0, "tickers": 0}
        files = [p for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(".part")]
        tickers = {p.name for p in self.root.iterdir() if p.is_dir()}
        return {
            "filings": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "tickers": len(tickers),
        }


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
