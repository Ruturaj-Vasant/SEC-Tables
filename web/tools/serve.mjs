/**
 * Static server for the browser suite.
 *
 * Deliberately not a bundler dev server: the point of these tests is to run the
 * real worker against real assets over real HTTP, with nothing rewriting the
 * module graph in between. It mounts four roots:
 *
 *   /              web/                     the harness page and dist/
 *   /pyodide/      web/vendor/pyodide/      pinned runtime + lxml wheel
 *   /py/           web/public/py/           the pinned sec-tables wheel
 *   /fixtures/     ../tests/fixtures/       the library's own hand-verified fixtures
 *
 * `/fixtures/` is mounted from the library rather than copied. A copy would
 * drift, and the whole value of those files is that they are the ones the
 * Python suite asserts against.
 */
import { createServer } from "node:http";
import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(HERE, "..");
const REPO = resolve(WEB, "..");

const MOUNTS = [
  ["/pyodide/", join(WEB, "vendor", "pyodide")],
  ["/py/", join(WEB, "public", "py")],
  ["/fixtures/", join(REPO, "tests", "fixtures")],
  ["/assets/", join(WEB, "test", "assets")],
  ["/", WEB],
];

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".wasm": "application/wasm",
  ".whl": "application/octet-stream",
  ".zip": "application/zip",
  ".txt": "text/plain; charset=utf-8",
  ".sha256": "text/plain; charset=utf-8",
  ".map": "application/json",
};

/** Only the pinned, content-addressed assets are cacheable. */
function cacheable(file) {
  return file.includes(join(WEB, "vendor", "pyodide")) || file.endsWith(".whl");
}

function localPath(urlPath) {
  const clean = normalize(decodeURIComponent(urlPath.split("?")[0]));
  if (clean.includes("..")) return null;
  for (const [prefix, root] of MOUNTS) {
    if (clean.startsWith(prefix)) {
      const rel = clean.slice(prefix.length);
      const full = join(root, rel);
      // Never serve outside the mount, whatever the path looked like.
      if (!full.startsWith(root)) return null;
      return full;
    }
  }
  return null;
}

const port = Number(process.env.PORT ?? 5199);

/**
 * A deliberately corrupted copy of the wheel, served under /py-tampered/.
 *
 * The checksum gate is the one part of preparation that is a security control
 * rather than a convenience, and a control that is never observed failing is
 * not known to work. One flipped byte is enough: SHA-256 has no near misses.
 */
async function tamperedWheel(name) {
  const bytes = await readFile(join(WEB, "public", "py", name));
  bytes[Math.floor(bytes.length / 2)] ^= 0xff;
  return bytes;
}

createServer(async (req, res) => {
  const path = (req.url ?? "/").split("?")[0];
  if (path.startsWith("/py-tampered/")) {
    try {
      const bytes = await tamperedWheel(path.slice("/py-tampered/".length));
      res.writeHead(200, {
        "content-type": "application/octet-stream",
        "content-length": bytes.length,
        "cache-control": "no-store",
      });
      res.end(bytes);
    } catch {
      res.writeHead(404).end("not found");
    }
    return;
  }

  let file = localPath(req.url ?? "/");
  if (!file) {
    res.writeHead(400).end("bad path");
    return;
  }
  try {
    let info = await stat(file);
    if (info.isDirectory()) {
      file = join(file, "index.html");
      info = await stat(file);
    }
    res.writeHead(200, {
      "content-type": TYPES[extname(file)] ?? "application/octet-stream",
      "content-length": info.size,
      // Cross-origin isolation is not required by this bridge (no SharedArrayBuffer,
      // no threads), but the headers are set so the suite runs under the same
      // policy a production deployment would most likely use.
      "cross-origin-opener-policy": "same-origin",
      "cross-origin-embedder-policy": "require-corp",
      "cross-origin-resource-policy": "same-origin",
      // The pinned runtime is immutable by construction — a version in the
      // path, a checksum on the wheel — so it is cacheable, and a returning
      // visitor should not re-download 13 MB of interpreter. Everything else
      // is code under active edit and must never be served stale.
      "cache-control": cacheable(file) ? "public, max-age=600" : "no-store",
    });
    createReadStream(file).pipe(res);
  } catch {
    res.writeHead(404).end(`not found: ${req.url}`);
  }
}).listen(port, () => console.log(`serving on http://127.0.0.1:${port}`));
