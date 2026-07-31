/**
 * The whole application: a form, a filing, a table.
 *
 * The split it exists to demonstrate is that **fetching is server-side and
 * extraction is browser-side**. The bytes come from a proxy because a page
 * cannot fetch from `sec.gov` (no permissive CORS) and cannot honestly identify
 * itself to SEC (it does not control its User-Agent). The extraction runs here
 * because it needs no network at all — the library is bytes in, table out.
 */
import * as React from "react";
import { SecTablesBridge, CancelledError } from "../../src/client.js";
import type { Profile } from "../../src/protocol.js";
import { fetchFiling, listFilings, ProxyError } from "./api.js";
import {
  describeProxyError,
  isValid,
  validateForm,
  type FormValues,
} from "./domain.js";
import { ExtractionTimeout, runExtraction } from "./extraction.js";
import {
  BUSY,
  STATUS_TEXT,
  canExtract as canExtractNow,
  canFind as canFindNow,
  initialState,
  reducer,
} from "./machine.js";
import { FilingForm } from "./components/FilingForm.js";
import { FilingViewer } from "./components/FilingViewer.js";
import { ResultTable } from "./components/ResultTable.js";

const WORKER_URL = new URL("./worker.js", import.meta.url);

const INITIAL: FormValues = {
  email: "",
  ticker: "",
  year: "",
  form: "DEF 14A",
  table: "summary_compensation",
};

