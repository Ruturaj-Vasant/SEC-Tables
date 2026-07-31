"""The real proxy, with a fake EDGAR behind it.

Runs `sec_proxy.server` unmodified — the same routing, validation, caching,
rate limiting and error mapping the live deployment uses — with only
`EdgarClient._http_get`, the library's documented network seam, replaced.

That is what makes the browser suite both real and deterministic: the app talks
to a genuine proxy over genuine HTTP, and the bytes it gets back are the
committed fixtures whose values a person verified by eye. A test that pointed at
live SEC would be testing SEC's availability, and would change its answer the
day a rendition changed.

The live path is exercised separately and on purpose — see the opt-in smoke test.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
REPO = WEB.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(WEB / "proxy"))

from sec_proxy.core import FilingService  # noqa: E402
from sec_proxy.limits import AbuseGuard, SlidingWindow  # noqa: E402
from sec_proxy.server import ProxyServer  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
ASSETS = WEB / "test" / "assets"

TICKERS = {
    "0": {"cik_str": 27904, "ticker": "DAL", "title": "DELTA AIR LINES, INC."},
    "1": {"cik_str": 1017480, "ticker": "AZZ", "title": "AZZ INC"},
    "2": {"cik_str": 1227654, "ticker": "CMP", "title": "COMPASS MINERALS INTERNATIONAL INC"},
    # A filing whose extraction genuinely raises review flags, so the interface
    # can be shown separating them from provenance on one real result.
    "4": {"cik_str": 874499, "ticker": "ABCP", "title": "AMBASE CORP"},
}

# (cik, form, filingDate, accession, primaryDocument, fixture served for it)
FILINGS = [
    # DAL 1997 is the acceptance case, and EDGAR holds exactly one DEF 14A for
    # it — so this holds one too. A fake that invented a second filing would
    # make the default selection differ from the live system, which is the one
    # difference a fixture-backed suite must not introduce.
    ("27904", "DEF 14A", "1997-09-19", "0000027904-97-000012", "", "dal_1997_sct.txt"),
    # 1994 carries two, so the filing chooser has something real to choose from.
    ("27904", "DEF 14A", "1994-09-13", "0000027904-94-000021", "", "dal_1994_sct.txt"),
    ("27904", "DEF 14A", "1994-11-02", "0000027904-94-000030", "", "dal_1994_sct.txt"),
    ("1017480", "DEF 14A", "2019-05-28", "0001017480-19-000045", "azz2019.htm", "azz_2019_ownership.html"),
    ("1227654", "DEF 14A", "2024-01-29", "0001227654-24-000009", "cmp2024.htm", "cmp_2024_director_comp.html"),
    ("874499", "DEF 14A", "1997-02-27", "0000874499-97-000003", "", "@assets/abcp_1997_review_flags.txt"),
]

# A filing whose extraction never returns: the malformed-colspan case the bridge
# found. Served for ticker HANG so the browser suite can prove the extraction
# timeout terminates and rebuilds the worker.
HANG_DOC = (
    b'<html><body><table>'
    b'<tr><th colspan="2000000000">Name and Principal Position</th>'
    b'<th>Salary</th><th>Bonus</th><th>Year</th></tr>'
    b'<tr><td>A Person</td><td>1</td><td>2</td><td>2020</td></tr>'
    b"</table></body></html>"
)
TICKERS["3"] = {"cik_str": 999999, "ticker": "HANG", "title": "PATHOLOGICAL FILING CO"}
FILINGS.append(("999999", "DEF 14A", "2020-04-01", "0000999999-20-000001", "hang.htm", None))


def _columnar(records: list[dict]) -> dict:
    """EDGAR returns parallel arrays, not a list of records."""
    keys = ["form", "filingDate", "accessionNumber", "primaryDocument"]
    return {k: [r[k] for r in records] for k in keys}


def _records_for(cik: str, recent: bool) -> dict:
    rows = [
        {
            "form": form,
            "filingDate": filed,
            "accessionNumber": accession,
            "primaryDocument": document,
        }
        for c, form, filed, accession, document, _ in FILINGS
        if c == cik and (filed >= "2001-01-01") == recent
    ]
    return _columnar(rows) if rows else {}


def fake_http_get(url: str):
    ok = {"Content-Type": "application/json"}

    if "company_tickers.json" in url:
        return 200, ok, json.dumps(TICKERS).encode()

    # Historical pages are checked first: they live under a path that also
    # contains "submissions/CIK".
    if "-submissions-" in url:
        cik = url.split("CIK")[1].split("-")[0].lstrip("0")
        return 200, ok, json.dumps(_records_for(cik, recent=False)).encode()

    if "submissions/CIK" in url:
        cik = url.split("CIK")[1].split(".json")[0].lstrip("0")
        return 200, ok, json.dumps({
            "cik": cik,
            "filings": {
                "recent": _records_for(cik, recent=True),
                "files": [{"name": f"CIK{int(cik):010d}-submissions-001.json"}],
            },
        }).encode()

    if "/Archives/" in url:
        for _, _, _, accession, document, fixture in FILINGS:
            plain = accession.replace("-", "")
            if accession in url or (document and plain in url):
                if fixture is None:
                    return 200, {"Content-Type": "text/html"}, HANG_DOC
                root = ASSETS if fixture.startswith("@assets/") else FIXTURES
                name = fixture.split("/", 1)[1] if fixture.startswith("@assets/") else fixture
                return 200, {"Content-Type": "text/plain"}, (root / name).read_bytes()
        return 404, {}, b"not found"

    return 404, {}, b"unmocked: " + url.encode()


def main() -> int:
    port = int(os.environ.get("PROXY_PORT", 5310))
    cache = Path(os.environ.get("FAKE_SEC_CACHE", "/tmp/sec-tables-fake-cache"))
    service = FilingService(cache_dir=cache, rate_per_second=1000.0, max_retries=1)

    real_source = service._source

    def _source(email: str):
        source = real_source(email)
        source.client._http_get = fake_http_get
        return source

    service._source = _source
    # The CORS policy is the shipped one — that is the whole point of running the
    # real server here, and the browser suite asserts against it. The per-client
    # limit is not: the entire suite runs from one address in a few minutes and
    # would otherwise throttle itself, which would look like an application bug.
    # Raising it here rather than in the policy keeps the deployed default honest.
    guard = AbuseGuard(window=SlidingWindow(limit=100_000, window_seconds=1.0))
    server = ProxyServer(("127.0.0.1", port), service, log_requests=False, guard=guard)
    sys.stderr.write(f"fake-SEC proxy on http://127.0.0.1:{port} (fixtures from {FIXTURES})\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
