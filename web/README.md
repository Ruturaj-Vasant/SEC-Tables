# sec-tables in the browser

Two things live here:

1. **The application** (`app/`, `proxy/`) — type a ticker and a year, get the
   filing and one disclosure table out of it. A small server fetches from SEC;
   the extraction runs in your tab, in Python, via WebAssembly.
2. **The Pyodide bridge** (`src/`) — the typed Web Worker boundary the app runs
   on. Bytes of a filing go in, a JSON-serializable extraction comes out. It has
   no interface, no SEC networking and no way to run arbitrary Python, and is
   usable on its own.

Both are verified in real Chromium against the library's own hand-verified
fixtures. `ARCHITECTURE.md` records the split and what each half may do.

```bash
npm install && npx playwright install chromium
npm run dev            # the app on http://127.0.0.1:5199/app.html (fetches from SEC)
npm run dev:fake       # the same app, served committed fixtures, no network
```

## Deploying it

Two hosts, because GitHub Pages is static and cannot run `web/proxy/`.

```
GitHub Pages ──HTTPS──► a small Python proxy ──► SEC EDGAR
  React + Pyodide         fetch + cache only
  extraction runs here
```

### 1. The proxy

`render.yaml` is a Render Blueprint: **New → Blueprint** in the Render dashboard,
point it at this repository. Nothing in it is a secret. The one value to review
is `SEC_TABLES_ALLOWED_ORIGINS`, which must name the frontend's origin —
`https://<account>.github.io`, not the repository path, because a Pages *project*
site shares its owner's origin.

Then confirm it is up:

```bash
curl https://<your-service>.onrender.com/api/health
```

Why Render, and what it costs, is DECISIONS D40. The short version:

- **Free**, and on the free plan "one instance" is a property of the plan rather
  than a setting. That matters here specifically: `core.py`'s SEC rate limiter is
  process-local, so a second replica would be a second full request budget
  against a ceiling that is per requester.
- **~60 s cold start** after 15 minutes of no traffic. The app pings
  `/api/health` on load so the wait overlaps Pyodide's 1.3 s preparation and the
  time spent typing — which helps, and does not eliminate it.
- **Ephemeral filesystem.** The filing cache does not survive a restart or a
  redeploy, so each restart costs one re-fetch per filing anyone asks for again.
  At this traffic that is a handful of SEC requests, which is why no paid volume
  is attached. `/api/health` reports `filingsPersistent: false`; nothing in the
  system claims otherwise.

Any of Fly.io, Cloud Run or Railway would also work — `PORT`, a health path and
SIGTERM are the whole contract — but see D40 for why they were not chosen.

### 2. The frontend

In repository settings choose **Pages → Build and deployment → GitHub Actions**,
then set the repository **variable** (Settings → Secrets and variables → Actions
→ Variables):

```
SEC_TABLES_API_BASE = https://<your-service>.onrender.com
```

A variable rather than a secret on purpose: it is a public URL that ships inside
a bundle any visitor can read, and storing it as a secret would imply a
confidentiality it does not have.

The workflow deploys pushes to `main` (and supports a manual run). If the
variable is unset the build still succeeds, the workflow prints a warning, and
the deployed page says plainly that it has no filing server — rather than
requesting `/api/filings` from a static host and reporting the 404 page as
malformed JSON.

### Running it locally

No configuration and no CORS: `tools/serve.mjs` forwards `/api/*` to the proxy on
another port, so page and API share an origin. `npm run dev` and everything works.

To point a local build at the deployed proxy instead:

```bash
SEC_TABLES_API_BASE=https://<your-service>.onrender.com npm run build
```

## The application

```
email → ticker → year → form (DEF 14A) → table
        ↓
   POST /api/filings   → every matching filing, never silently one of several
   POST /api/filing    → the raw bytes + metadata on headers
        ↓
   the filing, in a sandboxed frame
   the normalized table below it, with review warnings and provenance apart
   CSV of exactly the rows on screen
```

**Fetching is server-side and extraction is browser-side**, and neither half is
arbitrary. A page cannot fetch from SEC, for three separate measured reasons:
`www.sec.gov` sends no `Access-Control-Allow-Origin` at all; `data.sec.gov` does
send `*`, but SEC's edge answers a browser's own User-Agent with 403 and that 403
carries no CORS header either; and a page cannot supply the identification that
would fix it, because `fetch()` accepts a `User-Agent`, resolves without error,
and sends the browser's. Extraction, by contrast, needs no network at all: the
library is bytes in, table out. See DECISIONS D29-D30 and D38.

