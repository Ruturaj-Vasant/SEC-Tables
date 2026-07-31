"""On-disk cache for fetched filings.

Filings are **immutable once filed** — the 1997 Delta proxy will never change. So
a fetched document can be kept forever, which matters for two reasons beyond
speed: it keeps repeat runs off SEC's servers entirely, and it makes a result
reproducible against the exact bytes that produced it.

Layout mirrors the source so a cache directory is browsable and can be handed
straight to `LocalSource`:

    <cache>/<TICKER>/<FORM_FS>/<YYYY-MM-DD>_<FORM_FS>_<FILING><ext>

Keyed on identity, not on a content hash: the point is to avoid re-fetching, and
a hash of the content cannot be computed without first fetching it.

**`<FILING>` is not decoration.** The layout used to stop at the date, and
(ticker, form, filing_date) is not a filing identity: a company can file two
documents of the same form on the same day — an original proxy and an amended
one, or a proxy and its additional materials — and a cache keyed on the triple
returns the first one's bytes for the second one's request. Nothing about that
failure is visible: the caller asked for filing B, got filing A, and both are
real filings of the right form on the right date.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .sources import FilingRef, form_to_fs

DEFAULT_DIR_ENV = "SEC_TABLES_CACHE"

# EDGAR accession numbers: 10-digit filer prefix, 2-digit year, 6-digit sequence.
_ACCESSION_RE = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

#: Length of the fallback digest. Twelve hex characters is 48 bits — far more
#: than enough to separate the handful of filings one issuer makes in a day,
#: and short enough to keep the filename readable.
_DIGEST_CHARS = 12


def filing_token(ref: FilingRef) -> str:
    """A short, filesystem-safe token identifying *this* filing.

    Three sources, in descending order of authority:

    1. `ref.accession` — EDGAR's own identifier, when the source recorded it.
    2. An accession number found in the locator. Every archive URL carries one,
       so a ref built before `accession` existed still keys correctly.
    3. A digest of the locator. Covers `LocalSource` paths and anything else;
       it is stable, which is all the cache needs, but it says nothing to a
       human reading the directory.
    """
    accession = (ref.accession or "").strip()
    if not accession:
        found = _ACCESSION_RE.search(ref.locator or "")
        if found:
            accession = found.group(1)
    if accession:
        return _UNSAFE.sub("-", accession)
    digest = hashlib.sha256((ref.locator or "").encode("utf-8")).hexdigest()
    return digest[:_DIGEST_CHARS]


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
        """Where this filing's bytes live.

        The filing token goes after the date and before the extension, which
        keeps the name parseable by `LocalSource`: it searches the filename for
        a date and accepts any `.html`/`.htm`/`.txt`, so a cache directory
        remains a browsable corpus.
        """
        fs_form = form_to_fs(ref.form)
        name = f"{ref.filing_date.isoformat()}_{fs_form}_{filing_token(ref)}{suffix}"
        return self.root / ref.ticker.upper() / fs_form / name

    def find(self, ref: FilingRef) -> Optional[Path]:
        """An existing cached copy, whatever extension it was stored under.

        **Files written by the pre-token layout are not read.** One of those
        names, `1997-09-19_DEF_14A.txt`, could have come from any filing of
        that form on that date, and the cache has no way to tell which — so
        trusting it would mean sometimes returning the wrong filing's bytes and
        never knowing. They are left in place rather than deleted (they are
        someone's corpus, and `LocalSource` still reads them), and the filings
        they hold are fetched once more under a name that identifies them.
        """
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
