"""HTTP surface over `FilingService`.

Standard library only, matching the library's own choice — `fetch.py` uses
urllib so that installing sec-tables drags in no HTTP stack, and a proxy that
exists to expose it should not need a framework to do so.

Two endpoints, both POST. POST is not decoration: the visitor's email is in the
body, and a body does not appear in an access log, a `Referer`, a browser
history entry or a proxy's URL cache the way a query string does. That is the
whole reason the read-only "list filings" call is not a GET.
"""
from __future__ import annotations

import json
import os
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

MAX_BODY_BYTES = 64 * 1024


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

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/health":
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
                },
                "upstreamRequests": self.service.upstream_requests,
            })
            return
        self._send(404, {"error": {"kind": "not_found", "message": f"no route {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
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
        self.send_header("Access-Control-Expose-Headers", "X-Filing-Meta, X-Filing-Cache")
        self.end_headers()
        self.wfile.write(data)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: FilingService, *, log_requests: bool = True):
        super().__init__(address, ProxyHandler)
        self.service = service
        self.log_requests = log_requests


def build_server(
    host: str = "127.0.0.1",
    port: int = 5310,
    *,
    cache_dir: Optional[Path] = None,
    rate_per_second: float = DEFAULT_RATE_PER_SECOND,
    log_requests: bool = True,
) -> ProxyServer:
    service = FilingService(cache_dir=cache_dir, rate_per_second=rate_per_second)
    return ProxyServer((host, port), service, log_requests=log_requests)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SEC filing proxy for the sec-tables web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", 5310)))
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("SEC_TABLES_CACHE", str(Path.home() / ".cache" / "sec-tables-web")),
        help="Where filing bytes are kept. Filings are immutable, so this never expires.",
    )
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_PER_SECOND,
                        help="Requests per second across ALL visitors (SEC's ceiling is 10).")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    server = build_server(
        args.host, args.port,
        cache_dir=Path(args.cache_dir),
        rate_per_second=args.rate,
        log_requests=not args.quiet,
    )
    sys.stderr.write(
        f"sec-tables proxy on http://{args.host}:{args.port} "
        f"(cache {args.cache_dir}, {args.rate} req/s shared)\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
