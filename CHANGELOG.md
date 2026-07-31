# Changelog

## Unreleased — the browser application, deployed publicly

The Python library under `src/` is untouched. Everything here is `web/`.

**Added**
- `web/proxy/sec_proxy/cors.py` — origin allowlist (never `*`), preflight,
  `Vary: Origin` on allowed *and* denied responses, and
  `Access-Control-Expose-Headers` so a cross-origin `fetch()` can read
  `X-Filing-Meta`. A disallowed origin is refused before any SEC request.
- `web/proxy/sec_proxy/limits.py` — per-client sliding window and a global
  in-flight cap. CORS is not authentication; this is the thing that stops one
  client stalling everyone behind the shared SEC budget.
- `web/app/src/config.ts` — the API origin, resolved at build time from
  `SEC_TABLES_API_BASE`. Three states, and the third matters: a static build with
  no proxy configured says so, instead of requesting `/api/filings` from a static
  host and reporting the 404 page as malformed JSON.
- A warm-up ping on load, so the hosted proxy's cold start overlaps Pyodide's
  preparation rather than landing after the visitor presses a button.
- `render.yaml`, and `PORT`/`0.0.0.0`/SIGTERM handling for a hosted runtime.
- `npm run test:crossorigin` — the whole application workflow again with the API
  on a different origin, through real Chromium preflights.

**Changed**
- GitHub Pages is the production frontend, not a design preview.
- **A long-standing claim was corrected.** "SEC serves no permissive CORS" was
  only ever true of `www.sec.gov`; `data.sec.gov` sends `ACAO: *`. The proxy is
  still required, on stronger grounds: SEC's edge returns 403 to a browser's own
  User-Agent and that 403 carries no CORS header, and a page cannot supply an
  identity — `fetch()` accepts a `User-Agent`, resolves normally, and sends the
  browser's. Measured, not assumed. DECISIONS D38, R10-R11.

**Known limitations**
- The proxy host terminates TLS, so the request body carrying the contact email
  is decrypted on their infrastructure. True of any provider; now stated where a
  visitor can read it rather than only in DECISIONS.
- The filing cache is ephemeral on the selected host and does not survive a
  restart. `/api/health` reports `filingsPersistent: false`.
- ~60 s cold start after 15 minutes idle on the free plan.

## 0.3.0

Downloads filings itself. Three disclosure tables. 190 tests.

**Added**
- `fetch.py` — EDGAR over HTTP, stdlib only, no new dependencies. Built to SEC's
  published rules: 10 req/s as a *total* per-requester ceiling shared across
  hosts, a declared User-Agent with no shipped default, pre-May-2000 filings via
  the complete-submission path, and submission-history pagination.
- `sources.py` / `cache.py` — `Source` protocol with `LocalSource` and
  `EdgarSource` interchangeable; immutable on-disk cache with atomic writes.
- `cli.py` — `sec-tables TICKER --year Y --table T -o out.csv`. Warnings always
  print; provenance is reported separately from warnings; `--strict` turns
  review flags into a non-zero exit.
- Director Compensation (Item 402(r)) and Beneficial Ownership (Item 403).
- `suspect_identity_values` flag — an identity column holding addresses or
  footnote prose.
- Benchmark manifest: per-filing outcomes, content hashes, corpus fingerprint.
- Ground truth: four hand-verified fixtures across three backends and two eras.

**Fixed** — every one produced plausible output with no warning:
- Indented ruler lines truncated the first characters of every name
  ("Ronald W. Allen" → "ald W. Allen") and merged six executives into two.
- A name wrapped across `<br>` became a director called "Richard".
- A holder's address split mid-way, yielding "BlackRock, Inc. 55 East 52 nd".
- `\d{1,6}\s+\w\b` could never match "55 East" — a word boundary after a single
  character. Wrong since written; every test passed.
- bytes and str inputs produced different tables. The CLI reads bytes and the
  tests read text, so the tested path was not the shipped path.
- Prose containing "beneficial owner" was returned as a table.
- Duplicate candidates were reported as ambiguous selections.
- Item 402(r) has not existed before 2006; those filings were counted as misses.
- A person-name test was applied to institutional holders.

**Changed**
- `api.py` no longer names any table; everything table-specific is a profile.
- Benchmark reports `strict%` — clean *and* unflagged. **It is an
  automatic-acceptance rate, not an accuracy estimate.**

**Known limitations**
- Pre-2001 ASCII *ownership* is unreliable (12% strict). Flagged, not fixed.
- No labelled validation beyond four fixtures.

## 0.2.0
Generalised the pipeline so table types are data, not code. Three assembly
strategies. `require_tokens`, `mandated_from`, `identity_is_person`.

## 0.1.0
Summary Compensation Table, 1994-present, including pre-2001 ASCII/SGML.
