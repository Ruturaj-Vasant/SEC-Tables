/**
 * The sec-tables execution worker.
 *
 * One pinned Pyodide, one pinned wheel, one warm interpreter, one message
 * contract. It runs on its own thread because extraction is synchronous CPU
 * work in wasm: on the main thread a 3 MB filing would freeze the page, and
 * more importantly a pathological one would freeze it *permanently* (see
 * "Cancellation" below).
 *
 * What this worker deliberately does not do:
 *
 * - **No SEC requests.** `sec_tables.fetch` is never imported. A page cannot
 *   declare an honest SEC User-Agent, `/Archives` serves no permissive CORS,
 *   and rate limiting belongs to whatever supplies the bytes. Documents arrive
 *   as an ArrayBuffer from a proxy or a file input.
 * - **No arbitrary Python.** There is no `runPython` entry point on the
 *   protocol. The worker calls exactly one pinned function.
 * - **No dependency resolution.** lxml comes from Pyodide's own distribution
 *   (integrity-checked against pyodide-lock.json); the wheel is installed from
 *   the in-memory filesystem with `deps: false`, so nothing is ever fetched
 *   from an index.
 *
 * Cancellation: Pyodide owns this thread while Python runs, so a `cancel`
 * message sent during an extraction is not even dequeued until that extraction
 * finishes — the event loop is blocked. A queued request can therefore be
 * cancelled here; a running one can only be stopped by terminating the worker,
 * which is what the client does. This is not a shortcut: it is the only thing
 * that works, and it matters because a filing with `colspan="2000000000"`
 * expands the grid until the thread stops responding rather than raising.
 */
import {
  PROTOCOL_VERSION,
  RESULT_SCHEMA_VERSION,
  WHEEL_FILENAME,
  WHEEL_SHA256,
  PYODIDE_VERSION,
  DEFAULT_LOCATION,
  type RuntimeLocation,
} from "./pin.js";
import {
  errorResult,
  isWorkerRequest,
  type Diagnostics,
  type ExtractionResult,
  type RuntimeInfo,
  type WorkerRequest,
  type WorkerResponse,
} from "./protocol.js";
// Inlined at build time by esbuild's text loader, so the interpreter's own
// source is part of the worker bundle rather than a second network fetch that
// could be cached or substituted independently.
import BRIDGE_SOURCE from "./sec_bridge.py";

declare const self: DedicatedWorkerGlobalScope;

// ---------------------------------------------------------------------------
// PyProxy accounting
// ---------------------------------------------------------------------------

/**
 * Every PyProxy this worker creates passes through `own()`, and every one it
 * releases through `release()`. That is only meaningful because there are just
 * three creation sites in the whole file — the bridge module, the three
 * functions pinned off it, and micropip — and the steady state is a small
 * constant. `liveProxies` growing across extractions is the leak signal.
 */
interface Proxy {
  destroy(): void;
  [k: string]: any;
}

/** A Python function reached through a proxy: callable, and destroyable. */
type PyCallable = ((...args: unknown[]) => string) & Proxy;

let proxiesCreated = 0;
let proxiesDestroyed = 0;

function own<T extends Proxy>(p: T): T {
  proxiesCreated += 1;
  return p;
}

function release(p: Proxy | null | undefined): void {
  if (!p) return;
  p.destroy();
  proxiesDestroyed += 1;
}

// ---------------------------------------------------------------------------
// Runtime state
// ---------------------------------------------------------------------------

interface Runtime {
  pyodide: any;
  /** The pinned callables. Long-lived, destroyed only on teardown. */
  extractJson: PyCallable;
  runtimeInfo: PyCallable;
  diagnostics: PyCallable;
  raiseForTest: PyCallable;
  info: Omit<RuntimeInfo, "initializedRuntime">;
  /** Global namespace immediately after preparation, for stray-key detection. */
  baselineGlobals: Set<string>;
  preparationMs: number;
}

