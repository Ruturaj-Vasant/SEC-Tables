/**
 * The only way this page talks to SEC: through its own server.
 *
 * There is no direct-to-SEC path here and there cannot be one — `/Archives`
 * sends no permissive `Access-Control-Allow-Origin`, so the request would be
 * refused before it left the browser, and a page cannot set a User-Agent to
 * declare who is asking in the first place.
 */
import {
  parseFilingList,
  parseFilingMeta,
  type FilingListResponse,
  type FilingMeta,
  type ProxyErrorKind,
  type ProxyErrorShape,
} from "./domain.js";

export class ProxyError extends Error implements ProxyErrorShape {
  readonly kind: ProxyErrorKind;
  constructor(kind: ProxyErrorKind, message: string) {
    super(message);
    this.name = "ProxyError";
    this.kind = kind;
  }
}

export interface FilingQuery {
  email: string;
  ticker: string;
  year: number;
  form: string;
  filingId?: string | null;
}

export interface FetchedFiling {
  bytes: ArrayBuffer;
  meta: FilingMeta;
  cached: boolean;
}

async function readError(response: Response): Promise<ProxyError> {
  let kind: ProxyErrorKind = "upstream_failure";
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    if (body?.error?.kind) {
      kind = body.error.kind;
      message = body.error.message ?? message;
    }
  } catch {
    /* a non-JSON error body is itself the message */
  }
  return new ProxyError(kind, message);
}

/**
 * POST, for a read.
 *
 * The contact email is in the body deliberately: a query string lands in access
 * logs, `Referer` headers, browser history and intermediary caches, and the one
 * promise this app makes about that address is that it is not kept anywhere.
 */
async function post(path: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ProxyError("offline", (err as Error).message);
  }
  if (!response.ok) throw await readError(response);
  return response;
}

export async function listFilings(
  query: FilingQuery,
  signal?: AbortSignal,
): Promise<FilingListResponse> {
  const response = await post(
    "/api/filings",
    { email: query.email, ticker: query.ticker, year: query.year, form: query.form },
    signal,
  );
  return parseFilingList(await response.json());
}

export async function fetchFiling(
  query: FilingQuery,
  signal?: AbortSignal,
): Promise<FetchedFiling> {
  const response = await post(
    "/api/filing",
    {
      email: query.email,
      ticker: query.ticker,
      year: query.year,
      form: query.form,
      filingId: query.filingId ?? null,
    },
    signal,
  );
  // `arrayBuffer()`, not `text()`: the bytes go straight to the Worker, and a
  // pre-2001 filing is latin-1 that a text decode would quietly mangle.
  const bytes = await response.arrayBuffer();
  return {
    bytes,
    meta: parseFilingMeta(response.headers.get("X-Filing-Meta")),
    cached: response.headers.get("X-Filing-Cache") === "hit",
  };
}
