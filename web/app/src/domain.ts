/**
 * The parts of the application that are decisions rather than rendering.
 *
 * Everything here is pure and unit-tested without a browser: form validation,
 * the proxy's error vocabulary, the review/provenance split, CSV generation.
 * They live outside the components because each one is a place where being
 * subtly wrong produces a plausible-looking wrong answer rather than a crash.
 */
import type { ExtractionResult, Profile } from "../../src/protocol.js";

// ---------------------------------------------------------------------------
// The form
// ---------------------------------------------------------------------------

export const FORMS = ["DEF 14A", "DEFA14A", "DEFR14A", "10-K"] as const;
export type Form = (typeof FORMS)[number];

export const TABLES: ReadonlyArray<{ value: Profile; label: string; item: string }> = [
  { value: "summary_compensation", label: "Summary Compensation", item: "Item 402(c)/(b)" },
  { value: "director_compensation", label: "Director Compensation", item: "Item 402(r)" },
  { value: "beneficial_ownership", label: "Beneficial Ownership", item: "Item 403" },
];

/** EDGAR's own electronic coverage starts here. Before it there is nothing to fetch. */
export const MIN_YEAR = 1993;

export interface FormValues {
  email: string;
  ticker: string;
  year: string;
  form: Form;
  table: Profile;
}

export type FieldErrors = Partial<Record<keyof FormValues, string>>;

/**
 * Mirrors the proxy's validation deliberately.
 *
 * Not duplication for its own sake: the server must validate because a client
 * can be bypassed, and the client must validate because a round trip to say
 * "that is not an email" is a bad way to learn it. The server remains the
 * authority — this only decides whether it is worth asking.
 */
const EMAIL_RE =
  /^[^@\s,;<>()[\]\\]+@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$/;
const TICKER_RE = /^[A-Za-z0-9.-]{1,12}$/;

export function validateForm(values: FormValues, now = new Date()): FieldErrors {
  const errors: FieldErrors = {};

  const email = values.email.trim();
  if (!email) errors.email = "A contact address is needed: this app sends it to SEC as the contact for your request.";
  else if (!EMAIL_RE.test(email)) errors.email = "That is not a usable email address.";
  else if (email.length > 254) errors.email = "That email address is too long.";

  const ticker = values.ticker.trim();
  if (!ticker) errors.ticker = "A ticker is required.";
  else if (!TICKER_RE.test(ticker)) errors.ticker = "A ticker is letters, digits, dot or dash.";

  const year = Number(values.year);
  if (!values.year.trim()) errors.year = "A filing year is required.";
  else if (!Number.isInteger(year)) errors.year = "That is not a year.";
  else if (year < MIN_YEAR) errors.year = `EDGAR's electronic filings start in ${MIN_YEAR}.`;
  else if (year > now.getFullYear()) errors.year = "That year has not happened yet.";

  if (!FORMS.includes(values.form)) errors.form = "Unsupported form.";
  if (!TABLES.some((t) => t.value === values.table)) errors.table = "Unsupported table.";

  return errors;
}

export const isValid = (errors: FieldErrors): boolean => Object.keys(errors).length === 0;

// ---------------------------------------------------------------------------
// The proxy's vocabulary
// ---------------------------------------------------------------------------

export type ProxyErrorKind =
  | "invalid_input"
  | "not_found"
  | "throttled"
  | "upstream_failure"
  | "internal"
  | "offline";

export interface ProxyErrorShape {
  kind: ProxyErrorKind;
  message: string;
}

export interface FilingSummary {
  id: string;
  ticker: string;
  cik: string | null;
  form: string;
  filingDate: string;
  year: number;
  route: "complete_submission" | "primary_document";
}

export interface FilingListResponse {
  ticker: string;
  year: number;
  form: string;
  filings: FilingSummary[];
  defaultId: string | null;
}

export interface FilingMeta extends FilingSummary {
  sourceUrl: string;
}

/** Runtime shape checks. A response that does not match is a failure, not a `?.`. */
export function parseFilingList(value: unknown): FilingListResponse {
  const v = value as FilingListResponse;
  if (!v || !Array.isArray(v.filings)) throw new Error("proxy returned no filing list");
  for (const f of v.filings) {
    if (typeof f?.id !== "string" || typeof f?.filingDate !== "string") {
      throw new Error("proxy returned a malformed filing entry");
    }
  }
  return v;
}

export function parseFilingMeta(header: string | null): FilingMeta {
  if (!header) throw new Error("proxy returned a filing with no metadata");
  const meta = JSON.parse(header) as FilingMeta;
  if (typeof meta.filingDate !== "string" || typeof meta.form !== "string") {
    throw new Error("proxy returned malformed filing metadata");
  }
  return meta;
}

/**
 * What a person should be told, per error kind.
 *
 * `throttled` gets its own text because it is the only one where the right
 * response is to wait rather than to change something: SEC's published recovery
 * is ten minutes below the threshold, and the app shares one budget across
 * everyone using it.
 */
