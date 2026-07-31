/**
 * Everything this bridge trusts, pinned in one place.
 *
 * A version range would let a browser cache decide which Python runs, and a
 * wheel fetched without a checksum is code from the network executing with the
 * page's privileges. Both are pinned to exact values and verified at load.
 */

/** Bump when the wire protocol or the result shape changes. */
export const PROTOCOL_VERSION = 1 as const;
export const RESULT_SCHEMA_VERSION = 1 as const;

/** Pyodide 314.0.3 ships CPython 3.14.0 and lxml 6.0.2 as a built package. */
export const PYODIDE_VERSION = "314.0.3";
// The lock file declares 3.14.0 (the ABI series); the built interpreter
// reports its own patch level, which is what this asserts against.
export const PYTHON_VERSION = "3.14.2";
export const LXML_VERSION = "6.0.2";

export const SEC_TABLES_VERSION = "0.3.0";
export const WHEEL_FILENAME = `sec_tables-${SEC_TABLES_VERSION}-py3-none-any.whl`;

/**
 * SHA-256 of the pinned wheel, verified with Web Crypto before it is handed to
 * the interpreter. Regenerate with `npm run wheel` (which rebuilds from the
 * repo and rewrites this constant's companion .sha256 file).
 */
export const WHEEL_SHA256 =
  "fbb686bbfa9288de8f8bd68c21e085e7b915435e3d814818182bb0b5371eac59";

/**
 * Where the worker looks for its assets. Both are directories, both are
 * same-origin by default: `pyodideBaseUrl` must contain pyodide.mjs, the wasm,
 * pyodide-lock.json and the package wheels; `wheelBaseUrl` must contain the
 * sec-tables wheel.
 *
 * Overridable per-deployment (a CDN, a subpath) but never per-request: the
 * worker must not be talked into loading an interpreter from an arbitrary URL.
 */
export interface RuntimeLocation {
  pyodideBaseUrl: string;
  wheelBaseUrl: string;
}

export const DEFAULT_LOCATION: RuntimeLocation = {
  pyodideBaseUrl: "/pyodide/",
  wheelBaseUrl: "/py/",
};

/** The three tables sec-tables 0.3.0 registers. Mirrors `available_tables()`. */
export const PROFILES = [
  "summary_compensation",
  "director_compensation",
  "beneficial_ownership",
] as const;
