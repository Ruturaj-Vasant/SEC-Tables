/**
 * The normalized table, and everything needed to distrust it properly.
 *
 * Two rules shape this component:
 *
 * 1. **Review warnings and provenance are never mixed.** `ascii_source` on a
 *    1997 filing means the library read a plain-text table correctly;
 *    `ambiguous_selection` means two tables tied and the choice was a coin
 *    flip. Under one heading a reader learns to ignore both.
 * 2. **Nothing here says "verified".** Extraction succeeding means a table was
 *    found and parsed. It does not mean it is the right table, and nothing in
 *    the pipeline checks the values against the filing.
 */
import * as React from "react";
import type { ExtractionResult } from "../../../src/protocol.js";
import {
  FLAG_TEXT,
  csvFilename,
  describeRoute,
  partitionFlags,
  toCsv,
  type FilingMeta,
} from "../domain.js";

interface Props {
  result: ExtractionResult | null;
  meta: FilingMeta | null;
}

const ERA_TEXT: Record<string, string> = {
  pre2006: "pre-2006 (Item 402(b))",
  post2006: "post-2006 (Item 402(c))",
  transition: "2006 transition",
  single: "single schema version",
};

const BACKEND_TEXT: Record<string, string> = {
  ascii: "ASCII — space-aligned plain text, no markup at all",
  sgml: "SGML — an old EDGAR <TABLE> block with no row or cell tags",
  dom: "DOM — an HTML tree, parsed with lxml",
  narrative: "narrative prose",
};

function FlagList({ title, kind, flags }: { title: string; kind: string; flags: string[] }) {
  if (!flags.length) return null;
  return (
    <div className={`flags ${kind}`}>
      <h4>{title}</h4>
      <ul>
        {flags.map((flag) => (
          <li key={flag}>
            <code>{flag}</code>
            <span>{FLAG_TEXT[flag] ?? ""}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ResultTable({ result, meta }: Props) {
  const [csvUrl, setCsvUrl] = React.useState<string | null>(null);

  const csv = React.useMemo(
    () => (result?.ok ? toCsv(result.columns, result.rows) : null),
    [result],
  );

  React.useEffect(() => {
    if (!csv) {
      setCsvUrl(null);
      return;
    }
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    setCsvUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [csv]);

  if (!result) {
    return (
      <section className="panel result empty" aria-labelledby="result-heading">
        <h2 id="result-heading">Extracted table</h2>
        <p className="muted">The normalized table appears here after extraction.</p>
      </section>
    );
  }

  const { review, provenance, other } = partitionFlags(result);

  return (
    <section className="panel result" aria-labelledby="result-heading">
      <div className="panel-head">
        <h2 id="result-heading">Extracted table</h2>
        {result.ok && csvUrl ? (
          <a
            className="download"
            href={csvUrl}
            download={csvFilename(meta, result.profile)}
            data-testid="download-csv"
          >
            Download CSV
          </a>
        ) : null}
      </div>

      <dl className="facts">
        <div>
          <dt>Ticker</dt>
          <dd>
            {meta?.ticker ?? "—"}
            {meta?.cik ? <span className="muted"> · CIK {meta.cik}</span> : null}
          </dd>
        </div>
        <div>
          <dt>Form</dt>
          <dd>{meta?.form ?? "—"}</dd>
        </div>
        <div>
          <dt>Filing date</dt>
          <dd>{meta?.filingDate ?? "—"}</dd>
        </div>
        <div>
          <dt>Table</dt>
          <dd>{result.profile}</dd>
        </div>
        <div>
          <dt>Schema era</dt>
          <dd>{result.era ? (ERA_TEXT[result.era] ?? result.era) : "—"}</dd>
        </div>
        <div>
          <dt>Backend</dt>
          <dd title={result.backend ? BACKEND_TEXT[result.backend] : undefined}>
            {result.backend ?? "—"}
          </dd>
        </div>
        <div>
          <dt>Rows</dt>
          <dd>
            {result.rows.length} × {result.columns.length}
          </dd>
        </div>
        <div>
          <dt>Extraction time</dt>
          <dd>
            {result.executionMs.toFixed(0)} ms
            {result.preparationMs > 0 ? (
              <span className="muted"> · {(result.preparationMs / 1000).toFixed(1)}s startup</span>
            ) : null}
          </dd>
        </div>
        {typeof result.metadata.candidates === "number" ? (
          <div>
            <dt>Candidates</dt>
            <dd>
              {String(result.metadata.candidates)} scored
              {result.metadata.top_score !== undefined ? (
                <span className="muted"> · best {String(result.metadata.top_score)}</span>
              ) : null}
              {result.metadata.margin !== undefined && result.metadata.margin !== null ? (
                <span className="muted"> · margin {String(result.metadata.margin)}</span>
              ) : null}
            </dd>
          </div>
        ) : null}
        {meta ? (
          <div>
            <dt>Route</dt>
            <dd>{describeRoute(meta.route)}</dd>
          </div>
        ) : null}
      </dl>

      {review.length ? (
        <div className="callout review" role="status">
          <strong>This result asks for review.</strong> Extraction succeeded, which means a
          table was found and parsed — not that it is the right table or that the values are
          right. Check it against the filing above.
        </div>
      ) : null}

      <FlagList title="Review warnings" kind="review" flags={review} />
      <FlagList title="Provenance — how this was obtained, not a defect" kind="provenance" flags={provenance} />
      <FlagList title="Other" kind="other" flags={other} />

      {result.ok ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {result.columns.map((c, i) => (
                  <th key={`${c}-${i}`} scope="col">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, r) => (
                <tr key={r}>
                  {result.columns.map((_, c) => (
                    <td key={c}>{row[c] ?? ""}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">
          No table of this type cleared the score threshold in this filing. Many plain-text
          renditions mention a table in prose and then jump to its footnotes — the table is
          genuinely not in the document.
        </p>
      )}
    </section>
  );
}
