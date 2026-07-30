"""Tests for the EDGAR network source.

**No test here touches the network.** Every case substitutes `_http_get`, the
single seam through which requests leave the process. That is deliberate: a test
suite that hits SEC would be slow, flaky, and would itself consume the request
budget the code exists to protect.

The cases encode SEC's published access rules, so a change that violates one of
them fails here rather than on a user's IP.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from sec_tables import fetch
from sec_tables.fetch import (
    BLOCK_RECOVERY_SECONDS,
    EdgarClient,
    EdgarSource,
    FetchError,
    RateLimited,
    RateLimiter,
    resolve_user_agent,
)

GOOD_UA = "sec-tables test test@example.com"


class FakeHTTP:
    """Scripted responses keyed by URL substring, recording every call."""

    def __init__(self, routes: dict[str, tuple[int, dict, bytes]]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return (404, {}, b"")


def client(routes, **kw) -> EdgarClient:
    c = EdgarClient(user_agent=GOOD_UA, rate_per_second=1000.0, **kw)
    c._http_get = FakeHTTP(routes)  # type: ignore[assignment]
    return c


def ok(payload) -> tuple[int, dict, bytes]:
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return (200, {}, body)


# --------------------------------------------------------------- user agent

class TestUserAgent:
    def test_missing_is_a_hard_error_with_instructions(self, monkeypatch):
        monkeypatch.delenv(fetch.USER_AGENT_ENV, raising=False)
        with pytest.raises(FetchError) as exc:
            resolve_user_agent()
        assert fetch.USER_AGENT_ENV in str(exc.value)
        assert "@" in str(exc.value)  # shows the expected shape

    def test_no_default_is_ever_supplied(self, monkeypatch):
        """A shipped default attributes everyone's traffic to one identity,
        and that identity is what SEC blocks."""
        monkeypatch.delenv(fetch.USER_AGENT_ENV, raising=False)
        with pytest.raises(FetchError):
            EdgarClient()

    def test_bare_email_is_rejected(self, monkeypatch):
        """SEC's stated format is an app/organisation name AND a contact email."""
        monkeypatch.setenv(fetch.USER_AGENT_ENV, "bare@example.com")
        with pytest.raises(FetchError) as exc:
            resolve_user_agent()
        assert "organisation" in str(exc.value) or "application" in str(exc.value)

    def test_missing_email_is_rejected(self, monkeypatch):
        monkeypatch.setenv(fetch.USER_AGENT_ENV, "Acme Research")
        with pytest.raises(FetchError):
            resolve_user_agent()

    def test_valid_form_accepted(self, monkeypatch):
        monkeypatch.setenv(fetch.USER_AGENT_ENV, GOOD_UA)
        assert resolve_user_agent() == GOOD_UA

    def test_explicit_beats_environment(self, monkeypatch):
        monkeypatch.setenv(fetch.USER_AGENT_ENV, "Other other@example.com")
        assert resolve_user_agent(GOOD_UA) == GOOD_UA

    def test_user_agent_header_is_actually_sent(self, monkeypatch):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["ua"] = req.get_header("User-agent")
            raise AssertionError("stop here")

        monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
        c = EdgarClient(user_agent=GOOD_UA)
        with pytest.raises(AssertionError):
            c._http_get("https://www.sec.gov/x")
        assert sent["ua"] == GOOD_UA


# -------------------------------------------------------------- rate limiting

class TestRateLimiter:
    def test_paces_requests(self):
        limiter = RateLimiter(rate_per_second=50.0)
        import time
        start = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        assert time.monotonic() - start >= 0.05  # 4 waits at 1/50s

    def test_rejects_nonsense_rate(self):
        with pytest.raises(ValueError):
            RateLimiter(0)

    def test_default_is_below_the_sec_ceiling(self):
        """SEC's 10/s is a TOTAL per-requester ceiling, not a target."""
        c = EdgarClient(user_agent=GOOD_UA)
        assert c.rate_per_second < 10.0

    def test_one_shared_budget_not_one_per_host(self):
        """Per-host limiters would permit twice the intended rate, since the
        ceiling applies to the requester across SEC.gov."""
        c = client({"sec.gov": ok({})})
        before = c.limiter
        c.get("https://data.sec.gov/a")
        c.get("https://www.sec.gov/b")
        assert c.limiter is before


