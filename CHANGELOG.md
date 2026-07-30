# Changelog

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