let runtime: Runtime | null = null;
/** The single in-flight preparation. Two `prepare` messages share this one. */
let preparing: Promise<Runtime> | null = null;
let runtimeInitCount = 0;
let extractionsCompleted = 0;
const pending = new Set<string>();
/** Requests cancelled before their turn came. */
const cancelled = new Set<string>();

let location: RuntimeLocation = { ...DEFAULT_LOCATION };

// ---------------------------------------------------------------------------
// Preparation
// ---------------------------------------------------------------------------

function post(message: WorkerResponse): void {
  self.postMessage(message);
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Fetch the wheel and prove it is the pinned one before the interpreter sees
 * a single byte of it. A wheel is executable code; served over a link that can
 * be tampered with, or replaced in a stale cache, it runs with the page's
 * privileges. The digest is computed with Web Crypto over the raw response.
 */
async function fetchVerifiedWheel(): Promise<Uint8Array> {
  const url = new URL(WHEEL_FILENAME, new URL(location.wheelBaseUrl, self.location.href));
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`wheel fetch failed: ${response.status} ${response.statusText} (${url})`);
  }
  const bytes = await response.arrayBuffer();
  const digest = await sha256Hex(bytes);
  if (digest !== WHEEL_SHA256) {
    throw new Error(
      `wheel checksum mismatch: expected ${WHEEL_SHA256}, got ${digest}. ` +
        `Refusing to install ${WHEEL_FILENAME}.`,
    );
  }
  return new Uint8Array(bytes);
}

async function prepareRuntime(): Promise<Runtime> {
  const started = performance.now();
  runtimeInitCount += 1;

  const pyodideBase = new URL(location.pyodideBaseUrl, self.location.href).href;
  // Non-literal so bundlers leave it alone: pyodide.mjs must resolve its own
  // wasm and lock file relative to where it is actually served from.
  const moduleUrl = `${pyodideBase}pyodide.mjs`;
  const { loadPyodide } = await import(/* @vite-ignore */ moduleUrl);

  const pyodide = await loadPyodide({
    indexURL: pyodideBase,
    packageBaseUrl: pyodideBase,
    // Nothing is written to stdout in normal operation; if Python does print,
    // it should not look like a page error.
    stdout: (line: string) => console.info("[py]", line),
    stderr: (line: string) => console.warn("[py]", line),
  });

  // lxml is a Pyodide-built package: a C extension compiled to wasm, so it can
  // only come from the distribution, never from PyPI. Pyodide verifies it
  // against the sha256 in pyodide-lock.json.
  await pyodide.loadPackage(["lxml", "micropip"]);

  const wheel = await fetchVerifiedWheel();
  // micropip resolves an `emfs:` URL by parsing the *filename*, so the name
  // must be the real wheel name, not a temp path.
  const wheelPath = `/tmp/${WHEEL_FILENAME}`;
  pyodide.FS.writeFile(wheelPath, wheel);

  const micropip = own(pyodide.pyimport("micropip"));
  try {
    // deps:false is what keeps this install closed: lxml is already present,
    // and there is nothing else in the dependency set to go looking for.
    await micropip.install(`emfs:${wheelPath}`, { deps: false });
  } finally {
    release(micropip);
  }
  pyodide.FS.unlink(wheelPath);

  // The bridge is installed as a module rather than executed into globals, so
  // the interpreter's global namespace stays exactly as Pyodide left it.
  const sitePackages = pyodide.runPython("import site; site.getsitepackages()[0]");
  pyodide.FS.writeFile(`${sitePackages}/sec_bridge.py`, BRIDGE_SOURCE);

  const bridge = own(pyodide.pyimport("sec_bridge"));
  // `.copy()` matters: an attribute read off a module proxy is *borrowed* from
  // it, so destroying the module would destroy the four functions with it and
  // the first extraction would fail on a dead proxy. The copies outlive their
  // parent, which is then released immediately — the module object itself is
  // never needed again.
  const pin = (name: string): PyCallable => own(bridge[name].copy()) as PyCallable;
  let extractJson: PyCallable;
  let runtimeInfoFn: PyCallable;
  let diagnosticsFn: PyCallable;
  let raiseForTest: PyCallable;
  try {
    extractJson = pin("extract_json");
    runtimeInfoFn = pin("runtime_info");
    diagnosticsFn = pin("diagnostics");
    raiseForTest = pin("raise_for_test");
  } finally {
    release(bridge);
  }

  const pyInfo = JSON.parse(runtimeInfoFn());
  const preparationMs = performance.now() - started;

  return {
    pyodide,
    extractJson,
    runtimeInfo: runtimeInfoFn,
    diagnostics: diagnosticsFn,
    raiseForTest,
    info: {
      protocolVersion: PROTOCOL_VERSION,
      pyodideVersion: PYODIDE_VERSION,
      pythonVersion: pyInfo.pythonVersion,
      lxmlVersion: pyInfo.lxmlVersion,
      secTablesVersion: pyInfo.secTablesVersion,
      wheelSha256: WHEEL_SHA256,
      availableProfiles: pyInfo.availableProfiles,
    },
    baselineGlobals: new Set<string>(JSON.parse(diagnosticsFn()).mainGlobals),
    preparationMs,
  };
}

