"""Where filing bytes come from.

The core library takes bytes and returns a table; it has no opinion on how those
bytes were obtained. This module supplies them, behind one small interface, so
that a local directory and EDGAR-over-HTTP are interchangeable.

`LocalSource` works today against an on-disk corpus. `EdgarSource` (network) is a
separate optional module and must not be imported from here — keeping it out means
the core install needs no HTTP client and the CLI stays testable without a
network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional, Protocol

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# EDGAR form names contain spaces; filesystems and URLs prefer underscores.
# The original string is preserved on the ref so nothing has to guess it back.
DEFAULT_FORM = "DEF 14A"


def form_to_fs(form: str) -> str:
    return form.replace(" ", "_")


@dataclass(frozen=True)
class FilingRef:
    """One filing, located but not necessarily fetched."""

    ticker: str
    form: str
    filing_date: date
    locator: str  # path or URL — meaningful only to the source that produced it
    cik: Optional[str] = None
    # EDGAR's own identifier for the submission, when the source knows it.
    # Optional because a `LocalSource` file has none, and last because adding a
    # field to a frozen dataclass in the middle would break positional callers.
    #
    # It is here because (ticker, form, filing_date) is NOT a filing identity:
    # a company can file two documents of the same form on the same day, and
    # anything keyed on the triple silently conflates them.
    accession: Optional[str] = None

    @property
    def year(self) -> int:
        return self.filing_date.year

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.ticker} {self.form} {self.filing_date.isoformat()}"


class Source(Protocol):
    """Locates and reads filings.

    Deliberately two operations, not one. Listing is cheap and lets a caller
    choose among several filings for a year; fetching may be expensive or
    rate-limited, so it happens only for the chosen one.
    """

    name: str

    def list_filings(
        self, ticker: str, *, form: str = DEFAULT_FORM, year: Optional[int] = None
    ) -> list[FilingRef]:
        ...

    def read(self, ref: FilingRef) -> bytes:
        ...


class SourceError(RuntimeError):
    """Raised when a filing cannot be located or read."""


@dataclass
class LocalSource:
    """Reads filings from a directory tree.

    Expects the layout the project already produces:

        <root>/<TICKER>/<FORM_FS>/<YYYY-MM-DD>_<FORM_FS>.<ext>

    Both `.html` and `.txt` are accepted, and when a year has both, HTML is
    preferred — a plain-text rendition sometimes omits the table entirely while
    mentioning it in prose, which reads as an extraction failure but is really a
    missing document.
    """

    root: Path
    name: str = "local"

    _PREFERRED_SUFFIX_ORDER = (".html", ".htm", ".txt")

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if not self.root.is_dir():
            raise SourceError(f"local source root does not exist: {self.root}")

    def list_filings(
        self, ticker: str, *, form: str = DEFAULT_FORM, year: Optional[int] = None
    ) -> list[FilingRef]:
        tdir = self.root / ticker.upper()
        if not tdir.is_dir():
            return []

        out: list[FilingRef] = []
        for fdir_name in {form_to_fs(form), form}:
            fdir = tdir / fdir_name
            if not fdir.is_dir():
                continue
            for path in sorted(fdir.iterdir()):
                if path.suffix.lower() not in self._PREFERRED_SUFFIX_ORDER:
                    continue
                m = _DATE_RE.search(path.name)
                if not m:
                    continue
                try:
                    d = date(*(int(g) for g in m.groups()))
                except ValueError:
                    continue
                if year is not None and d.year != year:
                    continue
                out.append(FilingRef(ticker.upper(), form, d, str(path)))

        out.sort(key=lambda r: (r.filing_date, self._suffix_rank(r.locator)))
        return out

    def _suffix_rank(self, locator: str) -> int:
        suf = Path(locator).suffix.lower()
        return self._PREFERRED_SUFFIX_ORDER.index(suf) if suf in self._PREFERRED_SUFFIX_ORDER else 99

    def read(self, ref: FilingRef) -> bytes:
        try:
            return Path(ref.locator).read_bytes()
        except OSError as exc:
            raise SourceError(f"cannot read {ref.locator}: {exc}") from exc

    def tickers(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())


def pick_filing(
    refs: Iterable[FilingRef], *, prefer: str = "latest"
) -> Optional[FilingRef]:
    """Choose one filing from several for the same year.

    `latest` is the default because a company that files an amended or second
    proxy in a year is usually correcting the first.
    """
    items = list(refs)
    if not items:
        return None
    if prefer == "earliest":
        return items[0]
    return items[-1]