The proxy (`proxy/sec_proxy/`) is an HTTP surface over `sec_tables.fetch` and
adds no filing logic of its own — ticker→CIK, the historical `files[]`
pagination, the pre-May-2000 complete-submission route and the User-Agent rules
are the library's, already tested. It holds one SEC request budget for every
visitor at once, caches filing bytes forever (a filing is immutable once filed),
and **does not intentionally persist or log the contact email**: POST-only so it
cannot reach an access log, absent from the cache key and from every filename,
redacted from request logs. Tests assert that, and one asserts the opposite —
that it *does* reach SEC, because that is what it is for.

Asking the visitor for an address is this project's design choice, not an SEC
rule. SEC asks the *requester* to identify itself with a monitored contact; it
does not ask websites to collect their visitors' addresses. The alternative — one
shipped address carrying everyone's traffic — is what D19 rules out.

**And the statement above is about this application's code only.** That
qualification used to be hypothetical and is now concrete: the proxy is hosted,
which means **the host terminates TLS**, so the request body — which is where the
address is — is decrypted on their infrastructure. That is true of every provider
and there is nothing in this repository that can change it. Reverse proxies,
network intermediaries and browser extensions are outside it too. What is
testable, and tested, is that *this code* does not put the address in a URL, a
cache key, a filename, a log line or a response, cross-origin included.

### Abuse protection, and what it does not do

The proxy is public, so one client is stopped from spending everyone's SEC
budget: 60 requests per 5 minutes per client, plus a cap of 8 requests in flight.
Over either, the answer is a `throttled` error with `Retry-After`.

This is not security. CORS is enforced by browsers and any script bypasses it;
the limit has no accounts and cannot tell a second visitor from a second tab; and
a distributed client defeats it entirely. Behind Render the client address comes
from `X-Forwarded-For`, which a client can send — correct only because Render
overwrites it, and `SEC_TABLES_TRUST_FORWARDED` defaults to off for that reason.
`test_limits.py` includes a test proving the limit *is* defeated when that flag
is on, because a suite that showed it holding everywhere would be showing
something false. DECISIONS D42.

### What the app deliberately does not have

No file upload (the CLI is better at that), no custom Python, no saved projects,
no CodeMirror, no algorithm comparison, no advertisements. DECISIONS D28.

### Runtime safety

A filing containing `colspan="2000000000"` makes the library expand the grid
until the worker stops responding — it does not raise and does not return, so a
size limit would not catch it and there is nothing to await. The app therefore
enforces a wall-clock timeout and **terminates the worker**, which is the
bridge's existing cancellation. No cooperative cancellation is claimed anywhere,
because Pyodide holds the thread and Python could not honour one.

---

# The Pyodide bridge

```ts
const bridge = new SecTablesBridge({ workerUrl: new URL("./worker.js", import.meta.url) });

await bridge.prepare();                       // ~1.3 s, once
const result = await bridge.extract(          // ArrayBuffer, transferred not copied
  await file.arrayBuffer(),
  "1997-09-19",
  "summary_compensation",
);

result.ok        // true
result.backend   // "ascii"  — no DOM existed in 1997 to parse
result.era       // "pre2006"
result.provenance // ["ascii_source"] — how the answer was obtained, not a defect
result.rows      // string[][]
```

---

## Where this sits against a settled decision

`MASTER_PROMPT.md` §2 and `DECISIONS.md` D6 record **"no React / pyodide
frontend"** as settled, on three grounds: SEC serves no permissive CORS on
`/Archives`, filings are megabytes, and rate limits push fetching server-side.

