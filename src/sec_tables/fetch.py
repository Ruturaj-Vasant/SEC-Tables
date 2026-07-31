"""EDGAR over HTTP.

Implements the `Source` protocol, so it is interchangeable with `LocalSource` and
the rest of the library neither knows nor cares which one supplied the bytes.

Built against SEC's published access rules rather than assumption, because the
cost of being wrong here lands on the user's IP, not on a test:

* **10 requests/second is a total per-requester ceiling**, shared across
  `data.sec.gov` and `www.sec.gov` — not a per-host allowance. The default here is
  deliberately below it. SEC's published recovery after a block is ten minutes
  under the threshold.
* **A declared User-Agent is required**, in the form
  `Company or application name AdminContact@example.com`. A bare email may pass
  technically but does not follow the stated format.
* **Throttling responses are not contractually specified.** There is no guarantee
  of `Retry-After`, no guarantee of `X-RateLimit-*`, and no guarantee of 403 versus
  429. So: honour `Retry-After` when present, never depend on it, and treat 403,
  429 and transient 5xx alike as possible throttling.
* **Pre-May-2000 filings need the complete-submission path.** The accession
  *directory* layout is documented only for post-EDGAR-7.0 filings, so the
  `{accession-with-dashes}.txt` route is the primary one for exactly the era this
  library exists to cover.

Uses only the standard library, so installing the package brings no HTTP
dependency along with it.
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from .sources import DEFAULT_FORM, FilingRef, SourceError, form_to_fs

USER_AGENT_ENV = "SEC_USER_AGENT"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
SUBMISSIONS_PART_URL = "https://data.sec.gov/submissions/{name}"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
ARCHIVE_FULL_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_dashed}.txt"

# The accession-directory layout is documented for post-EDGAR-7.0 filings only.
_DIRECTORY_LAYOUT_FROM = date(2000, 5, 1)

# SEC's published recovery window after an IP is limited.
BLOCK_RECOVERY_SECONDS = 600

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class FetchError(SourceError):
    """Network or protocol failure while talking to EDGAR."""


class RateLimited(FetchError):
    """EDGAR signalled throttling and retries were exhausted."""


def resolve_user_agent(explicit: Optional[str] = None) -> str:
    """The declared User-Agent, or a hard error explaining the required form.

    Never falls back to a built-in default. A shipped default would attribute
    every user's traffic to one identity, and that identity is what gets blocked.
    """
    ua = (explicit or os.environ.get(USER_AGENT_ENV) or "").strip()
    if not ua:
        raise FetchError(
            f"SEC requires a declared User-Agent. Set {USER_AGENT_ENV}, e.g.\n"
            f'  export {USER_AGENT_ENV}="Acme Research acme@example.com"\n'
            "Format: a company or application name, then a monitored contact email."
        )
    if not _EMAIL_RE.search(ua):
        raise FetchError(
            f"{USER_AGENT_ENV} must include a contact email address.\n"
            f'  got: {ua!r}\n'
            f'  expected e.g. "Acme Research acme@example.com"'
        )
    if _EMAIL_RE.fullmatch(ua):
        raise FetchError(
            f"{USER_AGENT_ENV} should name an application or organisation as well "
            f"as an email.\n  got: {ua!r}\n"
            f'  expected e.g. "Acme Research {ua}"'
        )
    return ua


class RateLimiter:
    """Token bucket shared by every request this process makes.

    One bucket, not one per host: SEC's ceiling applies to the requester across
    SEC.gov, so per-host limiters would permit double the intended rate.
    """

    def __init__(self, rate_per_second: float = 5.0, burst: int = 1) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate_per_second)
        self.burst = max(1, int(burst))
        self._tokens = float(self.burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                time.sleep((1.0 - self._tokens) / self.rate)


@dataclass
class EdgarClient:
    """Low-level HTTP against SEC, with identification, pacing and retry."""

    user_agent: Optional[str] = None
    rate_per_second: float = 5.0
    max_retries: int = 4
    timeout: float = 30.0
    limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        self.user_agent = resolve_user_agent(self.user_agent)
        self.limiter = RateLimiter(self.rate_per_second)

    # Single seam for the network, so tests never touch it.
    def _http_get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        # No Accept-Encoding: urllib does not transparently decompress, so
        # advertising gzip would hand back compressed bytes the parser cannot read.
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read() or b""
        except urllib.error.URLError as exc:
            raise FetchError(f"network error for {url}: {exc.reason}") from exc

    def get(self, url: str) -> bytes:
        """Fetch a URL, pacing and retrying on anything that looks like throttling."""
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            status, headers, body = self._http_get(url)

            if 200 <= status < 300:
                return body
            if status == 404:
                raise FetchError(f"not found: {url}")
            # 403/429/5xx are all plausible throttling; SEC does not commit to
            # which it returns, so they are treated identically.
            if status in (403, 429) or status >= 500:
                if attempt == self.max_retries:
                    raise RateLimited(
                        f"EDGAR returned {status} for {url} after {attempt + 1} attempts. "
                        f"SEC's published recovery is ~{BLOCK_RECOVERY_SECONDS // 60} "
                        "minutes below the request threshold; reduce --rate and retry later."
                    )
                wait = self._retry_after(headers)
                if wait is None:
                    wait = delay + random.uniform(0, 0.5)  # jitter avoids lockstep retries
                    delay *= 2
                time.sleep(wait)
                continue
            raise FetchError(f"unexpected HTTP {status} for {url}")
        raise FetchError(f"exhausted retries for {url}")  # pragma: no cover

    @staticmethod
    def _retry_after(headers: dict[str, str]) -> Optional[float]:
        """Honour Retry-After when present. It frequently is not."""
        raw = None
        for key, value in headers.items():
            if key.lower() == "retry-after":
                raw = value
                break
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get(url))
        except json.JSONDecodeError as exc:
            raise FetchError(f"malformed JSON from {url}: {exc}") from exc


def _columnar_rows(block: dict[str, list]) -> list[dict[str, Any]]:
    """EDGAR returns filings as parallel arrays; turn them into records.

    Trailing arrays are occasionally shorter than the longest, so rows are cut to
    the shortest column rather than zipped blindly into IndexErrors.
    """
    if not block:
        return []
    keys = [k for k, v in block.items() if isinstance(v, list)]
    if not keys:
        return []
    n = min(len(block[k]) for k in keys)
    return [{k: block[k][i] for k in keys} for i in range(n)]


@dataclass
class EdgarSource:
    """Locates and downloads filings from EDGAR.

    `list_filings` walks the whole submission history, not just the recent block:
    EDGAR splits older filings into separate files referenced from the response,
    and ignoring them silently loses the earliest filings — which for this library
    is precisely the era that matters.
    """

    client: EdgarClient = field(default_factory=EdgarClient)
    name: str = "edgar"
    _ticker_map: Optional[dict[str, str]] = field(default=None, repr=False)

    # -- ticker resolution -------------------------------------------------
    def ticker_map(self) -> dict[str, str]:
        """Ticker -> CIK, from SEC's published mapping.

        Caveat worth stating loudly: this is a *current* association. It does not
        describe historical ticker ownership, so delisted issuers, renamed
        tickers and reused symbols are all unresolved by it. For a library that
        reaches back to 1994 that is a real limitation, not a footnote.
        """
        if self._ticker_map is None:
            raw = self.client.get_json(TICKERS_URL)
            rows = raw.values() if isinstance(raw, dict) else raw
            self._ticker_map = {
                str(r["ticker"]).upper(): str(r["cik_str"])
                for r in rows
                if r.get("ticker") and r.get("cik_str") is not None
            }
        return self._ticker_map

    def cik_for(self, ticker: str) -> str:
        try:
            return self.ticker_map()[ticker.upper()]
        except KeyError:
            raise FetchError(
                f"no CIK for ticker {ticker.upper()!r} in SEC's current mapping. "
                "Delisted or renamed issuers are absent from it; supply --cik directly."
            ) from None

    # -- filing history ----------------------------------------------------
    def _submission_records(self, cik: str) -> list[dict[str, Any]]:
        data = self.client.get_json(SUBMISSIONS_URL.format(cik=cik))
        filings = data.get("filings", {}) or {}
        records = _columnar_rows(filings.get("recent", {}) or {})

        # Older history lives in separate files listed here. Skipping them loses
        # the earliest filings entirely.
        for part in filings.get("files", []) or []:
            name = part.get("name")
            if not name:
                continue
            try:
                extra = self.client.get_json(SUBMISSIONS_PART_URL.format(name=name))
            except FetchError:
                continue
            records.extend(_columnar_rows(extra if isinstance(extra, dict) else {}))
        return records

    def list_filings(
        self,
        ticker: str,
        *,
        form: str = DEFAULT_FORM,
        year: Optional[int] = None,
        cik: Optional[str] = None,
    ) -> list[FilingRef]:
        cik = cik or self.cik_for(ticker)
        wanted = {form.upper(), form_to_fs(form).upper()}

        out: list[FilingRef] = []
        for rec in self._submission_records(cik):
            if str(rec.get("form", "")).upper() not in wanted:
                continue
            filed = _parse_date(rec.get("filingDate"))
            if filed is None or (year is not None and filed.year != year):
                continue
            accession = str(rec.get("accessionNumber") or "")
            if not accession:
                continue
            out.append(
                FilingRef(
                    ticker=ticker.upper(),
                    form=form,
                    filing_date=filed,
                    locator=_document_url(cik, accession, rec.get("primaryDocument"), filed),
                    cik=cik,
                    # Carried so the cache can tell two same-day filings apart.
                    # It is EDGAR's own identifier and it is right here; making
                    # the cache re-derive it from the URL would be a second
                    # place to get it wrong.
                    accession=accession,
                )
            )
        out.sort(key=lambda r: r.filing_date)
        return out

    def read(self, ref: FilingRef) -> bytes:
        return self.client.get(ref.locator)


def _parse_date(value: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _document_url(
    cik: str, accession: str, primary_document: Any, filed: date
) -> str:
    """URL for a filing's content.

    Pre-May-2000 filings go to the complete-submission text file: the accession
    *directory* layout is documented only for later filings, and old submissions
    frequently have no usable `primaryDocument` anyway. That path is therefore the
    primary route for 1994-2000, not a fallback.
    """
    bare_cik = str(int(cik))  # archive paths use the unpadded CIK
    plain = accession.replace("-", "")
    doc = str(primary_document or "").strip()

    if filed < _DIRECTORY_LAYOUT_FROM or not doc:
        return ARCHIVE_FULL_URL.format(cik=bare_cik, accession_dashed=accession)
    return ARCHIVE_DOC_URL.format(cik=bare_cik, accession=plain, document=doc)


def build_source(
    user_agent: Optional[str] = None, rate_per_second: float = 5.0
) -> EdgarSource:
    return EdgarSource(EdgarClient(user_agent=user_agent, rate_per_second=rate_per_second))
