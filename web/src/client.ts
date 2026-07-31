/**
 * Main-thread client for the sec-tables worker.
 *
 * Owns the worker's lifecycle and the request/response correlation, so callers
 * see promises instead of messages. It is not a UI, and it makes no decisions
 * about where documents come from.
 *
 * The one thing worth reading closely is `cancel()`. See its comment.
 */
import { DEFAULT_LOCATION, type RuntimeLocation } from "./pin.js";
import type {
  Diagnostics,
  ExtractionResult,
  FilingDate,
  Profile,
  RuntimeInfo,
  SelftestFault,
  WorkerResponse,
} from "./protocol.js";

export type { Diagnostics, ExtractionResult, Profile, RuntimeInfo };

export class CancelledError extends Error {
  constructor(requestId: string) {
    super(`request ${requestId} was cancelled`);
    this.name = "CancelledError";
  }
}

export class WorkerFailure extends Error {
  readonly kind: string;
  constructor(kind: string, message: string) {
    super(message);
    this.name = "WorkerFailure";
    this.kind = kind;
  }
}

export interface BridgeOptions {
  /** URL of the built worker bundle. */
  workerUrl: string | URL;
  location?: Partial<RuntimeLocation>;
}

interface Waiter {
  resolve: (value: any) => void;
  reject: (reason: unknown) => void;
}

export class SecTablesBridge {
  private worker: Worker | null = null;
  private readonly waiters = new Map<string, Waiter>();
  private nextId = 0;
  private readonly options: BridgeOptions;
  private readonly location: RuntimeLocation;
  /** Set when a rebuild is in progress, so `terminate` is idempotent. */
  private generation = 0;

  constructor(options: BridgeOptions) {
    this.options = options;
    this.location = { ...DEFAULT_LOCATION, ...(options.location ?? {}) };
  }

  /** The current worker, started on demand. */
  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    const worker = new Worker(this.options.workerUrl, { type: "module" });
    worker.onmessage = (event: MessageEvent<WorkerResponse>) => this.receive(event.data);
    worker.onerror = (event: ErrorEvent) => {
      // A worker-level error (a bad import, an out-of-memory abort) never
      // answers the outstanding requests, so fail them explicitly rather than
      // leaving callers hanging on a promise that can no longer settle.
      this.failAll(new WorkerFailure("WorkerError", event.message || "worker error"));
    };
    worker.postMessage({
      type: "configure",
      pyodideBaseUrl: this.location.pyodideBaseUrl,
      wheelBaseUrl: this.location.wheelBaseUrl,
    });
    this.worker = worker;
    return worker;
  }

  private receive(response: WorkerResponse): void {
    const waiter = this.waiters.get(response.requestId);
    if (!waiter) return; // Answer to a request whose worker was already torn down.
    this.waiters.delete(response.requestId);
    switch (response.type) {
      case "ready":
        waiter.resolve(response.runtime);
        return;
      case "result":
        waiter.resolve(response.result);
        return;
      case "diagnostics":
        waiter.resolve(response.diagnostics);
        return;
      case "cancelled":
        waiter.reject(new CancelledError(response.requestId));
        return;
      case "error":
        waiter.reject(new WorkerFailure(response.error.kind, response.error.message));
        return;
    }
  }

  private failAll(reason: unknown): void {
    for (const [, waiter] of this.waiters) waiter.reject(reason);
    this.waiters.clear();
  }

  private send<T>(message: Record<string, unknown>, transfer: Transferable[] = []): Promise<T> {
    const requestId = `r${++this.nextId}`;
    const worker = this.ensureWorker();
    return new Promise<T>((resolve, reject) => {
      this.waiters.set(requestId, { resolve, reject });
      worker.postMessage({ ...message, requestId }, transfer);
    });
  }

  /**
   * Bring the runtime up. Idempotent and safe to call concurrently: the worker
   * itself memoises preparation, so N simultaneous calls initialise one
   * interpreter and N-1 of them report `initializedRuntime: false`.
   */
  prepare(): Promise<RuntimeInfo> {
    return this.send<RuntimeInfo>({ type: "prepare" });
  }

  /**
   * Extract one table from one filing.
   *
   * `document` is transferred, not copied — after this call the caller's
   * ArrayBuffer is detached. That is the point: a multi-megabyte filing should
   * cross the thread boundary once, with no base64 and no second copy on the
   * main thread.
   */
  extract(
    document: ArrayBuffer,
    filingDate: FilingDate,
    profile: Profile,
  ): Promise<ExtractionResult> {
    return this.send<ExtractionResult>(
      { type: "extract", document, filingDate, profile },
      [document],
    );
  }

  diagnostics(): Promise<Diagnostics> {
    return this.send<Diagnostics>({ type: "diagnostics" });
  }

  /** Fault injection for the test suite; exercises the shipped error path. */
  selftest(fault: SelftestFault): Promise<ExtractionResult> {
    return this.send<ExtractionResult>({ type: "selftest", fault });
  }

  /**
   * Stop everything in flight and come back with a usable runtime.
   *
   * This terminates the worker rather than asking it to stop, because there is
   * no other option: Pyodide holds the worker's only thread for the duration of
   * an extraction, so a `cancel` message is not read until the work it would
   * cancel has already finished. Termination is also the only defence against
   * the input that does not finish at all — a `colspan` of two billion expands
   * the grid until the thread stops responding.
   *
   * The cost is the whole runtime: the next request pays a cold start again.
   * That is why this is `cancel()`, called deliberately, and not something the
   * client does on its own.
   */
  async cancel(): Promise<void> {
    const worker = this.worker;
    this.generation += 1;
    this.worker = null;
    this.failAll(new CancelledError("all"));
    if (worker) {
      worker.onmessage = null;
      worker.onerror = null;
      worker.terminate();
    }
  }

  /** Terminate without rebuilding. The bridge is reusable afterwards. */
  dispose(): void {
    void this.cancel();
  }

  get started(): boolean {
    return this.worker !== null;
  }
}
