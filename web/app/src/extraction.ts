/**
 * Running an extraction that might never finish.
 *
 * The bridge already established that this is a real case rather than a
 * defensive one: a filing containing `colspan="2000000000"` makes the library
 * expand the grid until the thread stops responding. It does not raise, so
 * there is nothing to catch, and it does not return, so there is nothing to
 * await. A size limit does not help either — that document is a few hundred
 * bytes.
 *
 * So the only thing that works is a wall clock plus termination, which is
 * exactly the cancellation the bridge already implements. Nothing here claims
 * cooperative cancellation, because Python cannot honour one: Pyodide owns the
 * worker's single thread for the duration of the call.
 */
import type { SecTablesBridge } from "../../src/client.js";
import type { ExtractionResult, Profile } from "../../src/protocol.js";

/** Generous next to a 33 ms proxy statement; short next to a hang. */
export const DEFAULT_EXTRACTION_TIMEOUT_MS = 30_000;

export class ExtractionTimeout extends Error {
  readonly timeoutMs: number;
  constructor(timeoutMs: number) {
    super(
      `Extraction did not finish within ${Math.round(timeoutMs / 1000)}s and was stopped. ` +
        `The Python runtime was rebuilt, so you can try again.`,
    );
    this.name = "ExtractionTimeout";
    this.timeoutMs = timeoutMs;
  }
}

export interface RunOptions {
  timeoutMs?: number;
  /** Called when preparation starts, so the UI can distinguish it from extracting. */
  onPreparing?: () => void;
  onExtracting?: () => void;
}

/**
 * Prepare (if needed), extract, and guarantee a settled promise.
 *
 * Preparation is awaited separately from extraction so the interface can say
 * "starting Python" rather than showing one long unexplained wait — a cold
 * start is over a second, and an unexplained second looks like a bug.
 */
export async function runExtraction(
  bridge: SecTablesBridge,
  document: ArrayBuffer,
  filingDate: string,
  profile: Profile,
  options: RunOptions = {},
): Promise<ExtractionResult> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_EXTRACTION_TIMEOUT_MS;

  options.onPreparing?.();
  await bridge.prepare();

  options.onExtracting?.();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      // Reject BEFORE terminating, and the order is load-bearing. `cancel()`
      // synchronously rejects the in-flight extract with `CancelledError`, so
      // terminating first would let that reach the race ahead of this and the
      // caller would be told the user cancelled something the clock stopped.
      reject(new ExtractionTimeout(timeoutMs));
      // Then terminate: the worker is wedged and will never answer, and leaving
      // it alive would hold its wasm heap for the life of the page.
      void bridge.cancel();
    }, timeoutMs);
  });

  try {
    return await Promise.race([bridge.extract(document, filingDate, profile), expired]);
  } finally {
    clearTimeout(timer);
  }
}