export function App() {
  const [state, dispatch] = React.useReducer(reducer, initialState(INITIAL));
  // One bridge for the page. It survives cancellation by rebuilding its worker
  // on the next request, so it does not need to be recreated here.
  const bridgeRef = React.useRef<SecTablesBridge | null>(null);
  const abortRef = React.useRef<AbortController | null>(null);

  const bridge = () => {
    bridgeRef.current ??= new SecTablesBridge({ workerUrl: WORKER_URL });
    return bridgeRef.current;
  };

  React.useEffect(() => () => bridgeRef.current?.dispose(), []);

  const busy = BUSY.has(state.status);

  const fail = (error: unknown) => {
    if (error instanceof ExtractionTimeout) {
      dispatch({ type: "timed_out", message: error.message });
      return;
    }
    if (error instanceof CancelledError || (error as Error)?.name === "AbortError") {
      dispatch({ type: "cancelled" });
      return;
    }
    if (error instanceof ProxyError) {
      dispatch({
        type: "failed",
        status: error.kind === "throttled" ? "throttled" : "failed",
        message: describeProxyError(error),
      });
      return;
    }
    dispatch({ type: "failed", status: "failed", message: (error as Error)?.message ?? String(error) });
  };

  /** Step 1: which filings exist. Never collapses several to one silently. */
  const onFind = async () => {
    const errors = validateForm(state.values);
    dispatch({ type: "validated", errors });
    if (!isValid(errors)) return;

    abortRef.current = new AbortController();
    dispatch({ type: "finding" });
    const query = {
      email: state.values.email.trim(),
      ticker: state.values.ticker.trim().toUpperCase(),
      year: Number(state.values.year),
      form: state.values.form,
    };
    try {
      const listed = await listFilings(query, abortRef.current.signal);
      if (!listed.filings.length) {
        dispatch({
          type: "failed",
          status: "failed",
          message: `SEC lists no ${query.form} for ${query.ticker} in ${query.year}.`,
        });
        return;
      }
      dispatch({ type: "found", filings: listed.filings, defaultId: listed.defaultId });

      // Fetching follows immediately: a listing on its own is not something a
      // person asked for, and the document is what makes Extract possible.
      dispatch({ type: "fetching" });
      const chosen = state.selectedFilingId ?? listed.defaultId;
      const filing = await fetchFiling({ ...query, filingId: chosen }, abortRef.current.signal);
      dispatch({
        type: "fetched",
        meta: filing.meta,
        bytes: new Uint8Array(filing.bytes),
        cached: filing.cached,
      });
    } catch (error) {
      fail(error);
    }
  };

  /** Re-fetch when the user picks a different filing from the list. */
  const onSelectFiling = async (id: string) => {
    dispatch({ type: "select_filing", id });
    const errors = validateForm(state.values);
    if (!isValid(errors)) return;
    abortRef.current = new AbortController();
    dispatch({ type: "fetching" });
    try {
      const filing = await fetchFiling(
        {
          email: state.values.email.trim(),
          ticker: state.values.ticker.trim().toUpperCase(),
          year: Number(state.values.year),
          form: state.values.form,
          filingId: id,
        },
        abortRef.current.signal,
      );
      dispatch({
        type: "fetched",
        meta: filing.meta,
        bytes: new Uint8Array(filing.bytes),
        cached: filing.cached,
      });
    } catch (error) {
      fail(error);
    }
  };

  /** Step 2: the same bytes, extracted in this tab. */
  const onExtract = async () => {
    if (!state.filingBytes || !state.filingMeta) return;
    try {
      // A copy, because the buffer is transferred to the worker and detached —
      // and the viewer above is still showing this document.
      const copy = state.filingBytes.slice().buffer;
      const result = await runExtraction(
        bridge(),
        copy,
        state.filingMeta.filingDate,
        state.values.table as Profile,
        {
          // A test seam, and the only one in the app. The suite has to prove
          // that a document which hangs extraction is actually stopped, and
          // waiting the real 30 seconds to watch it happen would make the
          // browser run four times longer for no extra evidence.
          timeoutMs: (window as unknown as { __secTablesTimeoutMs?: number }).__secTablesTimeoutMs,
          onPreparing: () => dispatch({ type: "preparing" }),
          onExtracting: () => dispatch({ type: "extracting" }),
        },
      );
      dispatch({ type: "extracted", result });
    } catch (error) {
      fail(error);
    }
  };

  const onCancel = () => {
    abortRef.current?.abort();
    // Terminates the worker if one is running. The bridge builds a new one on
    // the next request; there is no way to interrupt Python otherwise.
    void bridgeRef.current?.cancel();
    dispatch({ type: "cancelled" });
  };

  const statusClass =
    state.status === "needs_review"
      ? "review"
      : state.status === "successful"
        ? "ok"
        : ["failed", "throttled", "no_table"].includes(state.status)
          ? "bad"
          : "";

  return (
    <div className="app">
      <header>
        <h1>sec-tables</h1>
        <p className="muted">
          Fetch an SEC filing and read one disclosure table out of it. The filing is
          downloaded by this site's server; the extraction runs in your browser, in Python,
          via WebAssembly.
        </p>
      </header>

      <div className="columns">
        <div className="left">
          <FilingForm
            values={state.values}
            errors={state.errors}
            filings={state.filings}
            selectedFilingId={state.selectedFilingId}
            busy={busy}
            canExtract={canExtractNow(state)}
            onChange={(name, value) => dispatch({ type: "field", name, value })}
            onSelectFiling={onSelectFiling}
            onFind={onFind}
            onExtract={onExtract}
            onCancel={onCancel}
          />

          <p
            className={`status ${statusClass}`}
            data-status={state.status}
            data-testid="status"
            role="status"
            aria-live="polite"
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            {state.message ?? STATUS_TEXT[state.status]}
          </p>

          {state.recoveredFromTimeout ? (
            <p className="callout bad" role="alert">
              The Python runtime was terminated and rebuilt. The next extraction starts from a
              cold runtime.
            </p>
          ) : null}
        </div>

        <div className="right">
          <FilingViewer bytes={state.filingBytes} meta={state.filingMeta} cached={state.filingCached} />
          <ResultTable result={state.result} meta={state.filingMeta} />
        </div>
      </div>

      <footer className="muted">
        Not affiliated with, endorsed by, or connected to the U.S. Securities and Exchange
        Commission. Extraction is heuristic — verify anything you rely on against the filing.
      </footer>
    </div>
  );
}
