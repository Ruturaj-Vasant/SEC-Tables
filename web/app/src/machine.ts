/**
 * The application's state, as a reducer rather than a pile of booleans.
 *
 * Written as one enumerated status because the twelve states are genuinely
 * exclusive and several are easy to conflate in a way a user would feel:
 * "fetching the filing from SEC" and "starting Python" look the same from a
 * spinner but have completely different causes when they stall, and
 * "extracted, with warnings" is not a milder kind of success — it is a result
 * that should not be used without a look.
 */
import type { ExtractionResult } from "../../src/protocol.js";
import type { FieldErrors, FilingMeta, FilingSummary, FormValues } from "./domain.js";

export type Status =
  | "empty"
  | "validating"
  | "finding_filings"
  | "fetching_filing"
  | "preparing_python"
  | "extracting"
  | "successful"
  | "needs_review"
  | "no_table"
  | "throttled"
  | "cancelled"
  | "failed";

/** Statuses where work is in flight, so Cancel is meaningful and Extract is not. */
export const BUSY: ReadonlySet<Status> = new Set<Status>([
  "validating",
  "finding_filings",
  "fetching_filing",
  "preparing_python",
  "extracting",
]);

export const STATUS_TEXT: Record<Status, string> = {
  empty: "Enter a ticker and a year to begin.",
  validating: "Checking the form…",
  finding_filings: "Asking SEC which filings exist…",
  fetching_filing: "Downloading the filing from SEC…",
  preparing_python: "Starting Python in your browser…",
  extracting: "Finding and reading the table…",
  successful: "Extracted.",
  needs_review: "Extracted, but this result asks for review.",
  no_table: "No table of that type was found in this filing.",
  throttled: "SEC is rate-limiting this server.",
  cancelled: "Cancelled.",
  failed: "Something went wrong.",
};

export interface AppState {
  status: Status;
  values: FormValues;
  errors: FieldErrors;
  /** Every filing matching ticker/year/form. More than one is normal and shown. */
  filings: FilingSummary[];
  selectedFilingId: string | null;
  filingMeta: FilingMeta | null;
  filingBytes: Uint8Array | null;
  filingCached: boolean;
  result: ExtractionResult | null;
  /** What went wrong, in the words a person should read. */
  message: string | null;
  /** True when the last failure was the extraction timeout, which rebuilt the Worker. */
  recoveredFromTimeout: boolean;
}

export type Action =
  | { type: "field"; name: keyof FormValues; value: string }
  | { type: "validated"; errors: FieldErrors }
  | { type: "finding" }
  | { type: "found"; filings: FilingSummary[]; defaultId: string | null }
  | { type: "select_filing"; id: string }
  | { type: "fetching" }
  | { type: "fetched"; meta: FilingMeta; bytes: Uint8Array; cached: boolean }
  | { type: "preparing" }
  | { type: "extracting" }
  | { type: "extracted"; result: ExtractionResult }
  | { type: "cancelled" }
  | { type: "timed_out"; message: string }
  | { type: "failed"; status: Extract<Status, "throttled" | "failed">; message: string };

export const initialState = (values: FormValues): AppState => ({
  status: "empty",
  values,
  errors: {},
  filings: [],
  selectedFilingId: null,
  filingMeta: null,
  filingBytes: null,
  filingCached: false,
  result: null,
  message: null,
  recoveredFromTimeout: false,
});

export function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "field": {
      // Editing a field invalidates the *result*, not the filing: changing the
      // table to extract should not re-download a document already in hand.
      const values = { ...state.values, [action.name]: action.value };
      const identityChanged =
        action.name === "ticker" || action.name === "year" || action.name === "form";
      return {
        ...state,
        values,
        errors: { ...state.errors, [action.name]: undefined },
        status: BUSY.has(state.status) ? state.status : "empty",
        result: null,
        message: null,
        ...(identityChanged
          ? { filings: [], selectedFilingId: null, filingMeta: null, filingBytes: null }
          : {}),
      };
    }

    case "validated":
      return {
        ...state,
        errors: action.errors,
        status: Object.keys(action.errors).length ? "empty" : state.status,
      };

    case "finding":
      return { ...state, status: "finding_filings", message: null, result: null, errors: {} };

    case "found":
      return {
        ...state,
        status: "empty",
        filings: action.filings,
        // Default to the latest, which is `pick_filing`'s default and for the
        // same reason: a second proxy in one year is usually a correction.
        selectedFilingId: state.selectedFilingId ?? action.defaultId,
      };

    case "select_filing":
      return {
        ...state,
        selectedFilingId: action.id,
        // A different filing means the document on screen and the table under
        // it both belong to something else now.
        filingMeta: null,
        filingBytes: null,
        result: null,
        status: "empty",
      };

    case "fetching":
      return { ...state, status: "fetching_filing", message: null, result: null };

    case "fetched":
      return {
        ...state,
        status: "empty",
        filingMeta: action.meta,
        filingBytes: action.bytes,
        filingCached: action.cached,
      };

    case "preparing":
      return { ...state, status: "preparing_python", message: null };

    case "extracting":
      return { ...state, status: "extracting", message: null, recoveredFromTimeout: false };

    case "extracted": {
      const result = action.result;
      if (result.error) {
        return { ...state, status: "failed", result, message: `${result.error.kind}: ${result.error.message}` };
      }
      if (!result.ok) return { ...state, status: "no_table", result, message: null };
      // A result with review flags is NOT reported as plain success. Extraction
      // succeeding says a table was found and parsed; it says nothing about
      // whether it is the right table or the values are right.
      return {
        ...state,
        status: result.reviewRequired ? "needs_review" : "successful",
        result,
        message: null,
      };
    }

    case "cancelled":
      return { ...state, status: "cancelled", message: null };

    case "timed_out":
      return { ...state, status: "failed", message: action.message, recoveredFromTimeout: true };

    case "failed":
      return { ...state, status: action.status, message: action.message };

    default:
      return state;
  }
}

/** The filing currently chosen, or null when the listing has not run. */
export function selectedFiling(state: AppState): FilingSummary | null {
  return state.filings.find((f) => f.id === state.selectedFilingId) ?? null;
}

/** Extraction needs a document; the button says so rather than failing on click. */
export function canExtract(state: AppState): boolean {
  return !BUSY.has(state.status) && state.filingBytes !== null;
}

export function canFind(state: AppState): boolean {
  return !BUSY.has(state.status);
}
