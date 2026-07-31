"""Fake EDGAR, built to the shapes SEC actually returns.

Every test mocks `EdgarClient._http_get` — the library documents it as the
"single seam for the network" — so no test in this suite opens a socket to SEC,
and the retry, pacing and pagination logic above it all runs for real.

The fixtures deliberately reproduce two things that are easy to get wrong and
expensive to get wrong in production:

* submissions are **columnar** (parallel arrays), not a list of records, and
  older history lives in separate files named by `filings.files[]`;
* a 1997 filing has no usable `primaryDocument`, so it can only be reached
  through the complete-submission `.txt` route.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The proxy imports sec_tables; use the working tree, not whatever happens to be
# installed. An installed copy has already been older than the checkout once.
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "web" / "proxy"))


TICKERS_JSON = {
    "0": {"cik_str": 27904, "ticker": "DAL", "title": "DELTA AIR LINES, INC."},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}

# Recent block: modern filings, with a primary document.
DAL_RECENT = {
    "form": ["DEF 14A", "10-K", "DEF 14A"],
    "filingDate": ["2023-04-27", "2023-02-13", "2022-04-28"],
    "accessionNumber": ["0000027904-23-000042", "0000027904-23-000010", "0000027904-22-000031"],
    "primaryDocument": ["dal-20230427.htm", "dal-20221231.htm", "dal-20220428.htm"],
}

# Older history, reachable only by following `filings.files[]`. Two DEF 14A in
# 1997: the original and an amended one, which is the case that must not be
# silently collapsed to one.
DAL_OLD = {
    "form": ["DEF 14A", "DEF 14A", "DEF 14A"],
    "filingDate": ["1997-09-19", "1997-10-24", "1994-09-13"],
    "accessionNumber": ["0000027904-97-000012", "0000027904-97-000019", "0000027904-94-000021"],
    "primaryDocument": ["", "", ""],
}

FILING_BYTES = b"<TABLE>\nSUMMARY COMPENSATION TABLE\nRonald W. Allen  1997  562,500\n</TABLE>\n"


class FakeEdgar:
    """Records every URL asked for, answers the ones SEC would.

    `responses` maps a URL substring to `(status, headers, body)`, so a test can
    override any single route — a 429 on the archive fetch, a 500 on the ticker
    map — without rebuilding the rest of EDGAR.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.user_agents: list[str] = []
        self.responses: dict[str, tuple[int, dict, bytes]] = {}

    def install_on(self, client) -> None:
        original_ua = client.user_agent

        def _http_get(url: str):
            self.calls.append(url)
            self.user_agents.append(original_ua)
            for fragment, response in self.responses.items():
                if fragment in url:
                    return response
            return self._default(url)

        client._http_get = _http_get

    def _default(self, url: str):
        ok = {"Content-Type": "application/json"}
        if "company_tickers.json" in url:
            return 200, ok, json.dumps(TICKERS_JSON).encode()
        # Checked before the main submissions route: the historical pages live
        # at `submissions/CIK…-submissions-001.json`, so a naive "submissions/CIK"
        # test matches both and the old filings are never served.
        if "-submissions-" in url:
            return 200, ok, json.dumps(DAL_OLD).encode()
        if "submissions/CIK" in url:
            return 200, ok, json.dumps({
                "cik": "27904",
                "filings": {
                    "recent": DAL_RECENT,
                    "files": [{"name": "CIK0000027904-submissions-001.json"}],
                },
            }).encode()
        if "submissions/CIK0000027904-submissions-001.json" in url or "-submissions-001.json" in url:
            return 200, ok, json.dumps(DAL_OLD).encode()
        if "/Archives/" in url:
            return 200, {"Content-Type": "text/plain"}, FILING_BYTES
        return 404, {}, b"not found"


@pytest.fixture
def edgar() -> FakeEdgar:
    return FakeEdgar()


@pytest.fixture
def service(edgar, tmp_path):
    """A service whose every client is wired to the fake."""
    from sec_proxy.core import FilingService

    svc = FilingService(cache_dir=tmp_path / "cache", rate_per_second=1000.0, max_retries=2)
    real_source = svc._source

    def _source(email: str):
        source = real_source(email)
        edgar.install_on(source.client)
        inner = source.client._http_get

        def counted(url: str):
            svc.upstream_requests += 1
            return inner(url)

        source.client._http_get = counted
        return source

    svc._source = _source  # type: ignore[method-assign]
    svc.edgar = edgar  # type: ignore[attr-defined]
    return svc
