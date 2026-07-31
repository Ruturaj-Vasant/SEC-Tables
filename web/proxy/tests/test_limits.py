"""Abuse protection: what it does, and — equally asserted — what it does not.

Half of these tests exist to pin the limitations rather than the guarantees. A
control whose boundaries are only described in a comment drifts into being
described as stronger than it is, which is the failure mode DECISIONS R8 already
caught once in this project.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from sec_proxy.core import FilingService
from sec_proxy.cors import PAGES_ORIGIN
from sec_proxy.limits import AbuseGuard, InFlight, SlidingWindow, guard_from_env
from sec_proxy.server import ProxyServer

EMAIL = "researcher@example.com"
LIST = {"email": EMAIL, "ticker": "DAL", "year": 1997, "form": "DEF 14A"}


@pytest.fixture
def limited(edgar, tmp_path):
    """A proxy that allows three requests, so the limit is reachable in a test."""
    service = FilingService(cache_dir=tmp_path / "cache", rate_per_second=1000.0, max_retries=1)
    real_source = service._source

    def _source(email: str):
        source = real_source(email)
        edgar.install_on(source.client)
        return source

    service._source = _source  # type: ignore[method-assign]
    guard = AbuseGuard(window=SlidingWindow(limit=3, window_seconds=60.0))
    server = ProxyServer(("127.0.0.1", 0), service, log_requests=False, guard=guard)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", service, edgar, guard
    server.shutdown()
    server.server_close()


def post(base, path, payload, headers=None):
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class TestPerClientLimit:
    def test_over_the_limit_is_a_throttle_the_app_already_understands(self, limited):
        base, _, _, _ = limited
        for _ in range(3):
            assert post(base, "/api/filings", LIST)[0] == 200
        status, headers, body = post(base, "/api/filings", LIST)
        assert status == 429
        # `throttled` rather than a new kind: the app already has copy for it,
        # and from a visitor's point of view "this server is rate-limiting you"
        # and "SEC is rate-limiting this server" call for the same action.
        assert json.loads(body)["error"]["kind"] == "throttled"
        assert int(headers["Retry-After"]) >= 1

    def test_a_refused_request_does_not_reach_sec(self, limited):
        base, _, edgar, _ = limited
        for _ in range(3):
            post(base, "/api/filings", LIST)
        before = len(edgar.calls)
        post(base, "/api/filings", LIST)
        assert len(edgar.calls) == before

    def test_being_over_the_limit_does_not_extend_the_limit(self, limited):
        """Counting refusals would let a client hold itself out by knocking."""
        base, _, _, guard = limited
        for _ in range(3):
            post(base, "/api/filings", LIST)
        first = post(base, "/api/filings", LIST)[1]["Retry-After"]
        for _ in range(5):
            post(base, "/api/filings", LIST)
        assert int(post(base, "/api/filings", LIST)[1]["Retry-After"]) <= int(first)

    def test_health_is_never_rate_limited(self, limited):
        """It is the platform's health check and the frontend's warm-up ping.

        Limiting it means a busy proxy reports itself unhealthy and is restarted.
        """
        base, _, _, _ = limited
        for _ in range(20):
            with urllib.request.urlopen(f"{base}/api/health", timeout=10) as response:
                assert response.status == 200

    def test_the_cors_headers_survive_a_throttle(self, limited):
        base, _, _, _ = limited
        for _ in range(3):
            post(base, "/api/filings", LIST, {"Origin": PAGES_ORIGIN})
        status, headers, _ = post(base, "/api/filings", LIST, {"Origin": PAGES_ORIGIN})
        assert status == 429
        assert headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN


class TestClientIdentification:
    def test_forwarded_for_is_ignored_unless_it_is_trusted(self):
        """Untrusted, the header is attacker-controlled and must not key anything."""
        guard = AbuseGuard(trust_forwarded=False)
        assert guard.client_key("10.0.0.1", "1.2.3.4") == "10.0.0.1"

    def test_forwarded_for_is_the_first_entry_when_trusted(self):
        """Each hop appends, so the client is first — and only the hops after it
        being trusted makes that entry meaningful."""
        guard = AbuseGuard(trust_forwarded=True)
        assert guard.client_key("10.0.0.1", "1.2.3.4, 10.0.0.9") == "1.2.3.4"

    def test_a_trusted_but_absent_header_falls_back_to_the_socket(self):
        guard = AbuseGuard(trust_forwarded=True)
        assert guard.client_key("10.0.0.1", None) == "10.0.0.1"

    def test_a_spoofed_header_defeats_the_limit_when_trust_is_on(self, limited):
        """Asserted, not merely documented.

        This is the cost of `SEC_TABLES_TRUST_FORWARDED`, and it is only correct
        to set it behind a proxy that overwrites the header. A test that proves
        the limit holds under every configuration would be proving something
        false.
        """
        base, _, _, guard = limited
        guard.trust_forwarded = True
        for i in range(12):
            status, _, _ = post(base, "/api/filings", LIST, {"X-Forwarded-For": f"10.1.1.{i}"})
            assert status == 200, "each claimed address gets its own budget"


class TestInFlightCap:
    def test_refusing_is_immediate_rather_than_queueing(self):
        cap = InFlight(limit=2)
        assert cap.acquire() and cap.acquire()
        assert cap.acquire() is False
        cap.release()
        assert cap.acquire()

    def test_release_never_goes_negative(self):
        cap = InFlight(limit=1)
        cap.release()
        cap.release()
        assert cap.acquire()


class TestWindowUnit:
    def test_the_window_slides(self):
        window = SlidingWindow(limit=2, window_seconds=10.0)
        assert window.check("a", now=100.0)[0]
        assert window.check("a", now=101.0)[0]
        assert window.check("a", now=102.0)[0] is False
        # 111 is more than ten seconds after the first hit, so it has expired.
        assert window.check("a", now=111.5)[0]

    def test_clients_do_not_share_a_budget(self):
        window = SlidingWindow(limit=1, window_seconds=10.0)
        assert window.check("a", now=1.0)[0]
        assert window.check("b", now=1.0)[0]
        assert window.check("a", now=1.0)[0] is False

    def test_remaining_counts_down(self):
        window = SlidingWindow(limit=3, window_seconds=10.0)
        assert [window.check("a", now=1.0)[1] for _ in range(3)] == [2, 1, 0]

    def test_idle_clients_are_swept_rather_than_retained_forever(self):
        window = SlidingWindow(limit=5, window_seconds=1.0)
        for i in range(5000):
            window.check(f"client-{i}", now=1.0)
        window.check("recent", now=1000.0)
        assert len(window._hits) < 5000, "one-shot clients must not accumulate"

    def test_configuration_is_read_but_nonsense_does_not_disable_it(self):
        guard = guard_from_env({"SEC_TABLES_RATE_LIMIT": "10", "SEC_TABLES_RATE_WINDOW": "60"})
        assert guard.window.limit == 10 and guard.window.window_seconds == 60.0
        fallback = guard_from_env({"SEC_TABLES_RATE_LIMIT": "banana"})
        assert fallback.window.limit > 0

    def test_trust_forwarded_defaults_to_off(self):
        """The safe default is the one that is wrong on a platform and harmless
        off one: keying every visitor to a load balancer throttles everybody
        together, which is visible. The reverse silently removes the limit."""
        assert guard_from_env({}).trust_forwarded is False
        assert guard_from_env({"SEC_TABLES_TRUST_FORWARDED": "1"}).trust_forwarded is True
