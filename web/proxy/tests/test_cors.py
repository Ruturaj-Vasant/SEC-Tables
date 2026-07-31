"""Cross-origin behaviour, over a real socket.

These are response-level properties, so they are asserted against a real server
rather than a policy object in isolation: a correct `CorsPolicy` that the handler
forgets to call on the binary path would pass every unit test and break the only
request that carries a filing.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from sec_proxy.core import FilingService
from sec_proxy.cors import PAGES_ORIGIN, CorsPolicy, parse_origins, policy_from_env
from sec_proxy.server import ProxyServer, resolve_host, resolve_port

EMAIL = "researcher@example.com"
FOREIGN = "https://evil.example.com"
LOCAL = "http://127.0.0.1:5199"


@pytest.fixture
def live(edgar, tmp_path):
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


def call(base: str, path: str, *, method="POST", payload=None, origin=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json"} if data else {}
    if origin is not None:
        hdrs["Origin"] = origin
    hdrs.update(headers or {})
    request = urllib.request.Request(f"{base}{path}", data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


LIST = {"email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A"}


class TestAllowedOrigins:
    def test_the_production_pages_origin_is_allowed(self, live):
        base, _, _, _ = live
        status, headers, _ = call(base, "/api/filings", payload=LIST, origin=PAGES_ORIGIN)
        assert status == 200
        assert headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN

    def test_a_localhost_dev_origin_is_allowed_on_any_port(self, live):
        base, _, _, _ = live
        for origin in (LOCAL, "http://localhost:3000", "http://127.0.0.1:8080"):
            status, headers, _ = call(base, "/api/filings", payload=LIST, origin=origin)
            assert status == 200, origin
            assert headers["Access-Control-Allow-Origin"] == origin

    def test_an_unapproved_origin_is_refused_before_any_sec_request(self, live):
        """The refusal has to come first, not just the missing header.

        A browser discards a response with no `Access-Control-Allow-Origin`
        regardless — so serving one would spend a request from the shared SEC
        budget to produce bytes nobody can read.
        """
        base, _, edgar, _ = live
        status, headers, body = call(base, "/api/filings", payload=LIST, origin=FOREIGN)
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers
        assert json.loads(body)["error"]["kind"] == "origin_not_allowed"
        assert edgar.calls == [], "a refused origin must not reach SEC"

    def test_a_request_with_no_origin_is_served(self, live):
        """`curl` and every other non-browser client. CORS protects browsers."""
        base, _, _, _ = live
        status, headers, _ = call(base, "/api/filings", payload=LIST)
        assert status == 200
        assert "Access-Control-Allow-Origin" not in headers

    def test_never_a_wildcard(self, live):
        base, _, _, _ = live
        for origin in (PAGES_ORIGIN, LOCAL, None):
            _, headers, _ = call(base, "/api/filings", payload=LIST, origin=origin)
            assert headers.get("Access-Control-Allow-Origin") != "*"


class TestPreflight:
    def test_options_is_answered_for_an_allowed_origin(self, live):
        base, _, _, _ = live
        status, headers, _ = call(
            base, "/api/filing", method="OPTIONS", origin=PAGES_ORIGIN,
            headers={"Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"},
        )
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN
        assert "POST" in headers["Access-Control-Allow-Methods"]
        # Without this the browser never sends the real request: a JSON
        # Content-Type is not CORS-safelisted.
        assert "Content-Type" in headers["Access-Control-Allow-Headers"]
        assert int(headers["Access-Control-Max-Age"]) > 0

    def test_options_is_refused_for_an_unapproved_origin(self, live):
        base, _, _, _ = live
        status, headers, _ = call(
            base, "/api/filing", method="OPTIONS", origin=FOREIGN,
            headers={"Access-Control-Request-Method": "POST"},
        )
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers

    def test_preflight_touches_neither_sec_nor_the_rate_limit(self, live):
        base, _, edgar, _ = live
        for _ in range(5):
            call(base, "/api/filing", method="OPTIONS", origin=PAGES_ORIGIN)
        assert edgar.calls == []


class TestVaryAndExposedHeaders:
    def test_vary_origin_is_present_whether_allowed_or_not(self, live):
        """Both answers depend on `Origin`, so both must be marked as varying.

        A shared cache that stores the allowed response and replays it to a
        different origin breaks the app for the second visitor only, which is
        about as hard to reproduce as a bug gets.
        """
        base, _, _, _ = live
        for origin in (PAGES_ORIGIN, FOREIGN, None):
            _, headers, _ = call(base, "/api/filings", payload=LIST, origin=origin)
            assert "Origin" in headers["Vary"], origin

    def test_the_filing_response_exposes_its_metadata_headers(self, live):
        """The binary path, which is the one the app actually depends on.

        Cross-origin, `fetch()` may read seven response headers. `X-Filing-Meta`
        is not among them, so without this the bytes arrive and
        `parseFilingMeta` throws on null.
        """
        base, _, _, _ = live
        status, headers, body = call(
            base, "/api/filing", payload=LIST, origin=PAGES_ORIGIN,
        )
        assert status == 200
        assert b"SUMMARY COMPENSATION TABLE" in body
        assert headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN
        exposed = headers["Access-Control-Expose-Headers"]
        assert "X-Filing-Meta" in exposed
        assert "X-Filing-Cache" in exposed
        assert json.loads(headers["X-Filing-Meta"])["filingDate"] == "1997-10-24"

    def test_an_error_response_is_also_readable_cross_origin(self, live):
        """A 429 the browser cannot read becomes "Failed to fetch".

        Which is the least useful thing the app can say about the two failures
        most worth explaining — throttling and an upstream error.
        """
        base, _, edgar, _ = live
        edgar.responses["/Archives/"] = (429, {"Retry-After": "0"}, b"")
        status, headers, body = call(
            base, "/api/filing",
            payload={"email": EMAIL, "ticker": "DAL", "year": 2023, "form": "DEF 14A"},
            origin=PAGES_ORIGIN,
        )
        assert status == 429
        assert headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN
        assert json.loads(body)["error"]["kind"] == "throttled"

    def test_health_is_readable_cross_origin(self, live):
        base, _, _, _ = live
        status, headers, body = call(base, "/api/health", method="GET", origin=PAGES_ORIGIN)
        assert status == 200
        assert headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN
        assert json.loads(body)["ok"] is True


class TestEmailStillDoesNotLeakCrossOrigin:
    """The privacy invariants, re-checked on the path that is new.

    The existing suite proves them same-origin. CORS added headers to every
    response and a 403 branch that runs before validation, so this asserts the
    address is in none of them.
    """

    def test_no_response_header_carries_the_address(self, live):
        base, _, _, _ = live
        for origin in (PAGES_ORIGIN, FOREIGN):
            for path in ("/api/filings", "/api/filing"):
                _, headers, body = call(base, path, payload=LIST, origin=origin)
                blob = json.dumps(headers)
                assert EMAIL not in blob, (path, origin)
                assert EMAIL not in body.decode("utf-8", "replace")

    def test_a_refused_origin_is_told_nothing_about_the_request(self, live):
        base, _, _, _ = live
        _, _, body = call(base, "/api/filings", payload=LIST, origin=FOREIGN)
        text = body.decode()
        assert EMAIL not in text
        assert "DAL" not in text


class TestPolicyUnit:
    def test_configuration_replaces_the_default_rather_than_extending_it(self):
        policy = policy_from_env({"SEC_TABLES_ALLOWED_ORIGINS": "https://fork.example.com"})
        assert policy.allows("https://fork.example.com")
        assert not policy.allows(PAGES_ORIGIN), "a fork must not inherit this project's origin"

    def test_an_empty_configuration_falls_back_to_the_shipped_origin(self):
        assert policy_from_env({}).allows(PAGES_ORIGIN)

    def test_a_trailing_slash_is_not_an_origin(self):
        """Browsers send `https://host`, never `https://host/`."""
        assert parse_origins("https://a.example.com/, https://b.example.com") == frozenset(
            {"https://a.example.com", "https://b.example.com"}
        )

    def test_loopback_can_be_turned_off(self):
        policy = policy_from_env({"SEC_TABLES_ALLOW_LOOPBACK": "0"})
        assert not policy.allows(LOCAL)
        assert policy.allows(PAGES_ORIGIN)

    def test_a_missing_origin_is_not_an_allowed_origin(self):
        assert not CorsPolicy().allows(None)
        assert not CorsPolicy().allows("")

    def test_credentials_are_never_allowed(self):
        """Nothing here uses a cookie, and saying so is what keeps it that way."""
        headers = CorsPolicy().headers(PAGES_ORIGIN)
        assert "Access-Control-Allow-Credentials" not in headers


class TestPlatformContract:
    def test_port_comes_from_the_platform_first(self):
        assert resolve_port({"PORT": "10000", "PROXY_PORT": "5310"}) == 10000
        assert resolve_port({"PROXY_PORT": "5310"}) == 5310
        assert resolve_port({}) == 5310
        assert resolve_port({"PORT": "not-a-port"}) == 5310

    def test_a_platform_assigned_port_means_binding_every_interface(self):
        """A container that binds 127.0.0.1 passes its own health check and is
        unreachable from outside it."""
        assert resolve_host({"PORT": "10000"}) == "0.0.0.0"
        assert resolve_host({}) == "127.0.0.1"
        assert resolve_host({"PORT": "10000", "HOST": "127.0.0.1"}) == "127.0.0.1"

    def test_health_does_not_claim_the_cache_is_persistent(self, live):
        base, _, _, _ = live
        _, _, body = call(base, "/api/health", method="GET")
        assert json.loads(body)["cache"]["filingsPersistent"] is False
