/**
 * The Worker wire protocol — versioned, typed, and the only contract between
 * the page and the interpreter.
 *
 * Two rules shape it:
 *
 * 1. **Everything crossing the boundary is JSON-serializable or transferable.**
 *    A filing goes in as an ArrayBuffer (transferred, not copied); a result
 *    comes back as plain data. No PyProxy, no Date, no Python object ever
 *    reaches the page.
 * 2. **A result is a value, not an exception.** sec-tables reports "I could not
 *    find the table" as flags on a returned `Extraction`, not by raising, and
 *    that distinction survives the boundary: `ok: false` with `no_table_found`
 *    is a *finding*, while `error` is present only when Python actually raised.
 */
import { PROTOCOL_VERSION, RESULT_SCHEMA_VERSION, PROFILES } from "./pin.js";

export { PROTOCOL_VERSION, RESULT_SCHEMA_VERSION };

export type Profile = (typeof PROFILES)[number];

/** ISO `YYYY-MM-DD`. Selects the Regulation S-K era for a versioned schema. */
export type FilingDate = string;

export type SelftestFault = "value_error" | "key_error" | "runtime_error";

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

export type WorkerRequest =
  | { type: "prepare"; requestId: string }
  | {
      type: "extract";
      requestId: string;
      document: ArrayBuffer;
      filingDate: FilingDate;
      profile: Profile;
    }
  | { type: "cancel"; requestId: string }
  /** Not part of the extraction contract; used by tests to observe leaks. */
  | { type: "diagnostics"; requestId: string }
  /**
   * Fault injection, test-only. It routes through the *same* worker code that
   * wraps a real extraction, so what it exercises is the shipped error path.
   * It exists because no valid filing reaches a Python `raise`: sec-tables
   * returns anticipated failures as flags, and the inputs that might raise
   * (a `colspan` of two billion) hang the grid expansion instead.
   */
  | { type: "selftest"; requestId: string; fault: SelftestFault };

// ---------------------------------------------------------------------------
// The result
// ---------------------------------------------------------------------------

/**
 * A completed extraction attempt.
 *
 * Field-by-field this is the `Extraction` dataclass from sec_tables.types,
 * flattened. Two deliberate departures, both forced by evidence:
 *
 * - `columns` is `Extraction.table.roles` when roles were assigned and the raw
 *   header otherwise — the same precedence `Table.to_csv()` uses, so what the
 *   browser shows matches what the library writes.
 * - `flags` is the complete list; `provenance` and `reviewRequired` are the
 *   library's own partition of it. `ascii_source` on a 1997 filing is the
 *   library working, not a defect, and collapsing the two kinds would make
 *   every plain-text extraction look broken.
 */
export interface ExtractionResult {
  schemaVersion: typeof RESULT_SCHEMA_VERSION;
  ok: boolean;
  profile: string;
  era: string | null;
  backend: "dom" | "sgml" | "ascii" | "narrative" | null;
  columns: string[];
  rows: string[][];
  flags: string[];
  reviewRequired: boolean;
  provenance: string[];
  metadata: Record<string, unknown>;
  /**
   * Milliseconds *this request* spent preparing the runtime: the full cold
   * start on the first request, 0 once warm. Kept separate from executionMs so
   * a warm extraction time is never inflated by a one-time cost.
   */
  preparationMs: number;
  /** Milliseconds inside `sec_tables.extract()`, excluding preparation. */
  executionMs: number;
  error?: {
    kind: string;
    message: string;
  };
}

/** What the runtime actually loaded, reported once preparation completes. */
export interface RuntimeInfo {
  protocolVersion: typeof PROTOCOL_VERSION;
  pyodideVersion: string;
  pythonVersion: string;
  lxmlVersion: string;
  secTablesVersion: string;
  wheelSha256: string;
  availableProfiles: string[];
  /** False when this request found the runtime already prepared. */
  initializedRuntime: boolean;
}

/**
 * Leak and lifecycle counters. `liveProxies` and `livePythonExtractions` are
 * the two that matter: the first is JS holding Python objects, the second is
 * Python holding results. Both must be flat across repeated extractions.
 */
export interface Diagnostics {
  runtimeInitCount: number;
  proxiesCreated: number;
  proxiesDestroyed: number;
  liveProxies: number;
  /** Live `sec_tables.types.Extraction` objects after a gc pass. */
  livePythonExtractions: number;
  /** Keys added to the Python global namespace since preparation finished. */
  strayPythonGlobals: string[];
  /** Requests accepted and not yet answered. */
  pendingRequests: number;
  extractionsCompleted: number;
}

// ---------------------------------------------------------------------------
// Responses
// ---------------------------------------------------------------------------

export type WorkerResponse =
  | { type: "ready"; requestId: string; preparationMs: number; runtime: RuntimeInfo }
  | { type: "result"; requestId: string; result: ExtractionResult }
  | { type: "cancelled"; requestId: string }
  | { type: "diagnostics"; requestId: string; diagnostics: Diagnostics }
  /**
   * The request could not be answered at all — preparation failed, the message
   * was malformed, or an unknown type arrived. A Python exception *during*
   * extraction is not this: that comes back as a `result` with `ok: false` and
   * `error` set, because the request was answered.
   */
  | { type: "error"; requestId: string; error: { kind: string; message: string } };

// ---------------------------------------------------------------------------
// Validation — the worker trusts nothing it is sent
// ---------------------------------------------------------------------------

export function isProfile(v: unknown): v is Profile {
  return typeof v === "string" && (PROFILES as readonly string[]).includes(v);
}

/** `YYYY-MM-DD`, and a real calendar date. `2019-02-31` is rejected here. */
export function isFilingDate(v: unknown): v is FilingDate {
  if (typeof v !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
  const [y, m, d] = v.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return (
    dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d
  );
}

export function isWorkerRequest(v: unknown): v is WorkerRequest {
  if (typeof v !== "object" || v === null) return false;
  const m = v as Record<string, unknown>;
  if (typeof m.requestId !== "string" || m.requestId === "") return false;
  switch (m.type) {
    case "prepare":
    case "cancel":
    case "diagnostics":
      return true;
    case "selftest":
      return (
        m.fault === "value_error" ||
        m.fault === "key_error" ||
        m.fault === "runtime_error"
      );
    case "extract":
      return (
        m.document instanceof ArrayBuffer &&
        isFilingDate(m.filingDate) &&
        isProfile(m.profile)
      );
    default:
      return false;
  }
}

/** An empty result carrying an error, so a failure has the same shape as a success. */
export function errorResult(
  profile: string,
  kind: string,
  message: string,
  preparationMs: number,
  executionMs: number,
): ExtractionResult {
  return {
    schemaVersion: RESULT_SCHEMA_VERSION,
    ok: false,
    profile,
    era: null,
    backend: null,
    columns: [],
    rows: [],
    flags: [],
    reviewRequired: false,
    provenance: [],
    metadata: {},
    preparationMs,
    executionMs,
    error: { kind, message },
  };
}
