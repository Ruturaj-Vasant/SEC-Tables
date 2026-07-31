"""SEC filing acquisition for the browser app.

The browser cannot do this itself — measured, on three independent grounds, and
narrower than this file used to claim (DECISIONS D38, R10):

1. `www.sec.gov` sends no `Access-Control-Allow-Origin` at all, which covers
   every filing document under `/Archives/` and the ticker map under `/files/`.
2. `data.sec.gov` *does* send `*` on submissions and their pagination files —
   but SEC's edge answers a browser's own User-Agent with **403**, and that 403
   carries no CORS header either, so the permissive path is unreachable anyway.
3. A page cannot supply the identification that would fix (2). `fetch()` accepts
   a `User-Agent` header, resolves without error, and sends its own.

So a small server does the fetching and the browser does the extraction.

Everything here is a thin arrangement of `sec_tables.fetch`, `sec_tables.cache`
and `sec_tables.sources`. Nothing re-implements filing discovery, the historical
`files[]` pagination, or the pre-May-2000 archive route — those already exist,
are already tested, and a second copy would be a second thing to get wrong.

The three things this module *does* own:

1. **One request budget for everyone.** `EdgarClient` builds its own
   `RateLimiter` per instance, but a visitor's email has to go on the
   User-Agent, so there is one client per request. Every client is therefore
   given the same process-wide limiter after construction: SEC's ceiling is per
   requester, and this whole server is one requester.
2. **The visitor's email lives for one call chain.** This application does not
   store it, does not put it in a cache key or a filename, does not place it in
   a URL, and does not log it. That is a statement about this code; it is not a
   guarantee about hosting, reverse proxies or anything else in the path.
   Asking for it at all is a product choice, not an SEC rule — see
   `validate_email`.
3. **The browser never names a URL.** It asks for a ticker, a year and a form,
   and refers to a filing by an opaque id that this module minted. A locator is
   only ever produced by `EdgarSource`, and is re-checked against SEC hosts
   before it is fetched.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from sec_tables.cache import FilingCache
from sec_tables.fetch import (
    EdgarClient,
    EdgarSource,
    FetchError,
    RateLimited,
    RateLimiter,
)
from sec_tables.sources import DEFAULT_FORM, FilingRef, SourceError, form_to_fs

APP_NAME = "sec-tables-web"
APP_VERSION = "0.1"

# Deliberately below SEC's ten-per-second ceiling, and shared by every visitor.
DEFAULT_RATE_PER_SECOND = 4.0

# The forms this app offers. An open form field would be a way to make the
# server fetch arbitrary submission types on a stranger's behalf, and none of
# the three supported tables appear outside a proxy statement.
SUPPORTED_FORMS = ("DEF 14A", "DEFA14A", "DEFR14A", "10-K")

SUPPORTED_TABLES = (
    "summary_compensation",
    "director_compensation",
    "beneficial_ownership",
)

# EDGAR's own coverage begins in 1993-1994; a year outside this range is a typo,
# not a query worth spending a request on.
MIN_YEAR = 1993
MAX_YEAR = 2100

TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")

# Practical, not RFC 5322. It rejects the two things that actually matter — a
# missing domain and embedded whitespace — and leaves the rest to SEC.
EMAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\\]+@[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)+$")

# Only these hosts, and only these path prefixes, are ever fetched. Defence in
# depth: the browser cannot supply a URL in the first place.
ALLOWED_HOSTS = {"www.sec.gov", "data.sec.gov"}
ALLOWED_PREFIXES = ("/Archives/edgar/", "/files/", "/submissions/")


class ProxyError(Exception):
    """A failure with a stable machine-readable kind.

    `kind` is what the browser switches on; `message` is what a person reads.
    They are separate because the interface needs to distinguish "SEC is
    throttling us" from "you typed a ticker that does not exist", and an
    English string is the wrong thing to branch on.
    """

    status = 400
    kind = "invalid_input"

    def __init__(self, message: str, *, kind: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        if kind is not None:
            self.kind = kind
        if status is not None:
            self.status = status


class InvalidInput(ProxyError):
    status, kind = 400, "invalid_input"


class NotFound(ProxyError):
    status, kind = 404, "not_found"


class Throttled(ProxyError):
    status, kind = 429, "throttled"


class UpstreamFailure(ProxyError):
    status, kind = 502, "upstream_failure"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_email(raw: Any) -> str:
    """A contact address worth sending, or a refusal explaining why.

    A bare or malformed identity is rejected here rather than passed on: sending
    something obviously fake is worse than sending nothing, because it spends
    the shared request budget under an identity nobody can be reached at.

    To be accurate about whose requirement this is: SEC's fair-access policy
    asks the **requester** — the application or company making automated
    requests — to declare itself with a monitored contact. It does not ask a
    public website to collect every visitor's personal address. Doing that is
    this project's design choice, taken because the alternative is one shipped
    address carrying everyone's traffic, which [D19] rules out.
    """
    email = str(raw or "").strip()
    if not email:
        raise InvalidInput(
            "A contact email is required: this app sends it to SEC as the contact "
            "for your request."
        )
    if len(email) > 254:
        raise InvalidInput("That email address is too long.")
    if not EMAIL_RE.match(email):
        raise InvalidInput(
            f"{email!r} is not a usable contact address. It is sent to SEC as the "
            "contact for this request, so it should be a real, monitored mailbox, "
            "e.g. you@example.com."
        )
    return email


def validate_ticker(raw: Any) -> str:
    ticker = str(raw or "").strip().upper()
    if not ticker:
        raise InvalidInput("A ticker is required.")
    if not TICKER_RE.match(ticker):
        raise InvalidInput(f"{ticker!r} is not a ticker symbol.")
    return ticker


def validate_year(raw: Any) -> int:
    try:
        year = int(str(raw).strip())
    except (TypeError, ValueError):
        raise InvalidInput(f"{raw!r} is not a year.") from None
    if not (MIN_YEAR <= year <= MAX_YEAR):
        raise InvalidInput(f"{year} is outside EDGAR's coverage (from {MIN_YEAR}).")
    return year


def validate_form(raw: Any) -> str:
    form = str(raw or DEFAULT_FORM).strip().upper()
    for supported in SUPPORTED_FORMS:
        if form == supported.upper():
            return supported
    raise InvalidInput(f"{form!r} is not one of the supported forms: {', '.join(SUPPORTED_FORMS)}.")


def user_agent_for(email: str) -> str:
    """The declared identity for one request chain.

    Format follows SEC's stated one — an application name, then a contact —
    which `resolve_user_agent` also enforces, so a bare email cannot get through
    even if validation above were bypassed.
    """
    return f"{APP_NAME}/{APP_VERSION} ({email})"


def assert_fetchable(url: str) -> None:
    """Refuse anything that is not an SEC document URL.

    The browser cannot supply a URL — it names a filing by an id this server
    minted — so reaching this check with a bad value would already mean
    something upstream is wrong. It is here because "the caller cannot do that"
    is an argument, and an allowlist is a control.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise InvalidInput(f"refusing to fetch a non-SEC URL: {url}")
    if not any(parts.path.startswith(p) for p in ALLOWED_PREFIXES):
        raise InvalidInput(f"refusing to fetch outside SEC's archives: {url}")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@dataclass
