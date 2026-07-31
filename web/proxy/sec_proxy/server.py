"""HTTP surface over `FilingService`.

Standard library only, matching the library's own choice — `fetch.py` uses
urllib so that installing sec-tables drags in no HTTP stack, and a proxy that
exists to expose it should not need a framework to do so.

Two endpoints, both POST. POST is not decoration: the visitor's email is in the
body, and a body does not appear in an access log, a `Referer`, a browser
history entry or a proxy's URL cache the way a query string does. That is the
whole reason the read-only "list filings" call is not a GET.

Since the frontend moved to GitHub Pages the two are no longer same-origin, so
every response also carries a CORS decision (`cors.py`) and every request passes
an abuse check (`limits.py`). Neither changes what the endpoints do.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .core import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_RATE_PER_SECOND,
    SUPPORTED_FORMS,
    SUPPORTED_TABLES,
    FilingService,
    ProxyError,
    describe,
    filing_id,
    validate_email,
    validate_form,
    validate_ticker,
    validate_year,
)
from .cors import CorsPolicy, policy_from_env
from .limits import AbuseGuard, guard_from_env

MAX_BODY_BYTES = 64 * 1024

# Routes an abuse check applies to. `/api/health` is exempt on purpose: it is
# what a platform health check and the frontend's warm-up ping both call, it
# touches neither SEC nor the cache, and rate-limiting it would mean a busy
# proxy reports itself unhealthy and gets restarted.
GUARDED_PREFIX = "/api/"
UNGUARDED = {"/api/health"}


class ProxyHandler(BaseHTTPRequestHandler):
    """One request. Holds no state; the service is on the server object."""

    server_version = f"{APP_NAME}/{APP_VERSION}"
    # Default is HTTP/1.0, which closes the connection after every response and
    # makes a filing fetch pay a fresh TCP handshake.
    protocol_version = "HTTP/1.1"

    # -- privacy ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """Log the method, path and status. Never the body.

        The contact email is in the body of every request, so the default
        access-log line is already safe — but only by accident of where the
        field sits. This override makes it deliberate, and drops the query
        string as well, so no future GET parameter can leak into a log by
        someone adding one.
        """
        if not self.server.log_requests:  # type: ignore[attr-defined]
            return
        path = self.path.split("?")[0]
        sys.stderr.write(f"{self.command} {path} {args[1] if len(args) > 1 else ''}\n")

    # -- cross-origin -------------------------------------------------------

    @property
    def cors(self) -> CorsPolicy:
        return self.server.cors  # type: ignore[attr-defined]

    @property
    def guard(self) -> AbuseGuard:
        return self.server.guard  # type: ignore[attr-defined]

    def _origin(self) -> Optional[str]:
        return self.headers.get("Origin")

    def _cors_headers(self, *, preflight: bool = False) -> dict[str, str]:
        return self.cors.headers(self._origin(), preflight=preflight)

    def _origin_permitted(self) -> bool:
        """A browser's origin must be on the list; a non-browser has none.

        Refusing a disallowed origin *before* doing the work is the point. A
        browser would discard the response anyway for want of an
        `Access-Control-Allow-Origin` header, so serving it would spend a request
        from the shared SEC budget to produce something nobody can read.
        """
        origin = self._origin()
        return origin is None or self.cors.allows(origin)

    # -- plumbing -----------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ProxyError("malformed Content-Length") from None
        if length <= 0:
            raise ProxyError("a JSON body is required")
        if length > MAX_BODY_BYTES:
            raise ProxyError("request body too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ProxyError("malformed JSON body") from None

    def _send(self, status: int, payload: Any, extra: Optional[dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Every response, including errors. A 429 or a 502 that a browser cannot
        # read becomes a bare "Failed to fetch", which is the least informative
        # message the app can show for the two failures most worth explaining.
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_payload(self, exc: ProxyError) -> None:
        self._send(exc.status, {"error": {"kind": exc.kind, "message": exc.message}})

    @property
    def service(self) -> FilingService:
        return self.server.service  # type: ignore[attr-defined]

    # -- routes -------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        """The preflight.

        A JSON `Content-Type` is not CORS-safelisted, so the browser sends this
        before every real call and will not send the call at all if the answer
        does not name the method and the header. 204 rather than 200: there is
        no body, and a 200 with `Content-Length: 0` invites an intermediary to
        cache an empty body against the URL.
        """
        headers = self._cors_headers(preflight=True)
        allowed = "Access-Control-Allow-Origin" in headers
        self.send_response(204 if allowed else 403)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/health":
            # Reachable from any origin and never rate-limited: it is the
            # platform's health check and the frontend's warm-up ping, and it
            # reports configuration rather than anything about a visitor.
            self._send(200, {
                "ok": True,
                "app": f"{APP_NAME}/{APP_VERSION}",
                "ratePerSecond": self.service.rate_per_second,
                "forms": list(SUPPORTED_FORMS),
                "tables": list(SUPPORTED_TABLES),
                "cache": {
                    "metadataHits": self.service.metadata.hits,
                    "metadataMisses": self.service.metadata.misses,
                    "filingsEnabled": self.service.filings is not None,
                    # Named so nobody reads "cache" as "durable". On a host with
                    # an ephemeral filesystem this directory does not survive a
                    # restart, and the README says which host that is.
                    "filingsPersistent": bool(self.server.cache_persistent),  # type: ignore[attr-defined]
                },
                "upstreamRequests": self.service.upstream_requests,
            })
            return
        self._send(404, {"error": {"kind": "not_found", "message": f"no route {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if not self._origin_permitted():
            self._send(403, {"error": {
                "kind": "origin_not_allowed",
                "message": "This proxy does not serve that origin.",
            }})
            return

        guarded = path.startswith(GUARDED_PREFIX) and path not in UNGUARDED
        acquired = False
        try:
            if guarded:
                key = self.guard.client_key(
                    self.client_address[0], self.headers.get("X-Forwarded-For")
                )
                allowed, remaining, retry_after = self.guard.check(key)
                if not allowed:
                    self._send(
                        429,
                        {"error": {
                            "kind": "throttled",
                            "message": (
                                "Too many requests from this client. This server shares one "
                                "SEC request budget with everyone using it."
                            ),
                        }},
                        {"Retry-After": str(retry_after), "X-RateLimit-Remaining": "0"},
                    )
                    return
                if not self.guard.in_flight.acquire():
                    # Refusing beats queueing: past this point every extra thread
                    # would sit in the shared SEC limiter's sleep, and a visitor
                    # cannot tell a long wait from a broken page.
                    self._send(
                        503,
                        {"error": {
                            "kind": "throttled",
                            "message": "This server is busy. Try again in a few seconds.",
                        }},
                        {"Retry-After": "5"},
                    )
                    return
                acquired = True
                self._remaining = remaining

            if path == "/api/filings":
                self._filings()
            elif path == "/api/filing":
                self._filing()
            else:
                self._send(404, {"error": {"kind": "not_found", "message": f"no route {path}"}})
        except ProxyError as exc:
            self._send_error_payload(exc)
        except Exception as exc:  # pragma: no cover - last resort
            # The message is deliberately generic: an unexpected exception's text
            # can carry a URL or a header, and this response is going to a browser.
            sys.stderr.write(f"unhandled {type(exc).__name__} on {path}\n")
            self._send(500, {"error": {"kind": "internal", "message": "the proxy failed unexpectedly"}})
        finally:
            if acquired:
                self.guard.in_flight.release()

    def _filings(self) -> None:
        body = self._read_json()
        email = validate_email(body.get("email"))
        ticker = validate_ticker(body.get("ticker"))
        year = validate_year(body.get("year"))
        form = validate_form(body.get("form"))

        refs = self.service.list_filings(email=email, ticker=ticker, year=year, form=form)
        self._send(200, {
            "ticker": ticker,
            "year": year,
            "form": form,
            "filings": [describe(r) for r in refs],
            # The one a caller gets if it does not choose: the latest, matching
            # `pick_filing`'s default and its reasoning.
            "defaultId": filing_id(refs[-1]) if refs else None,
        })

    def _filing(self) -> None:
        body = self._read_json()
        email = validate_email(body.get("email"))
        ticker = validate_ticker(body.get("ticker"))
        year = validate_year(body.get("year"))
        form = validate_form(body.get("form"))
        wanted = body.get("filingId")
        if wanted is not None and not (isinstance(wanted, str) and wanted.isalnum()):
            raise ProxyError("malformed filing id")

        refs = self.service.list_filings(email=email, ticker=ticker, year=year, form=form)
        ref = self.service.resolve(refs, wanted)
        data, cached = self.service.fetch(email=email, ref=ref)

        meta = describe(ref, source_url=True)
        # The document goes back as bytes, not as base64 inside JSON: a filing
        # can be several megabytes and the browser needs an ArrayBuffer anyway.
        # Metadata rides on headers, which keeps one response instead of two
        # round trips.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Filing-Meta", json.dumps(meta, separators=(",", ":")))
        self.send_header("X-Filing-Cache", "hit" if cached else "miss")
        self.send_header("X-RateLimit-Remaining", str(getattr(self, "_remaining", "")))
        # Cross-origin, a `fetch()` may read seven response headers and no more.
        # `X-Filing-Meta` carries the filing's identity, so without this the
        # bytes arrive and the app has nothing to label them with.
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        service: FilingService,
        *,
        log_requests: bool = True,
        cors: Optional[CorsPolicy] = None,
        guard: Optional[AbuseGuard] = None,
        cache_persistent: bool = False,
    ):
        super().__init__(address, ProxyHandler)
        self.service = service
        self.log_requests = log_requests
        # Defaults rather than `policy_from_env()`, so constructing a server in a
        # test does not silently inherit the developer's shell.
        self.cors = cors if cors is not None else CorsPolicy()
        self.guard = guard if guard is not None else AbuseGuard()
        self.cache_persistent = cache_persistent


def build_server(
    host: str = "127.0.0.1",
    port: int = 5310,
    *,
    cache_dir: Optional[Path] = None,
    rate_per_second: float = DEFAULT_RATE_PER_SECOND,
    log_requests: bool = True,
    cors: Optional[CorsPolicy] = None,
    guard: Optional[AbuseGuard] = None,
    cache_persistent: bool = False,
) -> ProxyServer:
    service = FilingService(cache_dir=cache_dir, rate_per_second=rate_per_second)
    return ProxyServer(
        (host, port), service,
        log_requests=log_requests, cors=cors, guard=guard, cache_persistent=cache_persistent,
    )


def resolve_port(env: Optional[dict[str, str]] = None, default: int = 5310) -> int:
    """The port the platform told us to use, or the local default.

    `PORT` is the contract every one of the hosts considered uses (Render, Fly,
    Cloud Run, Railway) and it is checked first. `PROXY_PORT` stays because the
    dev script and the Playwright config already set it, and a deployment that
    happens to define both should follow the platform.
    """
    env = os.environ if env is None else env
    for name in ("PORT", "PROXY_PORT"):
        raw = (env.get(name) or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return default


def resolve_host(env: Optional[dict[str, str]] = None) -> str:
    """Loopback locally, every interface when a platform assigned the port.

    A container that binds 127.0.0.1 passes its own health check and is
    unreachable from outside it — a failure that looks like a routing problem and
    is not. Binding `0.0.0.0` only when `PORT` came from the environment keeps
    `python -m sec_proxy.server` on a laptop off the local network by default.
    """
    env = os.environ if env is None else env
    explicit = (env.get("HOST") or "").strip()
    if explicit:
        return explicit
    return "0.0.0.0" if (env.get("PORT") or "").strip() else "127.0.0.1"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SEC filing proxy for the sec-tables web app")
    parser.add_argument("--host", default=resolve_host())
    parser.add_argument("--port", type=int, default=resolve_port())
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("SEC_TABLES_CACHE", str(Path.home() / ".cache" / "sec-tables-web")),
        help="Where filing bytes are kept. Filings are immutable, so this never expires.",
    )
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_PER_SECOND,
                        help="Requests per second across ALL visitors (SEC's ceiling is 10).")
    parser.add_argument(
        "--cache-persistent", action="store_true",
        help="Assert the cache directory survives a restart. Reported on /api/health; "
             "set it only when a real volume is mounted, because the honest default is false.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cors = policy_from_env()
    guard = guard_from_env()
    server = build_server(
        args.host, args.port,
        cache_dir=Path(args.cache_dir),
        rate_per_second=args.rate,
        log_requests=not args.quiet,
        cors=cors,
        guard=guard,
        cache_persistent=args.cache_persistent,
    )
    sys.stderr.write(
        f"sec-tables proxy on http://{args.host}:{args.port} "
        f"(cache {args.cache_dir}, persistent={args.cache_persistent}, "
        f"{args.rate} req/s shared)\n"
        f"  origins: {', '.join(sorted(cors.origins))}"
        f"{' + loopback' if cors.allow_loopback else ''}\n"
        f"  per-client: {guard.window.limit} requests / {guard.window.window_seconds:.0f}s, "
        f"max {guard.in_flight.limit} in flight\n"
    )

    # Platforms stop a container by sending SIGTERM and killing it a short while
    # later. Without this the default disposition terminates the process
    # immediately and a filing being written to the cache is truncated — which
    # then reads back as a valid cache hit of a half-document.
    def _stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:  # pragma: no cover - not on the main thread
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
