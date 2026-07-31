/**
 * Test harness page entry.
 *
 * Exposes the real client on `window` so Playwright can drive it from outside
 * the browser. It adds no behaviour: every call below is a thin pass-through to
 * `SecTablesBridge`, because a harness that reimplements the thing it tests
 * proves nothing.
 *
 * Filings are fetched *inside* the page — an ArrayBuffer cannot be handed
 * across the Playwright boundary, and fetching in-page is also what a real
 * caller does with a proxy response or a File.
 */
import {
  SecTablesBridge,
  CancelledError,
  type Diagnostics,
  type ExtractionResult,
  type Profile,
  type RuntimeInfo,
} from "../src/client.js";
import type { SelftestFault } from "../src/protocol.js";

const WORKER_URL = new URL("./worker.js", import.meta.url);

let bridge = new SecTablesBridge({ workerUrl: WORKER_URL });

/** Errors the page saw, so a test can assert the console stayed clean. */
const consoleErrors: string[] = [];
const originalError = console.error;
console.error = (...args: unknown[]) => {
  consoleErrors.push(args.map(String).join(" "));
  originalError.apply(console, args as never);
};
window.addEventListener("error", (e) => consoleErrors.push(`window.error: ${e.message}`));
window.addEventListener("unhandledrejection", (e) =>
  consoleErrors.push(`unhandledrejection: ${String((e as PromiseRejectionEvent).reason)}`),
);

async function assetBytes(url: string): Promise<ArrayBuffer> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.arrayBuffer();
}

export interface ExtractCall {
  url: string;
  filingDate: string;
  profile: Profile;
}

const api = {
  /** Discard the current worker and start over, as a page would on cancel. */
  async reset(): Promise<void> {
    await bridge.cancel();
    bridge = new SecTablesBridge({ workerUrl: WORKER_URL });
    consoleErrors.length = 0;
  },

  prepare(): Promise<RuntimeInfo> {
    return bridge.prepare();
  },

  /** Prepare a throwaway bridge pointed at a different wheel directory. */
  async prepareFrom(wheelBaseUrl: string): Promise<{ kind: string; message: string }> {
    const isolated = new SecTablesBridge({
      workerUrl: WORKER_URL,
      location: { wheelBaseUrl },
    });
    try {
      await isolated.prepare();
      return { kind: "prepared", message: "" };
    } catch (err) {
      return { kind: (err as any).kind ?? (err as Error).name, message: (err as Error).message };
    } finally {
      isolated.dispose();
    }
  },

  /** Two `prepare()` calls issued in the same tick, deliberately not awaited apart. */
  prepareTwice(): Promise<RuntimeInfo[]> {
    return Promise.all([bridge.prepare(), bridge.prepare()]);
  },

  async extract(call: ExtractCall): Promise<ExtractionResult> {
    const bytes = await assetBytes(call.url);
    return bridge.extract(bytes, call.filingDate, call.profile);
  },

  /** Extract twice from the same asset; the second is the warm path. */
  async extractTwice(call: ExtractCall): Promise<ExtractionResult[]> {
    const first = await api.extract(call);
    const second = await api.extract(call);
    return [first, second];
  },

  /** Extract from bytes the page holds already, with no fetch in between. */
  async extractRawBytes(
    byteValues: number[],
    filingDate: string,
    profile: Profile,
  ): Promise<ExtractionResult & { detached: boolean }> {
    const buffer = new Uint8Array(byteValues).buffer;
    const result = await bridge.extract(buffer, filingDate, profile);
    // A transferred ArrayBuffer is detached on this side. Proving it confirms
    // the filing crossed the boundary once rather than being copied.
    return { ...result, detached: buffer.byteLength === 0 };
  },

  /** Synthesize a filing of a given size to find the practical ceiling. */
  async extractSynthetic(
    padBytes: number,
    seedUrl: string,
    filingDate: string,
    profile: Profile,
  ): Promise<ExtractionResult & { documentBytes: number }> {
    const seed = new Uint8Array(await assetBytes(seedUrl));
    const filler = new TextEncoder().encode(
      "\n<p>Ordinary proxy prose that contains no table at all.</p>",
    );
    const total = seed.byteLength + padBytes;
    const buffer = new Uint8Array(total);
    buffer.set(seed, 0);
    for (let i = seed.byteLength; i < total; i += filler.length) {
      buffer.set(filler.subarray(0, Math.min(filler.length, total - i)), i);
    }
    const documentBytes = buffer.byteLength;
    const result = await bridge.extract(buffer.buffer, filingDate, profile);
    return { ...result, documentBytes };
  },

  selftest(fault: SelftestFault): Promise<ExtractionResult> {
    return bridge.selftest(fault);
  },

  diagnostics(): Promise<Diagnostics> {
    return bridge.diagnostics();
  },

  /**
   * Start an extraction, cancel it mid-flight by terminating the worker, then
   * extract again on the rebuilt runtime.
   */
  async cancelMidFlightThenExtract(call: ExtractCall): Promise<{
    cancelKind: string;
    afterRebuild: ExtractionResult;
    reinitialized: boolean;
  }> {
    const bytes = await assetBytes(call.url);
    const inFlight = bridge.extract(bytes, call.filingDate, call.profile);
    const settled = inFlight.then(
      () => ({ kind: "completed" }),
      (err: unknown) => ({ kind: (err as Error).name }),
    );
    await bridge.cancel();
    const outcome = await settled;
    bridge = new SecTablesBridge({ workerUrl: WORKER_URL });
    const info = await bridge.prepare();
    const afterRebuild = await api.extract(call);
    return {
      cancelKind: outcome.kind,
      afterRebuild,
      reinitialized: info.initializedRuntime,
    };
  },

  /** Cancel a request queued behind a running one; the worker answers it. */
  async cancelQueued(call: ExtractCall): Promise<string> {
    const bytes = await assetBytes(call.url);
    const first = bridge.extract(bytes.slice(0), call.filingDate, call.profile);
    const second = bridge.extract(bytes, call.filingDate, call.profile);
    const secondOutcome = second.then(
      () => "completed",
      (err: unknown) => (err as Error).name,
    );
    await bridge.cancel();
    await first.catch(() => undefined);
    const kind = await secondOutcome;
    bridge = new SecTablesBridge({ workerUrl: WORKER_URL });
    return kind;
  },

  /** Send a message the protocol does not accept, straight at the worker. */
  async sendMalformed(payload: unknown): Promise<{ kind: string; message: string }> {
    const worker = new Worker(WORKER_URL, { type: "module" });
    try {
      return await new Promise((resolve, reject) => {
        worker.onmessage = (event: MessageEvent) => {
          const data = event.data;
          if (data.type === "error") resolve(data.error);
          else reject(new Error(`unexpected response: ${JSON.stringify(data)}`));
        };
        worker.postMessage(payload);
      });
    } finally {
      worker.terminate();
    }
  },

  consoleErrors(): string[] {
    return [...consoleErrors];
  },

  isCancelled(name: string): boolean {
    return name === CancelledError.name;
  },
};

declare global {
  interface Window {
    secBridge: typeof api;
  }
}

window.secBridge = api;
document.body.dataset.ready = "true";