export function describeProxyError(error: ProxyErrorShape): string {
  switch (error.kind) {
    case "throttled":
      return "SEC is rate-limiting this server. Everyone using this page shares one request budget; wait a minute and try again.";
    case "not_found":
      return error.message;
    case "invalid_input":
      return error.message;
    case "offline":
      return "Could not reach the filing server.";
    case "upstream_failure":
    case "internal":
    default:
      return `SEC request failed: ${error.message}`;
  }
}

// ---------------------------------------------------------------------------
// Flags
// ---------------------------------------------------------------------------

/**
 * The library's own partition, mirrored from `sec_tables.types`.
 *
 * Keeping them apart is the point. `ascii_source` on a 1997 filing means the
 * library did the hard thing correctly; showing it next to
 * `missing_required_columns` under one heading would teach a reader to ignore
 * both.
 */
export const REVIEW_FLAGS = new Set([
  "ambiguous_selection",
  "missing_required_columns",
  "unmapped_columns",
  "era_mismatch",
  "below_score_threshold",
  "suspect_identity_values",
]);

export const PROVENANCE_FLAGS = new Set([
  "ascii_source",
  "sgml_source",
  "no_filing_date",
  "predates_mandate",
]);

export const FLAG_TEXT: Record<string, string> = {
  ambiguous_selection: "Two candidate tables scored identically — the choice was a tie-break.",
  missing_required_columns: "Columns this era's rules require were not found.",
  unmapped_columns: "A column header did not map to a known field.",
  era_mismatch: "The filing date and the columns present disagree.",
  below_score_threshold: "The best candidate was too weak to trust.",
  suspect_identity_values: "The name column holds addresses or footnote text — row alignment drifted.",
  ascii_source: "Read from a space-aligned plain-text table (pre-2001 EDGAR).",
  sgml_source: "Read from an SGML <TABLE> block with no row or cell tags.",
  no_filing_date: "No filing date was supplied, so the schema era could not be pinned.",
  predates_mandate: "This disclosure was not required as of the filing date.",
  no_table_found: "No candidate table cleared the score threshold.",
  no_data_rows: "A table was selected but held no data rows.",
  tabulation_failed: "The selected table could not be turned into a grid.",
  assembly_emptied_table: "Row assembly removed every row.",
};

export interface FlagPartition {
  review: string[];
  provenance: string[];
  other: string[];
}

export function partitionFlags(result: ExtractionResult): FlagPartition {
  const review: string[] = [];
  const provenance: string[] = [];
  const other: string[] = [];
  for (const flag of result.flags) {
    if (REVIEW_FLAGS.has(flag)) review.push(flag);
    else if (PROVENANCE_FLAGS.has(flag)) provenance.push(flag);
    else other.push(flag);
  }
  return { review, provenance, other };
}

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------

/**
 * RFC 4180 quoting over the columns and rows that are actually on screen.
 *
 * Generated from the displayed values rather than re-derived, so a downloaded
 * file cannot disagree with what was read. Short rows are padded: a ragged CSV
 * silently shifts every later column.
 */
export function toCsv(columns: string[], rows: string[][]): string {
  const cell = (value: string): string => {
    const v = value ?? "";
    return /[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  };
  const width = columns.length;
  const lines = [columns.map(cell).join(",")];
  for (const row of rows) {
    const padded = width > row.length ? [...row, ...Array(width - row.length).fill("")] : row.slice(0, width);
    lines.push(padded.map(cell).join(","));
  }
  return lines.join("\n") + "\n";
}

export function csvFilename(meta: FilingMeta | null, profile: string): string {
  const ticker = meta?.ticker ?? "filing";
  const date = meta?.filingDate ?? "unknown-date";
  return `${ticker}_${date}_${profile}.csv`.toLowerCase();
}

// ---------------------------------------------------------------------------
// Filing display
// ---------------------------------------------------------------------------

/**
 * Whether a filing should be shown as markup or as preserved plain text.
 *
 * Sniffed from the bytes, not from the file extension: a pre-2001
 * complete-submission `.txt` is where the ASCII and SGML backends earn their
 * keep, and rendering one as HTML collapses the column alignment that *is* the
 * table.
 */
export function looksLikeHtml(bytes: Uint8Array): boolean {
  const head = new TextDecoder("utf-8", { fatal: false })
    .decode(bytes.subarray(0, 8192))
    .toLowerCase();
  // `<td` and `<tr` are the discriminator that matters, and it is the same one
  // the library's SGML backend exists for: an old EDGAR <TABLE> block has no
  // row or cell tags at all — its columns are runs of spaces. So a document
  // containing `<table>` is only HTML if it also closes rows and cells.
  return /<html|<!doctype html|<body|<div|<font|<span|<p[\s>]|<t[dr][\s>]/.test(head);
}

export function describeRoute(route: string): string {
  return route === "complete_submission"
    ? "complete submission text file — the pre-May-2000 route"
    : "primary document";
}
