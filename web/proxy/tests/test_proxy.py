"""Proxy behaviour, with SEC mocked at the library's own network seam.

What is being tested here is not "does urllib work" — it is the set of things
that are expensive to get wrong against a real regulator: identifying ourselves
correctly, staying under one shared budget, following the historical pagination,
reaching pre-2000 filings at all, not fetching anything that is not SEC, and
never keeping the visitor's email.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import pytest

from sec_proxy.core import (
    APP_NAME,
    FilingService,
    InvalidInput,
    NotFound,
    Throttled,
    UpstreamFailure,
    assert_fetchable,
    describe,
    filing_id,
    user_agent_for,
    validate_email,
    validate_form,
    validate_ticker,
    validate_year,
)
from sec_tables.fetch import RateLimiter
from sec_tables.sources import FilingRef

EMAIL = "researcher@example.com"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestEmailValidation:
    @pytest.mark.parametrize("good", [
        "a@b.co", "researcher@example.com", "first.last+tag@sub.example.org",
    ])
    def test_accepts_a_real_address(self, good):
        assert validate_email(good) == good

    @pytest.mark.parametrize("bad", [
        "", "   ", None, "notanemail", "@example.com", "user@", "user@nodot",
        "user name@example.com", "a@b.c om", "two@addresses@example.com",
    ])
    def test_rejects_anything_unusable(self, bad):
        with pytest.raises(InvalidInput):
            validate_email(bad)

    def test_user_agent_names_the_application_and_the_contact(self):
        """SEC's stated format is an application name AND a monitored contact.

        A bare email may pass technically but does not follow the published
        form, and `resolve_user_agent` rejects it outright — so this is checked
        against the library's own validator, not just by eye.
        """
        from sec_tables.fetch import resolve_user_agent

        ua = user_agent_for(EMAIL)
        assert APP_NAME in ua and EMAIL in ua
        assert resolve_user_agent(ua) == ua

    def test_a_bare_email_would_be_refused_by_the_library(self):
        from sec_tables.fetch import FetchError, resolve_user_agent

        with pytest.raises(FetchError):
            resolve_user_agent(EMAIL)


class TestOtherValidation:
    def test_ticker_is_normalised_and_bounded(self):
        assert validate_ticker(" dal ") == "DAL"
        with pytest.raises(InvalidInput):
            validate_ticker("../../etc/passwd")
        with pytest.raises(InvalidInput):
            validate_ticker("")

    def test_year_must_be_inside_edgar_coverage(self):
        assert validate_year("1997") == 1997
        for bad in ("1899", "abc", 1850, 3000):
            with pytest.raises(InvalidInput):
                validate_year(bad)

    def test_form_defaults_to_the_proxy_statement(self):
        assert validate_form(None) == "DEF 14A"
        assert validate_form("def 14a") == "DEF 14A"
        with pytest.raises(InvalidInput):
            validate_form("S-1")


# ---------------------------------------------------------------------------
# Filing discovery
# ---------------------------------------------------------------------------


class TestTickerMapping:
    def test_current_mapping_resolves_a_ticker_to_a_cik(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        assert refs and refs[0].cik == "27904"
        assert any("company_tickers.json" in c for c in service.edgar.calls)

    def test_an_unknown_ticker_is_not_found_not_a_crash(self, service):
        with pytest.raises(NotFound) as exc:
            service.list_filings(email=EMAIL, ticker="NOSUCH", year=2023, form="DEF 14A")
        assert "NOSUCH" in str(exc.value)


class TestHistoricalPagination:
    def test_old_filings_come_from_the_files_pages_not_the_recent_block(self, service):
        """1997 exists only in `filings.files[]`.

        A reader that stops at `recent` silently loses everything before roughly
        2020 for an active filer — which is precisely the era this library was
        built for, so it would look like "no filing" rather than a bug.
        """
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert [r.filing_date for r in refs] == [date(1997, 9, 19), date(1997, 10, 24)]
        assert any("submissions-001.json" in c for c in service.edgar.calls)

    def test_a_page_that_fails_does_not_lose_the_recent_block(self, service):
        service.edgar.responses["submissions-001.json"] = (500, {}, b"")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        assert [r.filing_date for r in refs] == [date(2023, 4, 27)]


class TestArchiveRoutes:
    def test_pre_may_2000_uses_the_complete_submission_text_route(self, service):
        """The accession-*directory* layout is documented only for later filings,
        and a 1997 record carries no usable primaryDocument anyway."""
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert refs[0].locator.endswith("/0000027904-97-000012.txt")
        assert describe(refs[0])["route"] == "complete_submission"

    def test_modern_filings_use_the_primary_document(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        assert refs[0].locator.endswith("/dal-20230427.htm")
        assert describe(refs[0])["route"] == "primary_document"

    def test_the_archive_cik_is_unpadded(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert "/edgar/data/27904/" in refs[0].locator


class TestMultipleFilings:
    def test_every_match_is_returned_not_silently_one(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert len(refs) == 2, "a year with an original and an amended proxy must show both"

    def test_ids_are_stable_opaque_and_distinct(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        ids = [filing_id(r) for r in refs]
        assert len(set(ids)) == 2
        assert all(len(i) == 16 and i.isalnum() for i in ids)
        # No URL leaks through the handle the client is given.
        assert not any("sec.gov" in i for i in ids)

    def test_default_selection_is_the_latest(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert service.resolve(refs, None).filing_date == date(1997, 10, 24)

    def test_an_explicit_choice_wins(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        chosen = service.resolve(refs, filing_id(refs[0]))
        assert chosen.filing_date == date(1997, 9, 19)

    def test_an_unknown_id_is_refused_rather_than_guessed(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        with pytest.raises(NotFound):
            service.resolve(refs, "0" * 16)

    def test_no_filings_is_not_found(self, service):
        with pytest.raises(NotFound):
            service.resolve([], None)


# ---------------------------------------------------------------------------
# Caching and pacing
# ---------------------------------------------------------------------------


class TestCaching:
    def test_metadata_is_reused_so_a_second_lookup_costs_nothing(self, service):
        service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        first = len(service.edgar.calls)
        service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert len(service.edgar.calls) == first, "the second listing hit SEC again"
        assert service.metadata.hits >= 1

    def test_filing_bytes_are_cached_on_disk_and_reused(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        data, cached = service.fetch(email=EMAIL, ref=refs[0])
        assert data and cached is False
        again, cached_again = service.fetch(email=EMAIL, ref=refs[0])
        assert again == data and cached_again is True

    def test_the_cache_path_contains_nothing_about_the_visitor(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        service.fetch(email=EMAIL, ref=refs[0])
        paths = [str(p) for p in service.filings.root.rglob("*")]
        assert paths, "nothing was cached"
        assert not any("example.com" in p or "researcher" in p for p in paths)

    def test_a_cached_filing_needs_no_second_visitor_identity(self, service):
        """A different visitor asking for the same filing pays no SEC request."""
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        service.fetch(email=EMAIL, ref=refs[0])
        before = len(service.edgar.calls)
        data, cached = service.fetch(email="someone.else@example.org", ref=refs[0])
        assert cached is True and data
        assert len(service.edgar.calls) == before


class TestRateBudget:
    def test_every_visitor_shares_one_bucket(self, tmp_path):
        """Two visitors are two clients but must not be two budgets.

        `EdgarClient` builds its own limiter per instance, and one client per
        visitor is unavoidable because the User-Agent carries their address. So
        the shared limiter is asserted by identity, not by timing.
        """
        svc = FilingService(cache_dir=tmp_path, rate_per_second=4.0)
        a = svc._source("a@example.com").client
        b = svc._source("b@example.com").client
        assert a.limiter is b.limiter is svc.limiter
        assert a.user_agent != b.user_agent

    def test_the_default_rate_is_below_secs_ceiling(self):
        from sec_proxy.core import DEFAULT_RATE_PER_SECOND

        assert DEFAULT_RATE_PER_SECOND < 10.0

    def test_the_bucket_actually_paces(self):
        limiter = RateLimiter(rate_per_second=20.0)
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        # 5 tokens at 20/s with a burst of 1 cannot complete instantly.
        assert time.monotonic() - started >= 0.15

    def test_concurrent_requests_serialise_through_the_same_limiter(self, tmp_path):
        svc = FilingService(cache_dir=tmp_path, rate_per_second=50.0)
        seen = []

        def work():
            svc.limiter.acquire()
            seen.append(time.monotonic())

        threads = [threading.Thread(target=work) for _ in range(8)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(seen) == 8
        assert max(seen) - started >= 7 / 50.0 * 0.8


# ---------------------------------------------------------------------------
# SEC failure modes
# ---------------------------------------------------------------------------


class TestUpstreamFailures:
    def test_403_is_read_as_throttling_and_retried(self, service):
        """SEC does not commit to 403 vs 429, so both mean the same thing."""
        service.edgar.responses["/Archives/"] = (403, {}, b"blocked")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        with pytest.raises(Throttled):
            service.fetch(email=EMAIL, ref=refs[0])
        archive_calls = [c for c in service.edgar.calls if "/Archives/" in c]
        assert len(archive_calls) > 1, "a throttled request was not retried"

    def test_429_is_throttling(self, service):
        service.edgar.responses["/Archives/"] = (429, {"Retry-After": "0"}, b"slow down")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        with pytest.raises(Throttled):
            service.fetch(email=EMAIL, ref=refs[0])

    def test_retry_after_is_honoured_when_present(self, service):
        service.edgar.responses["/Archives/"] = (429, {"Retry-After": "0.2"}, b"")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        started = time.monotonic()
        with pytest.raises(Throttled):
            service.fetch(email=EMAIL, ref=refs[0])
        assert time.monotonic() - started >= 0.2 * service.max_retries

    def test_500_is_retried_then_reported_as_throttling(self, service):
        service.edgar.responses["/Archives/"] = (500, {}, b"")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        with pytest.raises(Throttled):
            service.fetch(email=EMAIL, ref=refs[0])

    def test_a_transient_500_that_recovers_succeeds(self, service):
        """The retry has to actually help, not merely delay the same failure."""
        state = {"n": 0}
        real = service.edgar._default

        def flaky(url):
            if "/Archives/" in url:
                state["n"] += 1
                if state["n"] == 1:
                    return 500, {}, b""
            return real(url)

        service.edgar._default = flaky
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        data, cached = service.fetch(email=EMAIL, ref=refs[0])
        assert data and state["n"] == 2

    def test_a_404_archive_path_is_not_found_not_throttling(self, service):
        service.edgar.responses["/Archives/"] = (404, {}, b"")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        with pytest.raises(NotFound):
            service.fetch(email=EMAIL, ref=refs[0])

    def test_an_empty_document_is_an_upstream_failure(self, service):
        service.edgar.responses["/Archives/"] = (200, {}, b"")
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")
        with pytest.raises(UpstreamFailure):
            service.fetch(email=EMAIL, ref=refs[0])

    def test_a_ticker_map_failure_does_not_masquerade_as_no_filings(self, service):
        service.edgar.responses["company_tickers.json"] = (500, {}, b"")
        with pytest.raises((Throttled, UpstreamFailure)):
            service.list_filings(email=EMAIL, ticker="DAL", year=2023, form="DEF 14A")


# ---------------------------------------------------------------------------
# SSRF
# ---------------------------------------------------------------------------


class TestNoArbitraryFetching:
    @pytest.mark.parametrize("hostile", [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/Archives/edgar/data/1/x.txt",
        "file:///etc/passwd",
        "https://www.sec.gov.evil.com/Archives/edgar/",
        "https://data.sec.gov/../../etc/passwd",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
    ])
    def test_a_non_archive_url_is_refused(self, hostile):
        with pytest.raises(InvalidInput):
            assert_fetchable(hostile)

    def test_the_real_routes_pass(self):
        assert_fetchable("https://www.sec.gov/Archives/edgar/data/27904/0000027904-97-000012.txt")
        assert_fetchable("https://www.sec.gov/Archives/edgar/data/27904/000002790423000042/dal.htm")

    def test_a_forged_locator_never_reaches_the_network(self, service):
        """The client cannot supply a URL, but if a locator were ever forged the
        allowlist stops it before any request is made."""
        forged = FilingRef("DAL", "DEF 14A", date(1997, 9, 19), "http://127.0.0.1:9/secret")
        before = len(service.edgar.calls)
        with pytest.raises(InvalidInput):
            service.fetch(email=EMAIL, ref=forged)
        assert len(service.edgar.calls) == before


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


class TestEmailIsNotKept:
    def test_the_service_holds_no_reference_to_it_after_a_call(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        service.fetch(email=EMAIL, ref=refs[0])

        blob = json.dumps({
            "metadata": {k: str(v) for k, v in service.metadata._entries.items()},
            "service": {k: str(v) for k, v in vars(service).items() if not callable(v)},
        })
        assert EMAIL not in blob

    def test_it_is_not_written_to_the_cache_directory(self, service):
        refs = service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        service.fetch(email=EMAIL, ref=refs[0])
        for path in service.filings.root.rglob("*"):
            if path.is_file():
                assert EMAIL.encode() not in path.read_bytes()
                assert EMAIL not in str(path)

    def test_it_does_reach_sec_because_sec_requires_it(self, service):
        """The counterpart to every test above: the address is not secret from
        SEC, it is *for* SEC. Only persistence and logging are ruled out."""
        service.list_filings(email=EMAIL, ticker="DAL", year=1997, form="DEF 14A")
        assert any(EMAIL in ua for ua in service.edgar.user_agents)
