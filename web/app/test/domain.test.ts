/**
 * The decisions, tested without a browser.
 *
 * These run under `node --test` after esbuild compiles them, so they are fast
 * enough to run on every change — which matters, because every function here is
 * one where being subtly wrong produces a plausible-looking wrong answer rather
 * than a crash.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  csvFilename,
  describeProxyError,
  isValid,
  looksLikeHtml,
  parseFilingList,
  parseFilingMeta,
  partitionFlags,
  toCsv,
  validateForm,
  type FormValues,
} from "../src/domain.js";
import { reducer, initialState, canExtract, canFind, BUSY, type AppState } from "../src/machine.js";
import type { ExtractionResult } from "../../src/protocol.js";

const NOW = new Date("2026-07-31T00:00:00Z");

const form = (over: Partial<FormValues> = {}): FormValues => ({
  email: "researcher@example.com",
  ticker: "DAL",
  year: "1997",
  form: "DEF 14A",
  table: "summary_compensation",
  ...over,
});

const extraction = (over: Partial<ExtractionResult> = {}): ExtractionResult => ({
  schemaVersion: 1,
  ok: true,
  profile: "summary_compensation",
  era: "pre2006",
  backend: "ascii",
  columns: ["name", "year", "salary"],
  rows: [["Ronald W. Allen", "1997", "562500"]],
  flags: ["ascii_source"],
  reviewRequired: false,
  provenance: ["ascii_source"],
  metadata: { candidates: 2, profile: "summary_compensation" },
  preparationMs: 0,
  executionMs: 7.5,
  ...over,
});

// ---------------------------------------------------------------------------
// Form validation
// ---------------------------------------------------------------------------

test("a complete form validates", () => {
  assert.ok(isValid(validateForm(form(), NOW)));
});

test("the missing-email message does not claim SEC demands the visitor's address", () => {
  // The requirement SEC states is on the *requester*: an application declaring
  // itself with a monitored contact. Asking each visitor for theirs is this
  // app's choice, and the copy must not launder that into a regulation.
  const message = validateForm(form({ email: "" }), NOW).email!;
  assert.doesNotMatch(message.toLowerCase(), /sec requires/);
  assert.match(message, /this app sends it to SEC/);
});

test("email rejects what SEC could not contact anyone at", () => {
  for (const bad of ["", "   ", "nope", "@example.com", "user@", "user@nodot", "a b@c.com"]) {
    const errors = validateForm(form({ email: bad }), NOW);
    assert.ok(errors.email, `${bad} should have been rejected`);
  }
});

test("email accepts ordinary addresses including plus-tags and subdomains", () => {
  for (const good of ["a@b.co", "first.last+tag@sub.example.org", "x@y.example"]) {
    assert.equal(validateForm(form({ email: good }), NOW).email, undefined);
  }
});

test("ticker rejects anything that is not a symbol", () => {
  assert.ok(validateForm(form({ ticker: "" }), NOW).ticker);
  assert.ok(validateForm(form({ ticker: "../../etc" }), NOW).ticker);
  assert.equal(validateForm(form({ ticker: "BRK.B" }), NOW).ticker, undefined);
});

test("year is bounded by EDGAR's coverage at one end and today at the other", () => {
  assert.ok(validateForm(form({ year: "1899" }), NOW).year, "before EDGAR");
  assert.ok(validateForm(form({ year: "2099" }), NOW).year, "in the future");
  assert.ok(validateForm(form({ year: "abc" }), NOW).year);
  assert.equal(validateForm(form({ year: "1993" }), NOW).year, undefined);
});

// ---------------------------------------------------------------------------
// API result shapes
// ---------------------------------------------------------------------------

test("a filing list must actually be a filing list", () => {
  const good = parseFilingList({
    ticker: "DAL", year: 1997, form: "DEF 14A", defaultId: "b",
    filings: [{ id: "a", filingDate: "1997-09-19" }, { id: "b", filingDate: "1997-10-24" }],
  });
  assert.equal(good.filings.length, 2);
  assert.throws(() => parseFilingList({}), /no filing list/);
  assert.throws(() => parseFilingList({ filings: [{ id: 1 }] }), /malformed filing entry/);
});

test("filing metadata missing from the response is an error, not an empty object", () => {
  assert.throws(() => parseFilingMeta(null), /no metadata/);
  assert.throws(() => parseFilingMeta(JSON.stringify({ ticker: "DAL" })), /malformed/);
  const meta = parseFilingMeta(JSON.stringify({
    id: "a", ticker: "DAL", cik: "27904", form: "DEF 14A",
    filingDate: "1997-09-19", year: 1997, route: "complete_submission",
    sourceUrl: "https://www.sec.gov/Archives/x.txt",
  }));
  assert.equal(meta.filingDate, "1997-09-19");
});

// ---------------------------------------------------------------------------
// SEC error interpretation
// ---------------------------------------------------------------------------

test("throttling is explained as waiting, not as a mistake the user made", () => {
  const text = describeProxyError({ kind: "throttled", message: "429" });
  assert.match(text, /rate-limiting/);
  assert.match(text, /shares one request budget/);
});

test("input and not-found errors are passed through in the server's own words", () => {
  assert.equal(describeProxyError({ kind: "not_found", message: "no CIK for XYZ" }), "no CIK for XYZ");
  assert.equal(describeProxyError({ kind: "invalid_input", message: "bad year" }), "bad year");
});

test("an unreachable proxy is named as such rather than blamed on SEC", () => {
  assert.match(describeProxyError({ kind: "offline", message: "connect" }), /filing server/);
});

// ---------------------------------------------------------------------------
// Flag partitioning
// ---------------------------------------------------------------------------

test("provenance never lands in the review bucket", () => {
  const parts = partitionFlags(extraction({
    flags: ["ascii_source", "ambiguous_selection", "sgml_source", "unmapped_columns"],
  }));
  assert.deepEqual(parts.review, ["ambiguous_selection", "unmapped_columns"]);
  assert.deepEqual(parts.provenance, ["ascii_source", "sgml_source"]);
  assert.deepEqual(parts.other, []);
});

test("a flag that is neither is kept rather than dropped", () => {
  const parts = partitionFlags(extraction({ ok: false, flags: ["no_table_found"] }));
  assert.deepEqual(parts.other, ["no_table_found"]);
  assert.deepEqual(parts.review, []);
});

test("every review flag the library defines is classified as one", () => {
  // The list is mirrored from sec_tables.types.REVIEW_FLAGS; if the library adds
  // one and this is not updated, the new flag would render as "other" and read
  // as harmless.
  const parts = partitionFlags(extraction({
    flags: [
      "ambiguous_selection", "missing_required_columns", "unmapped_columns",
      "era_mismatch", "below_score_threshold", "suspect_identity_values",
    ],
  }));
  assert.equal(parts.review.length, 6);
  assert.equal(parts.other.length, 0);
});

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------

test("csv quotes commas, quotes and newlines", () => {
  const csv = toCsv(["name", "note"], [["Allen, Ronald", 'said "hi"'], ["x", "a\nb"]]);
  assert.equal(
    csv,
    'name,note\n"Allen, Ronald","said ""hi"""\nx,"a\nb"\n',
  );
});

test("csv preserves an empty cell as empty, never as zero", () => {
  // Missing is not zero is a library invariant; a CSV that writes 0 would break
  // it on the way out.
  const csv = toCsv(["a", "b"], [["", "5"]]);
  assert.equal(csv, "a,b\n,5\n");
});

test("csv pads short rows instead of shifting later columns", () => {
  assert.equal(toCsv(["a", "b", "c"], [["1"]]), "a,b,c\n1,,\n");
});

test("csv truncates over-long rows rather than widening the header", () => {
  assert.equal(toCsv(["a"], [["1", "2"]]), "a\n1\n");
});

test("the csv filename identifies the filing it came from", () => {
  const meta = {
    id: "a", ticker: "DAL", cik: "27904", form: "DEF 14A", filingDate: "1997-09-19",
    year: 1997, route: "complete_submission" as const, sourceUrl: "https://www.sec.gov/x",
  };
  assert.equal(csvFilename(meta, "summary_compensation"), "dal_1997-09-19_summary_compensation.csv");
});

// ---------------------------------------------------------------------------
// Filing metadata interpretation
// ---------------------------------------------------------------------------

test("an SGML plain-text filing is not mistaken for HTML", () => {
  const sgml = new TextEncoder().encode("<TABLE>\n<CAPTION>\n   NAME      SALARY\n");
  assert.equal(looksLikeHtml(sgml), false, "a <TABLE> block is not an HTML document");
  const html = new TextEncoder().encode("<html><body><div>hi</div></body></html>");
  assert.equal(looksLikeHtml(html), true);
});

test("a modern filing that starts with a comment is still HTML", () => {
  const html = new TextEncoder().encode("<!-- generated -->\n<!DOCTYPE HTML>\n<html>");
  assert.equal(looksLikeHtml(html), true);
});

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

const start = (): AppState => initialState(form());

test("a result with review flags is not reported as plain success", () => {
  const state = reducer(start(), {
    type: "extracted",
    result: extraction({ reviewRequired: true, flags: ["ambiguous_selection"] }),
  });
  assert.equal(state.status, "needs_review");
});

test("no table found is its own state, not a failure", () => {
  const state = reducer(start(), {
    type: "extracted",
    result: extraction({ ok: false, flags: ["no_table_found"], rows: [], columns: [] }),
  });
  assert.equal(state.status, "no_table");
  assert.equal(state.message, null);
});

test("a python exception is a failure and says which exception", () => {
  const state = reducer(start(), {
    type: "extracted",
    result: extraction({ ok: false, error: { kind: "MemoryError", message: "out of memory" } }),
  });
  assert.equal(state.status, "failed");
  assert.match(state.message!, /MemoryError/);
});

test("a timeout is recorded as having rebuilt the runtime", () => {
  const state = reducer(start(), { type: "timed_out", message: "stopped after 30s" });
  assert.equal(state.status, "failed");
  assert.equal(state.recoveredFromTimeout, true);
});

test("throttling keeps its own status so the UI can say wait", () => {
  const state = reducer(start(), { type: "failed", status: "throttled", message: "slow down" });
  assert.equal(state.status, "throttled");
});

test("changing the ticker discards the filing; changing the table does not", () => {
  let state = reducer(start(), {
    type: "fetched",
    meta: { id: "a", ticker: "DAL", cik: "1", form: "DEF 14A", filingDate: "1997-09-19", year: 1997, route: "complete_submission", sourceUrl: "u" },
    bytes: new Uint8Array([1, 2, 3]),
    cached: false,
  });
  assert.ok(state.filingBytes);

  const afterTable = reducer(state, { type: "field", name: "table", value: "beneficial_ownership" });
  assert.ok(afterTable.filingBytes, "re-reading a different table must not re-download the filing");

  const afterTicker = reducer(state, { type: "field", name: "ticker", value: "AAPL" });
  assert.equal(afterTicker.filingBytes, null, "a different company is a different document");
  assert.deepEqual(afterTicker.filings, []);
});

test("selecting a different filing clears the document and the result", () => {
  let state = reducer(start(), {
    type: "found",
    filings: [
      { id: "a", ticker: "DAL", cik: "1", form: "DEF 14A", filingDate: "1997-09-19", year: 1997, route: "complete_submission" },
      { id: "b", ticker: "DAL", cik: "1", form: "DEF 14A", filingDate: "1997-10-24", year: 1997, route: "complete_submission" },
    ],
    defaultId: "b",
  });
  assert.equal(state.selectedFilingId, "b", "the latest is the default, as pick_filing does");
  state = reducer(state, { type: "extracted", result: extraction() });
  state = reducer(state, { type: "select_filing", id: "a" });
  assert.equal(state.result, null);
  assert.equal(state.filingBytes, null);
});

test("extract is unavailable until a document is in hand", () => {
  const state = start();
  assert.equal(canExtract(state), false);
  const withDoc = reducer(state, {
    type: "fetched",
    meta: { id: "a", ticker: "DAL", cik: "1", form: "DEF 14A", filingDate: "1997-09-19", year: 1997, route: "complete_submission", sourceUrl: "u" },
    bytes: new Uint8Array([1]),
    cached: true,
  });
  assert.equal(canExtract(withDoc), true);
});

test("nothing can be started while something is in flight", () => {
  for (const status of BUSY) {
    const state = { ...start(), status, filingBytes: new Uint8Array([1]) };
    assert.equal(canFind(state), false, status);
    assert.equal(canExtract(state), false, status);
  }
});

test("editing a field does not interrupt work already running", () => {
  const busy = { ...start(), status: "extracting" as const };
  assert.equal(reducer(busy, { type: "field", name: "email", value: "x@y.zz" }).status, "extracting");
});