The first and third of those are about **fetching**, and this bridge does not
fetch. `sec_tables.fetch` is never imported; documents arrive as an ArrayBuffer
from whatever supplied them. The second — document size — was the open empirical
question, and it is now measured rather than assumed: a 17 KB proxy extracts in
33 ms warm, a 1 MB document in 0.6 s, and the wall is at ~512 MB, far above any
real filing. See [Measurements](#measurements).

So this does not reopen D6, and the application above does not either: **the
browser still never talks to SEC**. A server does the fetching, for the two
reasons D29 records — no permissive CORS, and a page cannot set its own
`User-Agent` to identify itself. What D6 rules out is a browser that downloads
filings from SEC. Nothing here does that, and nothing here should be extended to.

---

## Phase 0 — compatibility, established by running it

| question | answer | evidence |
| --- | --- | --- |
| Does the v0.3.0 wheel install in current stable Pyodide? | **Yes**, unchanged. | `micropip.install(..., deps=False)` into Pyodide 314.0.3. |
| Which Pyodide? | **314.0.3** — the current `latest` on npm. Ships **CPython 3.14.2** (the lock file declares the 3.14 ABI series) and **lxml 6.0.2** as a built package. | `vendor/pyodide/pyodide-lock.json`, and the runtime reports its own versions at preparation. |
| How must lxml be loaded? | `pyodide.loadPackage("lxml")`, **never** micropip from PyPI. lxml is a C extension; only the Emscripten build works, and only Pyodide's distribution has one. Pyodide integrity-checks it against `pyodide-lock.json`. | — |
| Can the wheel install with dependency resolution off, after lxml? | **Yes.** `deps: false` from an `emfs:` path. One catch: micropip parses the *filename*, so the file must be written to the in-memory FS under its real wheel name — `/tmp/w.whl` fails with a wheel-filename parse error. | — |
| Any import that fails in a browser? | **None.** `sec_tables`, `sec_tables.api`, `select.dom`, `tabulate`, `cache`, `sources`, `cli` and even `fetch` all import cleanly — `fetch.py` uses `urllib` which imports fine in Pyodide, it just cannot open a socket. | probe over every module |
| Is the extraction core isolated from `fetch.py`? | **Yes, by construction.** `import sec_tables` pulls 14 modules and `sec_tables.fetch` is not one of them, nor is `cli`, nor is `urllib`. No library change was needed to get this. | `sys.modules` diff across the import |
| JSON shape of a result? | Confirmed against the real objects — see below. | `src/sec_bridge.py` |
| Any library change required? | **No.** `SEC_Library` is untouched: no file under `src/`, `tests/`, `bench/` or `pyproject.toml` was modified. | `git diff` |

### The result shape, checked against the real `Extraction`

The shape in the task description survived contact with the dataclass, with two
things worth stating:

* **`columns`** is `table.roles` when roles were assigned and `table.header`
  otherwise — the same precedence `Table.to_csv()` uses, so the browser shows
  the column names the library would write.
* **`metadata`** is `Extraction.meta` verbatim. Observed values across all three
  profiles: `candidates: int`, `profile: str`, `top_score: int`,
  `margin: int | null`, `rows_before_assembly: int`, and on flagged results
  `unmapped: string[]`, `missing_roles: string[]`, `suspect_identities:
  string[]`, `era_from_columns: str`. All JSON-safe. The encoder still passes
  `default=str`, because `meta` is an open `dict[str, Any]` the library may add
  to and a future non-serializable value should degrade to its repr rather than
  break every extraction in the browser.

A **failed** extraction keeps the same shape: `ok: false`, empty `columns` and
`rows`, `backend: null`. `error` is present **only when Python actually raised**.
"No table in this document" is a *result* — `ok: false` with
`flags: ["no_table_found"]` and no `error` — because that is what the library
means by it, and flattening the two would throw away the distinction the whole
flag system exists to preserve.

---

## Architecture

```
src/pin.ts          every version, URL and checksum this bridge trusts
src/protocol.ts     the versioned wire contract + request validation
src/sec_bridge.py   the Python half: bytes in, JSON string out
src/worker.ts       the worker: one runtime, one wheel, one pinned callable
src/client.ts       main-thread client; promises, lifecycle, cancellation

tools/vendor-pyodide.mjs  assemble a pinned, self-hosted Pyodide + lxml
tools/build-wheel.mjs     rebuild the wheel and check it against the pin
tools/gen_expected.py     CPython expectations for the parity check
tools/serve.mjs           static server for the suite (no bundler in the path)
tools/measure.mjs         the measurements below

test/browser/       28 Playwright tests, real Chromium, real wheel
```

### Five decisions worth the words

**The wheel is checksum-verified before the interpreter sees it.** A wheel is
executable code running with the page's privileges. It is fetched, hashed with
Web Crypto, compared to `WHEEL_SHA256`, and only then written to the in-memory
filesystem. A test serves a copy with one byte flipped and asserts preparation
refuses it.

**Nothing is resolved from an index.** lxml comes from Pyodide's own
distribution; the wheel installs with `deps: false` from `emfs:`. There is no
path by which this bridge fetches a package it was not pinned to.

**Python returns a JSON string, never an object.** A Python object crossing into
JS becomes a PyProxy the caller must destroy by hand, and one missed `destroy()`
pins memory for the life of the page. A `str` converts outright, so the
steady-state proxy cost of an extraction is **zero**. Four proxies exist in
total — the pinned callables — and a test asserts that number is still four
after thirteen extractions.

*(Getting this right needed one non-obvious step: an attribute read off a module
proxy is **borrowed** from it, so destroying the module destroys the functions
with it. They are `.copy()`d, then the module proxy is released.)*

**The bridge is installed as a module, not executed into globals.** A
per-request global outlives the request. `sec_bridge.py` goes into
site-packages, and diagnostics compare the interpreter's global namespace
against its state at preparation — a test asserts nothing was added.

**Cancellation terminates the worker.** Pyodide owns the worker's only thread
while Python runs, so a `cancel` message is not dequeued until the work it would
cancel has already finished. A *queued* request can be cancelled in-worker; a
*running* one cannot, and pretending otherwise would be a lie in the type
signature. This is not just a Pyodide quirk: a filing with
`colspan="2000000000"` makes the library expand the grid until the thread stops
responding — verified in CPython, where it does not raise and does not return —
so termination is the only defence that works. The cost is a cold start
afterwards, which is why `cancel()` is explicit rather than automatic.

---

## Verification

**54 browser tests** — 28 for the bridge, 19 for the application, 7 for the
cross-origin contract — plus 40 unit tests and 127 proxy tests. One real browser,
the real wheel, no mocked Python anywhere.

```bash
cd web
npm install
npx playwright install chromium

npm test                                  # 54 Playwright tests (vendor + build first)
npm run test:crossorigin                  # the app workflow again, on two origins
npm run unit                              # 40 unit tests, node, no browser
npm run typecheck                         # tsc --noEmit
npm run test:proxy                        # 127 proxy tests, SEC mocked at its network seam
SEC_LIVE_EMAIL=you@example.com npm run test:live   # opt-in: the one test that calls SEC
```

The application suite runs against the **real** proxy with SEC's network seam
faked, so routing, validation, caching and error mapping are all genuine while
the filing bytes stay the committed fixtures. `npm run test:live` swaps in the
real proxy and is the only thing here that touches EDGAR; CI never depends on it.

`npm run test:crossorigin` is the one that covers the deployed shape. It rebuilds
the frontend against the proxy's own port — a different port is a different
origin — and re-runs the **entire** application workflow through real Chromium
preflights: the DAL 1997 acceptance case, the filing chooser, CSV, and the test
that the contact address never reaches a URL. Reusing the existing suite rather
than writing a parallel cross-origin one is deliberate; a second copy would drift
from the first. A footer assertion fails the run if the build quietly came out
same-origin, which would otherwise let it claim to prove something it had not.

They answer three separate questions:

1. **Is it the right answer?** Values a person read off the filings, copied from
   `tests/test_golden.py` into `test/browser/fixtures.ts` — all six committed
   fixtures, across ASCII, SGML and DOM, and all three table profiles.
2. **Is it the same answer as CPython?** Whole-output equality against
   `test/expected/cases.json`, generated by running **the same bridge module**
   under host CPython. This is the check that would catch wasm-lxml parsing a
   filing differently, and it covers every field except the two timings.
3. **Does the boundary behave?** One runtime under concurrent preparation, warm
   reuse, transferred bytes, flag partitioning, error conversion, protocol
   rejection, cancellation and rebuild, no leaks, a clean console.

```
✓ cold preparation loads the pinned runtime and reports what it loaded
✓ two simultaneous preparations initialize exactly one runtime
✓ a wheel that fails its checksum is refused
✓ ASCII: Delta 1997, values as read off the filing
✓ ASCII: Delta 1994, the indented-ruler regression
✓ SGML: Alaska Air 1994 selects the SGML backend
✓ DOM: Compass Minerals 2024 director compensation, all ten directors
✓ DOM: AZZ 2019 beneficial ownership keeps the address out of the name
✓ ASCII: CVS 1996 ownership, three holders and no footnote prose
✓ DOM: Apple 2003 explodes stacked years into person-years
✓ all three profiles work through one warm runtime
✓ parity with CPython: 8 documents
✓ raw bytes are the primary input path and are transferred, not copied
✓ warm repeat extraction reuses the runtime and is faster than the cold one
✓ a document with no table is a result, not an error
✓ review flags and provenance flags stay separate
✓ a Python exception becomes a typed error on a well-formed result
✓ a malformed message is rejected without touching the runtime
✓ cancelling terminates the worker, and the rebuilt one extracts correctly
✓ a queued request can be cancelled without losing the running one
✓ repeated extraction leaks no PyProxy, no Python objects and no request state

✓ DAL 1997 Summary Compensation: the whole workflow, with verified values
✓ the CSV downloads the same canonical rows that are on screen
✓ a modern HTML filing extracts director compensation through lxml
✓ beneficial ownership keeps the address out of the holder name
✓ a year with two filings shows both and defaults to the later one
✓ form errors are reported per field before anything is sent
✓ a ticker SEC does not know is a clear message, not a crash
✓ asking for a table the filing does not contain is a result, not an error
✓ a filing that hangs extraction is stopped, and the app recovers
✓ the filing is sandboxed and cannot reach the application
✓ the blob URL is released when the filing changes
✓ review warnings are visually separate from provenance, on one result
✓ nothing on the page claims the result is verified or accurate
✓ the contact field says where the address goes before it is typed
✓ the layout reflows on a narrow viewport without horizontal scroll
✓ the whole form is reachable and operable from the keyboard
✓ invalid fields are announced to assistive technology

45 passed
```

Every test also fails if the browser console is dirty — a worker that throws
inside `onmessage`, a wasm abort, an unhandled rejection during preparation all
print without necessarily breaking an assertion.

### Fault injection, and why it exists

The Python-exception test uses a deliberate injected fault (`selftest`), routed
through the *same* worker code that wraps a real extraction. It is there because
**no valid filing reaches a Python `raise`**: sec-tables returns everything it
anticipates as flags. The inputs that might raise do not — a two-billion
`colspan` hangs instead. The one genuine exception observed in this work came
from the size sweep below, where a 512 MB document produced a real
`MemoryError`, converted correctly and without killing the runtime.

---

## Measurements

One device, one browser, one run. **These are observations, not guarantees.**
Regenerate with `npm run measure` (writes `measurements.json`).

**Apple M3 Pro (Mac15,6), 11 cores, 18 GiB · macOS darwin 25.3.0 arm64 ·
Chromium 151.0.7922.34, headless, via Playwright · assets served from
127.0.0.1** · Pyodide 314.0.3 · Python 3.14.2 · lxml 6.0.2 · sec-tables 0.3.0.

### What a browser downloads

| | raw | gzip |
| --- | ---: | ---: |
| Pyodide runtime (`pyodide.asm.wasm` 9.15, `python_stdlib.zip` 2.43, `pyodide.asm.mjs` 1.19, lock 0.11) | 12.90 MB | 6.10 MB |
| lxml wheel | 1.56 MB | 1.56 MB |
| micropip wheel | 0.11 MB | 0.11 MB |
| **sec-tables 0.3.0 wheel** | **0.06 MB** (65,215 B) | 0.06 MB |
| worker bundle | 0.02 MB | 0.01 MB |
| **total** | **14.65 MB** | **7.83 MB** |

The wheels are already-compressed zips, so gzip buys nothing on them; the
runtime compresses well. The library itself is 0.4% of the payload — what a
browser pays for here is Python, not sec-tables.

### Preparation

| | |
| --- | ---: |
| cold preparation, empty cache | **1,255 ms** |
| cold preparation, warm HTTP cache, fresh page | **1,259 ms** |

Identical, and that is the finding: over loopback the download is free, so the
1.3 s is almost entirely **compiling ~9 MB of wasm and booting CPython**. Over a
real network a first visit additionally pays for 7.8 MB gzipped; a second visit
pays the 1.3 s again, because compilation is not what the HTTP cache stores.

### Extraction — the committed documents

| document | KB | backend | rows | first ms | warm ms |
| --- | ---: | --- | ---: | ---: | ---: |
| `cvs_1996_ownership.txt` | 1.2 | ascii | 3 | 3.5 | 3.5 |
| `dal_1994_sct.txt` | 2.6 | ascii | 15 | 6.3 | 6.0 |
| `dal_1997_sct.txt` | 3.3 | ascii | 15 | 7.8 | 7.9 |
| `aapl_2003_sct_stacked.html` | 6.7 | dom | 15 | 18.6 | 11.5 |
| `azz_2019_ownership.html` | 16.8 | dom | 5 | 30.9 | 29.3 |
| `cmp_2024_director_comp.html` | 17.3 | dom | 10 | 32.9 | 33.4 |
| `abcp_1997_review_flags.txt` | 48.1 | sgml | 6 | 66.5 | 65.0 |
| `alk_1994_sgml_sct.txt` | 56.8 | sgml | 17 | 34.9 | 33.8 |

"First" and "warm" barely differ because the runtime is warm in both — the cold
cost is preparation, which is reported separately and never folded into
`executionMs`. Extraction of a real proxy statement is tens of milliseconds.

### How large a filing this browser will take

A real proxy is 0.1–5 MB. The sweep goes far past that on purpose, appending
prose to a real filing:

| document | ok | rows found | execution |
| ---: | --- | ---: | ---: |
| 1 MB | yes | 10 | 0.57 s |
| 4 MB | yes | 10 | 2.2 s |
| 16 MB | yes | 10 | 8.8 s |
| 64 MB | yes | 10 | 34.5 s |
| 128 MB | yes | 10 | 69.0 s |
| 256 MB | yes | 10 | 32.9 s |
| **512 MB** | **no** | — | **`MemoryError` after 0.4 s** |

Findings, stated as narrowly as the evidence allows:

* **The practical limit is time, not memory.** Cost is roughly linear at
  ~0.55 s/MB up to 128 MB, so a 20 MB filing — already far larger than anything
  EDGAR serves for a proxy — costs about 11 seconds.
* **512 MB is a hard wall**, and it fails cleanly: a real Python `MemoryError`,
  converted to `error.kind: "MemoryError"` on a well-formed result, with the
  runtime still usable afterwards. wasm32 caps the heap at 4 GB, and the
  document exists several times over on the way in (JS buffer → wasm heap →
  Python `bytes` → decoded `str`).
* **256 MB was faster than 128 MB** (32.9 s vs 69.0 s) — non-monotonic, not
  investigated, and flagged here rather than smoothed over.
* Correctness does not degrade with size: the right ten directors come back out
  of a 256 MB document.

---

## Known limitations

* **No cancellation of running work.** Only termination, which costs the runtime.
  A pathological `colspan`/`rowspan` will hang the worker until it is terminated
  — this is a pre-existing library behaviour (verified in CPython: it neither
  raises nor returns), not something the bridge introduces, and it is *not*
  fixed here because the library is out of scope for this task.
* **Chromium only.** Firefox and Safari are untested. Nothing here uses
  SharedArrayBuffer or wasm threads, so they should work, but "should" is not a
  measurement.
* **Measurements are from one machine.** An M3 Pro is not a phone. Cold
  preparation on low-end hardware will be several times 1.3 s.
* **The parity expectations are generated, not hand-verified.** They pin
  browser-vs-CPython agreement. Correctness comes from the six fixtures, whose
  values a person read off the filings.
* **`test/expected/cases.json` must be regenerated when the library changes.**
  `npm run wheel` fails loudly on a checksum mismatch, which is the tripwire;
  `python3 tools/gen_expected.py` then refreshes the expectations. The generator
  imports from `../src` deliberately — an installed `sec-tables` in
  site-packages can be older than the checkout, and was during this work, which
  briefly made the browser look wrong when it was the only side running the
  current library.
* **No SEC access, and none should be added here.** The bridge takes bytes. Who
  supplies them, under what User-Agent and inside what rate limit, is a separate
  problem with a separate answer.