class MetadataCache:
    """Submission metadata, kept in memory with a TTL.

    Filing *bytes* are immutable once filed and go to `FilingCache` on disk
    forever. Metadata is not immutable — a company files again — so it expires.
    A day is the right order: EDGAR's own submissions bulk file is rebuilt
    nightly.
    """

    ttl_seconds: float = 86_400.0
    _entries: dict[str, tuple[float, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._entries = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or time.monotonic() - entry[0] > self.ttl_seconds:
                self.misses += 1
                return None
            self.hits += 1
            return entry[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def filing_id(ref: FilingRef) -> str:
    """A stable, opaque handle for one filing.

    Derived from the locator so it is stable across listings, and hashed so it
    carries no URL for a client to tamper with. The client sends this back; the
    server re-lists and matches, so the locator it eventually fetches is always
    one it produced itself.
    """
    return hashlib.sha256(ref.locator.encode("utf-8")).hexdigest()[:16]


def describe(ref: FilingRef, *, source_url: bool = False) -> dict[str, Any]:
    out = {
        "id": filing_id(ref),
        "ticker": ref.ticker,
        "cik": ref.cik,
        "form": ref.form,
        "filingDate": ref.filing_date.isoformat(),
        "year": ref.year,
        # Which EDGAR route this filing takes, which is a real difference: the
        # complete-submission text file is the whole submission, headers and
        # exhibits included, while a primary document is just the proxy.
        "route": "complete_submission" if ref.locator.endswith(".txt") else "primary_document",
    }
    if source_url:
        out["sourceUrl"] = ref.locator
    return out


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass
class FilingService:
    """Everything the HTTP layer needs, with no HTTP in it.

    Separated so the tests below exercise SEC behaviour — pagination, the
    pre-2000 route, throttling, cache reuse — without a socket, and the HTTP
    handler stays a thin translation of exceptions into status codes.
    """

    cache_dir: Optional[Path] = None
    rate_per_second: float = DEFAULT_RATE_PER_SECOND
    max_retries: int = 3
    timeout: float = 30.0
    # One bucket for the whole process. Two visitors do not get two budgets.
    limiter: RateLimiter = None  # type: ignore[assignment]
    metadata: MetadataCache = None  # type: ignore[assignment]
    filings: Optional[FilingCache] = None
    # Test seam mirroring `EdgarClient._http_get`; None means real HTTP.
    http_get: Optional[Any] = None

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = RateLimiter(self.rate_per_second)
        if self.metadata is None:
            self.metadata = MetadataCache()
        if self.filings is None and self.cache_dir is not None:
            self.filings = FilingCache(Path(self.cache_dir))
        self.upstream_requests = 0

    # -- client construction ------------------------------------------------

    def _source(self, email: str) -> EdgarSource:
        """A source carrying this visitor's identity and the shared budget."""
        client = EdgarClient(
            user_agent=user_agent_for(email),
            rate_per_second=self.rate_per_second,
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        # Replace the per-instance bucket with the process-wide one. Without
        # this, ten concurrent visitors would each get the full rate and the
        # server would sail past SEC's ceiling while every individual client
        # believed it was behaving.
        client.limiter = self.limiter
        if self.http_get is not None:
            counted = self.http_get

            def _counting_get(url: str):
                self.upstream_requests += 1
                return counted(url)

            client._http_get = _counting_get  # type: ignore[method-assign]
        return EdgarSource(client)

    # -- operations ---------------------------------------------------------

    def list_filings(self, *, email: str, ticker: str, year: int, form: str) -> list[FilingRef]:
        """Every matching filing, newest last. Never silently one of several.

        A company can file an original proxy and then an amended one in the same
        year, and which is wanted is the caller's call, not this server's.
        """
        key = f"{ticker}|{form}|{year}"
        cached = self.metadata.get(key)
        if cached is not None:
            return [_ref_from_dict(d) for d in cached]

        source = self._source(email)
        try:
            refs = source.list_filings(ticker, form=form, year=year)
        except RateLimited as exc:
            raise Throttled(str(exc)) from exc
        except FetchError as exc:
            raise _translate_fetch_error(exc) from exc
        except SourceError as exc:
            raise UpstreamFailure(str(exc)) from exc

        self.metadata.put(key, [_ref_to_dict(r) for r in refs])
        return refs

    def resolve(self, refs: list[FilingRef], filing_id_value: Optional[str]) -> FilingRef:
        """Turn a client-supplied id back into a server-produced locator."""
        if not refs:
            raise NotFound("No filing of that form was found for that ticker and year.")
        if not filing_id_value:
            # `pick_filing`'s default: a second proxy in one year is usually a
            # correction of the first.
            return refs[-1]
        for ref in refs:
            if filing_id(ref) == filing_id_value:
                return ref
        raise NotFound("That filing is no longer in the current listing for this ticker and year.")

    def fetch(self, *, email: str, ref: FilingRef) -> tuple[bytes, bool]:
        """The filing's bytes, and whether they came from cache.

        Cached on disk under (ticker, form, date) — never under anything derived
        from the visitor — because a filing is immutable once filed, so this is
        both polite to SEC and what makes a second look at the same document
        instant.
        """
        suffix = ".txt" if ref.locator.endswith(".txt") else ".html"
        if self.filings is not None:
            hit = self.filings.get(ref)
            if hit:
                return hit, True

        assert_fetchable(ref.locator)
        source = self._source(email)
        try:
            data = source.read(ref)
        except RateLimited as exc:
            raise Throttled(str(exc)) from exc
        except FetchError as exc:
            raise _translate_fetch_error(exc) from exc
        except SourceError as exc:
            raise UpstreamFailure(str(exc)) from exc

        if not data:
            raise UpstreamFailure("SEC returned an empty document.")
        if self.filings is not None:
            self.filings.put(ref, data, suffix=suffix)
        return data, False


def _ref_to_dict(ref: FilingRef) -> dict[str, Any]:
    return {
        "ticker": ref.ticker,
        "form": ref.form,
        "filing_date": ref.filing_date.isoformat(),
        "locator": ref.locator,
        "cik": ref.cik,
        # Round-tripped so a metadata-cache hit keys the filing cache the same
        # way a fresh listing does. Without it the second request for a filing
        # would fall back to deriving the identity from the URL — which happens
        # to work for EDGAR, and would quietly stop working for anything else.
        "accession": ref.accession,
    }


def _ref_from_dict(d: dict[str, Any]) -> FilingRef:
    y, m, day = (int(p) for p in d["filing_date"].split("-"))
    return FilingRef(
        ticker=d["ticker"], form=d["form"], filing_date=date(y, m, day),
        locator=d["locator"], cik=d.get("cik"), accession=d.get("accession"),
    )


def _translate_fetch_error(exc: FetchError) -> ProxyError:
    """Map the library's one error type onto something a UI can branch on.

    `FetchError` covers "no CIK for that ticker", "404", and "the network
    broke". They need different words in front of a person, and the only thing
    distinguishing them is the message, so that is what is read here — narrowly,
    on strings this repository owns.
    """
    text = str(exc)
    if text.startswith("no CIK for ticker"):
        return NotFound(text)
    if text.startswith("not found:"):
        return NotFound("SEC has no document at the archive path for that filing.")
    return UpstreamFailure(text)
