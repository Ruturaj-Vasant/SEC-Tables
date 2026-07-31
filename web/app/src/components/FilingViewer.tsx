/**
 * The original filing, shown as SEC filed it — inside a sandbox.
 *
 * A proxy statement is a document from a third party, and it goes into an
 * `<iframe sandbox>` with no `allow-scripts` and no `allow-same-origin`. That
 * combination gives it an opaque origin: its markup cannot run script, cannot
 * read this page, cannot reach the API this page talks to, and cannot navigate
 * the top window. `dangerouslySetInnerHTML` would give it all four.
 *
 * Plain-text filings are wrapped in a `<pre>` instead of being handed over as
 * markup. Before ~2001 the table *is* the whitespace — columns are aligned with
 * runs of spaces — so collapsing it would destroy exactly the documents this
 * library exists to read.
 */
import * as React from "react";
import { looksLikeHtml, type FilingMeta } from "../domain.js";

interface Props {
  bytes: Uint8Array | null;
  meta: FilingMeta | null;
  cached: boolean;
}

const escapeHtml = (s: string): string =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/**
 * Decode the way the library does: UTF-8, then latin-1, never with `errors=ignore`.
 *
 * Dropping undecodable bytes silently shortens lines, and in a space-aligned
 * table every dropped character moves a column boundary.
 */
function decode(bytes: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return new TextDecoder("windows-1252").decode(bytes);
  }
}

function documentFor(bytes: Uint8Array): Blob {
  if (looksLikeHtml(bytes)) {
    return new Blob([bytes as BlobPart], { type: "text/html" });
  }
  const text = decode(bytes);
  const page =
    `<!doctype html><meta charset="utf-8">` +
    `<style>` +
    `html{background:#fff;color:#111}` +
    `pre{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;` +
    `white-space:pre;tab-size:8;margin:1rem}` +
    `@media (prefers-color-scheme:dark){html{background:#14161a;color:#e6e6e6}}` +
    `</style><pre>${escapeHtml(text)}</pre>`;
  return new Blob([page], { type: "text/html" });
}

export function FilingViewer({ bytes, meta, cached }: Props) {
  const [url, setUrl] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!bytes) {
      setUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(documentFor(bytes));
    setUrl(objectUrl);
    // Revoked whenever the filing changes and on unmount. A Blob URL pins its
    // blob in memory until revoked, so a few multi-megabyte filings viewed in
    // one session would otherwise never be released.
    return () => URL.revokeObjectURL(objectUrl);
  }, [bytes]);

  if (!meta || !url) {
    return (
      <section className="panel viewer empty" aria-labelledby="viewer-heading">
        <h2 id="viewer-heading">Filing</h2>
        <p className="muted">The filing appears here once it has been fetched from SEC.</p>
      </section>
    );
  }

  const kind = looksLikeHtml(bytes!) ? "HTML" : "plain text";
  return (
    <section className="panel viewer" aria-labelledby="viewer-heading">
      <div className="panel-head">
        <h2 id="viewer-heading">
          {meta.ticker} {meta.form} · {meta.filingDate}
        </h2>
        <p className="muted">
          {kind} · {(bytes!.byteLength / 1024).toFixed(0)} KB ·{" "}
          {meta.route === "complete_submission" ? "complete submission" : "primary document"} ·{" "}
          {cached ? "from this server's cache" : "fetched from SEC"} ·{" "}
          <a href={meta.sourceUrl} target="_blank" rel="noreferrer noopener">
            source on sec.gov
          </a>
        </p>
      </div>
      <iframe
        className="filing-frame"
        title={`${meta.ticker} ${meta.form} filed ${meta.filingDate}`}
        src={url}
        // No allow-scripts and no allow-same-origin: the filing gets an opaque
        // origin and stays inert. allow-popups is omitted too — a filing has no
        // reason to open a window.
        sandbox=""
        referrerPolicy="no-referrer"
      />
    </section>
  );
}