/**
 * Prepare once, however many callers ask.
 *
 * The promise is memoised *before* the first await, so two `prepare` messages
 * arriving in the same tick — or an `extract` racing a `prepare` — join the
 * same initialisation instead of starting two interpreters. Two Pyodide
 * instances in one worker would each hold ~20 MB of wasm heap and silently
 * halve the memory available to the larger filings.
 */
function ensureRuntime(): Promise<Runtime> {
  if (runtime) return Promise.resolve(runtime);
  if (!preparing) {
    preparing = prepareRuntime()
      .then((r) => {
        runtime = r;
        return r;
      })
      .catch((err) => {
        // A failed preparation must not poison the worker: the next request is
        // allowed to try again (the wheel server may simply have been down).
        preparing = null;
        throw err;
      });
  }
  return preparing;
}

// ---------------------------------------------------------------------------
// Request handling
// ---------------------------------------------------------------------------

function describeError(err: unknown): { kind: string; message: string } {
  if (err && typeof err === "object") {
    const e = err as any;
    // Pyodide surfaces a Python exception as a PythonError carrying the Python
    // class name in `.type`. Reporting that instead of "PythonError" is the
    // difference between an actionable message and a generic one.
    if (typeof e.type === "string" && e.name === "PythonError") {
      const message = String(e.message ?? "").trim();
      // The full traceback is useful, the last line is what a caller reads.
      const lastLine = message.split("\n").filter(Boolean).pop() ?? message;
      return { kind: e.type, message: lastLine };
    }
    if (e instanceof Error) return { kind: e.name, message: e.message };
  }
  return { kind: "Error", message: String(err) };
}

/**
 * Run one pinned Python call and answer with a well-formed result either way.
 *
 * Both `extract` and `selftest` come through here, so the error conversion the
 * suite exercises with an injected fault is the same code a real Python
 * exception would take.
 */
async function answerWithResult(
  requestId: string,
  profile: string,
  call: (rt: Runtime) => string,
): Promise<void> {
  let preparationMs = 0;
  let rt: Runtime;
  try {
    const alreadyWarm = runtime !== null;
    const startedWaiting = performance.now();
    rt = await ensureRuntime();
    preparationMs = alreadyWarm ? 0 : performance.now() - startedWaiting;
  } catch (err) {
    const { kind, message } = describeError(err);
    post({ type: "error", requestId, error: { kind, message } });
    return;
  }

  if (cancelled.delete(requestId)) {
    post({ type: "cancelled", requestId });
    return;
  }

  const started = performance.now();
  try {
    const json = call(rt);
    const executionMs = performance.now() - started;
    const result = JSON.parse(json) as ExtractionResult;
    result.schemaVersion = RESULT_SCHEMA_VERSION;
    result.preparationMs = preparationMs;
    result.executionMs = executionMs;
    extractionsCompleted += 1;
    post({ type: "result", requestId, result });
  } catch (err) {
    const { kind, message } = describeError(err);
    post({
      type: "result",
      requestId,
      result: errorResult(profile, kind, message, preparationMs, performance.now() - started),
    });
  }
}