class TestThrottlingResponses:
    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    def test_all_plausible_throttles_are_retried(self, status, monkeypatch):
        """SEC does not commit to 403 vs 429, so they are treated alike."""
        monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
        c = client({"sec.gov": (status, {}, b"")}, max_retries=2)
        with pytest.raises(RateLimited):
            c.get("https://www.sec.gov/x")
        assert len(c._http_get.calls) == 3  # initial + 2 retries

    def test_retry_after_is_honoured_when_present(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))
        c = client({"sec.gov": (429, {"Retry-After": "7"}, b"")}, max_retries=1)
        with pytest.raises(RateLimited):
            c.get("https://www.sec.gov/x")
        assert 7.0 in slept

    def test_works_without_retry_after(self, monkeypatch):
        """It is frequently absent; backoff must not depend on it."""
        slept: list[float] = []
        monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))
        c = client({"sec.gov": (403, {}, b"")}, max_retries=2)
        with pytest.raises(RateLimited):
            c.get("https://www.sec.gov/x")
        assert slept and all(s > 0 for s in slept)

    def test_recovery_guidance_is_in_the_error(self, monkeypatch):
        monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
        c = client({"sec.gov": (429, {}, b"")}, max_retries=0)
        with pytest.raises(RateLimited) as exc:
            c.get("https://www.sec.gov/x")
        assert str(BLOCK_RECOVERY_SECONDS // 60) in str(exc.value)

    def test_success_after_a_retry(self, monkeypatch):
        monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
        seq = [(429, {}, b""), (200, {}, b"payload")]
        c = EdgarClient(user_agent=GOOD_UA, rate_per_second=1000.0)
        c._http_get = lambda url: seq.pop(0)  # type: ignore[assignment]
        assert c.get("https://www.sec.gov/x") == b"payload"

    def test_404_is_not_retried(self):
        c = client({})
        with pytest.raises(FetchError) as exc:
            c.get("https://www.sec.gov/missing")
        assert "not found" in str(exc.value)


# ------------------------------------------------------------------- source

SUBMISSIONS = {
    "cik": "27904",
    "filings": {
        "recent": {
            "accessionNumber": ["0000027904-23-000015", "0000027904-20-000009"],
            "filingDate": ["2023-04-14", "2020-04-10"],
            "form": ["DEF 14A", "10-K"],
            "primaryDocument": ["dal-20230414.htm", "dal-10k.htm"],
        },
        "files": [{"name": "CIK0000027904-submissions-001.json"}],
    },
}
OLDER = {
    "accessionNumber": ["0000027904-97-000021"],
    "filingDate": ["1997-09-19"],
    "form": ["DEF 14A"],
    "primaryDocument": [""],
}
TICKERS = {"0": {"cik_str": 27904, "ticker": "DAL", "title": "Delta Air Lines"}}


def source(extra_routes=None) -> EdgarSource:
    routes = {
        "company_tickers.json": ok(TICKERS),
        "submissions/CIK0000027904.json": ok(SUBMISSIONS),
        "submissions-001.json": ok(OLDER),
    }
    routes.update(extra_routes or {})
    return EdgarSource(client(routes))


class TestTickerResolution:
    def test_maps_ticker_to_cik(self):
        assert source().cik_for("dal") == "27904"

    def test_map_is_fetched_once(self):
        s = source()
        s.cik_for("DAL"); s.cik_for("DAL")
        assert sum("company_tickers" in u for u in s.client._http_get.calls) == 1

    def test_unknown_ticker_explains_the_historical_limitation(self):
        """SEC's mapping is a CURRENT association; delisted issuers are absent."""
        with pytest.raises(FetchError) as exc:
            source().cik_for("ZZZZ")
        assert "--cik" in str(exc.value)


class TestFilingHistory:
    def test_older_history_files_are_followed(self):
        """EDGAR splits old filings into separate files; ignoring them silently
        loses the earliest filings — exactly this library's differentiated era."""
        refs = source().list_filings("DAL")
        years = {r.filing_date.year for r in refs}
        assert 1997 in years, "pre-2001 filing lost — `files` block not followed"
        assert 2023 in years

    def test_filters_by_form(self):
        refs = source().list_filings("DAL")
        assert all(r.form == "DEF 14A" for r in refs)
        assert len(refs) == 2  # the 10-K is excluded

    def test_filters_by_year(self):
        refs = source().list_filings("DAL", year=1997)
        assert len(refs) == 1 and refs[0].filing_date == date(1997, 9, 19)

    def test_cik_can_bypass_ticker_lookup(self):
        s = source()
        s.list_filings("WHATEVER", cik="27904")
        assert not any("company_tickers" in u for u in s.client._http_get.calls)

    def test_results_are_chronological(self):
        refs = source().list_filings("DAL")
        assert [r.filing_date for r in refs] == sorted(r.filing_date for r in refs)


class TestDocumentURLs:
    def test_modern_filing_uses_the_accession_directory(self):
        ref = [r for r in source().list_filings("DAL") if r.year == 2023][0]
        assert "/Archives/edgar/data/27904/000002790423000015/dal-20230414.htm" in ref.locator

    def test_pre_2000_uses_the_complete_submission_path(self):
        """The accession-directory layout is documented only for post-EDGAR-7.0
        filings, so 1994-2000 must go to {accession-with-dashes}.txt."""
        ref = [r for r in source().list_filings("DAL") if r.year == 1997][0]
        assert ref.locator.endswith("/0000027904-97-000021.txt")

    def test_cik_is_unpadded_in_archive_paths(self):
        ref = source().list_filings("DAL")[0]
        assert "/data/27904/" in ref.locator
        assert "0000027904" not in ref.locator.split("/data/")[1].split("/")[0]

    def test_missing_primary_document_falls_back_to_full_submission(self):
        assert fetch._document_url("27904", "0000027904-10-000001", "", date(2010, 1, 1)).endswith(".txt")


class TestReading:
    def test_read_returns_document_bytes(self):
        s = source({"0000027904-97-000021.txt": (200, {}, b"<TABLE>old filing")})
        ref = s.list_filings("DAL", year=1997)[0]
        assert s.read(ref) == b"<TABLE>old filing"

    def test_source_satisfies_the_protocol(self):
        """Interchangeable with LocalSource, so the CLI needs no special case."""
        s = source()
        assert hasattr(s, "list_filings") and hasattr(s, "read") and s.name == "edgar"


class TestColumnarParsing:
    def test_ragged_arrays_do_not_raise(self):
        rows = fetch._columnar_rows({"a": [1, 2, 3], "b": ["x"]})
        assert rows == [{"a": 1, "b": "x"}]

    def test_empty_block(self):
        assert fetch._columnar_rows({}) == []
