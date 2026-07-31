# Live SEC application — architecture and compatibility

Written before implementation, from the existing graph rather than from a reread
of the tree. Records what is reused, what is new, and the three constraints that
shape the split.

## The split

```
browser                          server (same origin)            SEC
─────────────────────────────    ─────────────────────────    ─────────
React form                       proxy /api/filings   ───────► data.sec.gov
  email, ticker, year, form,       EdgarSource.list_filings     submissions
  table                            (+ historical `files` pages)
        │
        ├─ POST /api/filings ───► proxy /api/filing    ───────► www.sec.gov
        │  ◄── filing list         EdgarSource.read              /Archives
        │                          FilingCache
        ├─ POST /api/filing ─────►
        │  ◄── raw bytes + metadata headers
        │
        ├─ sandboxed <iframe>  ← blob: URL of those bytes
        │
        └─ SecTablesBridge.extract(ArrayBuffer) ─► Pyodide Worker ─► sec_tables.extract()
                                                     (unchanged)
```

**Fetching is server-side. Extraction is browser-side.** Nothing else moves.

## What is reused, unchanged

The graph query on `EdgarSource` / `FilingRef` / `FilingCache` / `pick_filing`
returned a complete filing-acquisition path already implemented in Python:

| need | already exists | where |
| --- | --- | --- |
| ticker → CIK | `EdgarSource.ticker_map` / `cik_for` | `fetch.py:235,253` |
| filing history incl. **historical pagination** | `_submission_records` follows `filings.files[]` | `fetch.py:263` |
| **pre-May-2000 complete-submission route** | `_document_url` switches on `_DIRECTORY_LAYOUT_FROM` | `fetch.py:325` |
| multiple filings per year | `list_filings` returns a sorted list; `pick_filing` chooses | `fetch.py:281`, `sources.py:139` |
| declared User-Agent, no default | `resolve_user_agent` — rejects bare email *and* missing app name | `fetch.py:69` |
| one shared rate budget | `RateLimiter`, "one bucket, not one per host" | `fetch.py:97` |
| 403/429/5xx retry with `Retry-After` | `EdgarClient.get` | `fetch.py:154` |
| immutable filing cache | `FilingCache` | `cache.py:38` |

The proxy is therefore an HTTP surface over `fetch.py`, not a reimplementation.
**No filing-discovery or archive-path logic is written in TypeScript.**

Two seams make it fit without touching the library:

* `EdgarClient.limiter` is a plain attribute assigned in `__post_init__`, so the
  proxy replaces it with one process-wide `RateLimiter` after construction. Every
  visitor shares one budget, which is what SEC's per-requester ceiling means.
* `EdgarClient._http_get` is documented as "single seam for the network", so
  every proxy test mocks that one method and no test touches SEC.

## Three constraints, and what each forces

**1. A browser still cannot fetch from SEC.** `www.sec.gov/Archives` sends no
permissive `Access-Control-Allow-Origin`, so a `fetch()` from a page is blocked
by CORS before it starts. That has not changed and is not worked around here —
it is the reason a server sits in the path at all. Related: a page cannot make an
honest SEC User-Agent declaration, because the browser controls that header.

**2. A contact goes to SEC; asking the visitor for it is our choice.**
*(Revised — the earlier text here said the visitor's email was "SEC's
requirement, not ours", which overstated the guidance.)* SEC's fair-access
policy asks the **requester** — the application or company making automated
requests — to declare itself with a monitored contact address. It does not ask
a public website to collect every visitor's personal address.

This app asks for one anyway, and that is a product decision: the alternative is
shipping a single address that carries every user's traffic, which D19 rules out
because that identity is the one that gets blocked and it misrepresents who is
asking. The proxy builds `sec-tables-web/0.1 (<visitor email>)` per request and
sends it in the `User-Agent` header.

What this application does with it: uses it for one HTTP call chain and
discards it. It is not written to disk, not part of the cache key or any
filename, not in a URL (every API call is POST, so it cannot reach an access
log), and not logged. **That is a statement about this code, not a guarantee
covering hosting providers, reverse proxies, browser extensions or any other
infrastructure in the path.** The form says all of this before the field.

**3. Extraction can hang, not just fail.** The bridge already found that
`colspan="2000000000"` expands the grid until the thread stops responding. So
the app enforces a wall-clock extraction timeout and terminates the Worker, which
is the bridge's existing cancellation semantics. No cooperative-cancellation
claim is made anywhere, because Python cannot honour one.

## What the browser is not trusted with

The client never supplies a URL. It sends `{ticker, year, form}` and, for the
second call, an opaque `filingId` that the proxy re-resolves against its own
listing. A locator is only ever produced by `EdgarSource`, and is additionally
checked against an allowlist of SEC hosts and path prefixes before any fetch.
That closes SSRF by construction rather than by validation.

## Compatibility with the existing bridge

Nothing in `src/worker.ts`, `src/client.ts`, `src/protocol.ts`, `src/pin.ts` or
`src/sec_bridge.py` changes. The app is a consumer of `SecTablesBridge` exactly
as the harness is, and all 28 bridge tests continue to run against the harness
page. The one addition on the app side is a timeout wrapper around
`bridge.extract()` that calls the existing `bridge.cancel()` — built out of the
published API, not by reaching into the worker.
