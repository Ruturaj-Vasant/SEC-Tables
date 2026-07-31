"""The HTTP surface, over a real socket, against a fake EDGAR.

A real server rather than a handler unit test, because three of the properties
that matter are properties of the *response*: that a filing comes back as bytes
rather than base64, that its metadata rides on headers, and that an error is a
JSON shape a UI can branch on.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from io import StringIO

import pytest

from sec_proxy.core import FilingService
from sec_proxy.server import ProxyServer

EMAIL = "researcher@example.com"


@pytest.fixture
def live(edgar, tmp_path):
    """A proxy on a real ephemeral port, wired to the fake EDGAR."""
    service = FilingService(cache_dir=tmp_path / "cache", rate_per_second=1000.0, max_retries=1)
    real_source = service._source

    def _source(email: str):
        source = real_source(email)
        edgar.install_on(source.client)
        return source

    service._source = _source  # type: ignore[method-assign]
    server = ProxyServer(("127.0.0.1", 0), service, log_requests=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", service, edgar, server
    server.shutdown()
    server.server_close()


def post(base: str, path: str, payload: dict):
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class TestRoutes:
    def test_health_reports_configuration_without_touching_sec(self, live):
        base, _, edgar, _ = live
        with urllib.request.urlopen(f"{base}/api/health", timeout=10) as response:
            body = json.loads(response.read())
        # The rate here is the fixture's (raised so tests do not sleep); that the
        # *default* sits below SEC's ceiling is asserted in test_proxy.py.
        assert body["ok"] and body["ratePerSecond"] > 0
        assert "DEF 14A" in body["forms"]
        assert set(body["tables"]) == {
            "summary_compensation", "director_compensation", "beneficial_ownership",
        }
        assert edgar.calls == []

    def test_filings_lists_every_match(self, live):
        base, _, _, _ = live
        status, _, body = post(base, "/api/filings", {
            "email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A",
        })
        payload = json.loads(body)
        assert status == 200
        assert [f["filingDate"] for f in payload["filings"]] == ["1997-09-19", "1997-10-24"]
        assert payload["defaultId"] == payload["filings"][-1]["id"]
        assert payload["filings"][0]["route"] == "complete_submission"
        # The listing hands out no URLs; a client cannot ask for one.
        assert not any("sourceUrl" in f for f in payload["filings"])

    def test_filing_returns_bytes_with_metadata_on_headers(self, live):
        base, _, _, _ = live
        _, _, listed = post(base, "/api/filings", {
            "email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A",
        })
        chosen = json.loads(listed)["filings"][0]["id"]

        status, headers, body = post(base, "/api/filing", {
            "email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A", "filingId": chosen,
        })
        assert status == 200
        assert headers["Content-Type"] == "application/octet-stream"
        assert b"SUMMARY COMPENSATION TABLE" in body, "the raw filing, not base64"
        meta = json.loads(headers["X-Filing-Meta"])
        assert meta["filingDate"] == "1997-09-19"
        assert meta["ticker"] == "DAL" and meta["cik"] == "27904"
        assert meta["sourceUrl"].startswith("https://www.sec.gov/Archives/")
        assert headers["X-Filing-Cache"] == "miss"

    def test_a_second_fetch_is_served_from_cache(self, live):
        base, _, _, _ = live
        payload = {"email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A"}
        post(base, "/api/filing", payload)
        _, headers, _ = post(base, "/api/filing", payload)
        assert headers["X-Filing-Cache"] == "hit"

    def test_an_unknown_route_is_a_structured_404(self, live):
        base, _, _, _ = live
        status, _, body = post(base, "/api/nope", {})
        assert status == 404
        assert json.loads(body)["error"]["kind"] == "not_found"


class TestStructuredErrors:
    @pytest.mark.parametrize("payload,kind,status", [
        ({"ticker": "DAL", "year": 1997}, "invalid_input", 400),
        ({"email": "nope", "ticker": "DAL", "year": 1997}, "invalid_input", 400),
        ({"email": EMAIL, "ticker": "", "year": 1997}, "invalid_input", 400),
        ({"email": EMAIL, "ticker": "DAL", "year": 1750}, "invalid_input", 400),
        ({"email": EMAIL, "ticker": "DAL", "year": 1997, "form": "S-1"}, "invalid_input", 400),
        ({"email": EMAIL, "ticker": "NOSUCH", "year": 1997}, "not_found", 404),
    ])
    def test_bad_input_is_named_not_just_rejected(self, live, payload, kind, status):
        base, _, _, _ = live
        got_status, _, body = post(base, "/api/filings", payload)
        assert got_status == status
        assert json.loads(body)["error"]["kind"] == kind
        assert json.loads(body)["error"]["message"]

    def test_throttling_is_its_own_kind(self, live):
        base, _, edgar, _ = live
        edgar.responses["/Archives/"] = (429, {"Retry-After": "0"}, b"")
        status, _, body = post(base, "/api/filing", {
            "email": EMAIL, "ticker": "DAL", "year": 2023, "form": "DEF 14A",
        })
        assert status == 429
        assert json.loads(body)["error"]["kind"] == "throttled"

    def test_upstream_failure_is_distinguishable_from_bad_input(self, live):
        base, _, edgar, _ = live
        edgar.responses["/Archives/"] = (200, {}, b"")
        status, _, body = post(base, "/api/filing", {
            "email": EMAIL, "ticker": "DAL", "year": 2023, "form": "DEF 14A",
        })
        assert status == 502
        assert json.loads(body)["error"]["kind"] == "upstream_failure"

    def test_a_malformed_body_does_not_reach_sec(self, live):
        base, _, edgar, _ = live
        request = urllib.request.Request(
            f"{base}/api/filings", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert json.loads(exc.read())["error"]["kind"] == "invalid_input"
        assert edgar.calls == []

    def test_a_malformed_filing_id_is_refused(self, live):
        base, _, _, _ = live
        status, _, body = post(base, "/api/filing", {
            "email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A",
            "filingId": "../../etc/passwd",
        })
        assert status == 400
        assert json.loads(body)["error"]["kind"] == "invalid_input"


class TestEmailNeverLeaves:
    def test_it_is_not_in_any_request_line_or_log(self, live, capsys):
        """Every call is a POST, so the address is in a body — which no access
        log records — and the handler's logger is overridden to drop the query
        string as well, so a future GET parameter cannot leak into one either."""
        base, service, _, server = live
        server.log_requests = True
        post(base, "/api/filings", {"email": EMAIL, "ticker": "DAL", "year": 1997})
        captured = capsys.readouterr()
        assert EMAIL not in captured.err
        assert EMAIL not in captured.out
        assert "/api/filings" in captured.err

    def test_it_never_appears_in_a_url(self, live):
        base, _, edgar, _ = live
        post(base, "/api/filing", {"email": EMAIL, "ticker": "DAL", "year": 1997})
        assert not any(EMAIL in url for url in edgar.calls)
        assert not any("@" in url.split("://", 1)[1].split("/")[0] for url in edgar.calls)

    def test_an_error_response_does_not_echo_it_back(self, live):
        base, _, _, _ = live
        _, _, body = post(base, "/api/filings", {
            "email": EMAIL, "ticker": "ZZZZZ", "year": 1997,
        })
        assert EMAIL not in body.decode()