function collectDiagnostics(requestId: string): Diagnostics {
  let livePythonExtractions = 0;
  let strayPythonGlobals: string[] = [];
  if (runtime) {
    const py = JSON.parse(runtime.diagnostics());
    livePythonExtractions = py.liveExtractions;
    strayPythonGlobals = (py.mainGlobals as string[]).filter(
      (k) => !runtime!.baselineGlobals.has(k),
    );
  }
  return {
    runtimeInitCount,
    proxiesCreated,
    proxiesDestroyed,
    liveProxies: proxiesCreated - proxiesDestroyed,
    livePythonExtractions,
    strayPythonGlobals,
    // Excluding this request, which is by definition still in flight.
    pendingRequests: [...pending].filter((id) => id !== requestId).length,
    extractionsCompleted,
  };
}

async function handle(request: WorkerRequest): Promise<void> {
  switch (request.type) {
    case "prepare": {
      try {
        const alreadyWarm = runtime !== null;
        // Whoever finds no runtime *and* no preparation under way is the one
        // that starts it. A second caller in the same tick also sees no
        // runtime, but it joins rather than initialises, and reporting both as
        // initialisers would hide exactly the thing this flag is for.
        const startedInitialization = !alreadyWarm && preparing === null;
        const startedWaiting = performance.now();
        const rt = await ensureRuntime();
        post({
          type: "ready",
          requestId: request.requestId,
          preparationMs: alreadyWarm ? 0 : performance.now() - startedWaiting,
          runtime: { ...rt.info, initializedRuntime: startedInitialization },
        });
      } catch (err) {
        post({ type: "error", requestId: request.requestId, error: describeError(err) });
      }
      return;
    }

    case "extract": {
      const bytes = new Uint8Array(request.document);
      await answerWithResult(request.requestId, request.profile, (rt) =>
        rt.extractJson(bytes, request.filingDate, request.profile),
      );
      return;
    }

    case "selftest": {
      await answerWithResult(request.requestId, "selftest", (rt) =>
        rt.raiseForTest(request.fault),
      );
      return;
    }

    case "cancel": {
      // Only reachable for a request still queued behind another: while Python
      // runs, this thread cannot process messages at all.
      cancelled.add(request.requestId);
      post({ type: "cancelled", requestId: request.requestId });
      return;
    }

    case "diagnostics": {
      post({
        type: "diagnostics",
        requestId: request.requestId,
        diagnostics: collectDiagnostics(request.requestId),
      });
      return;
    }
  }
}

self.onmessage = (event: MessageEvent) => {
  const request = event.data;

  // Point the worker at a different asset root. Accepted only before anything
  // is loaded, so it can never appear mid-session and swap the origin the
  // interpreter came from. Deliberately not a `WorkerRequest`: it is host
  // configuration, not a unit of work, and it is never answered.
  if (request?.type === "configure") {
    if (!runtime && !preparing) {
      location = {
        pyodideBaseUrl: request.pyodideBaseUrl ?? location.pyodideBaseUrl,
        wheelBaseUrl: request.wheelBaseUrl ?? location.wheelBaseUrl,
      };
    }
    return;
  }

  if (!isWorkerRequest(request)) {
    const requestId =
      typeof request?.requestId === "string" ? request.requestId : "unknown";
    post({
      type: "error",
      requestId,
      error: { kind: "ProtocolError", message: `malformed request: ${JSON.stringify(request)}` },
    });
    return;
  }
  pending.add(request.requestId);
  void handle(request).finally(() => {
    pending.delete(request.requestId);
    cancelled.delete(request.requestId);
  });
};
