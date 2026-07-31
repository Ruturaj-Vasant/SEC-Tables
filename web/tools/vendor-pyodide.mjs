/**
 * Assemble a self-contained, pinned Pyodide distribution in `vendor/pyodide/`.
 *
 * The runtime files come from the locally installed `pyodide` package (pinned
 * in package.json). The two wheels this bridge needs — lxml and micropip — are
 * not shipped inside that package, so they are downloaded once from the
 * official CDN for the *same* version and verified against the sha256 already
 * recorded in pyodide-lock.json.
 *
 * The result is a directory that serves the whole runtime from one origin, so
 * a test run does not depend on a CDN being reachable and a deployment does not
 * depend on a third party's cache behaviour.
 */
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile, copyFile, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = join(HERE, "..");
const SOURCE = join(WEB, "node_modules", "pyodide");
const OUT = join(WEB, "vendor", "pyodide");

const RUNTIME_FILES = [
  "pyodide.mjs",
  "pyodide.asm.mjs",
  "pyodide.asm.wasm",
  "python_stdlib.zip",
  "pyodide-lock.json",
];

/** Only what the bridge actually loads. Nothing else is vendored. */
const PACKAGES = ["lxml", "micropip"];

const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

async function main() {
  const lock = JSON.parse(await readFile(join(SOURCE, "pyodide-lock.json"), "utf8"));
  const version = JSON.parse(await readFile(join(SOURCE, "package.json"), "utf8")).version;
  await mkdir(OUT, { recursive: true });

  const report = [];
  for (const name of RUNTIME_FILES) {
    const to = join(OUT, name);
    await copyFile(join(SOURCE, name), to);
    report.push([name, (await stat(to)).size, "package"]);
  }

  for (const name of PACKAGES) {
    const entry = lock.packages[name];
    if (!entry) throw new Error(`${name} is not in pyodide-lock.json`);
    const to = join(OUT, entry.file_name);
    if (!existsSync(to)) {
      const url = `https://cdn.jsdelivr.net/pyodide/v${version}/full/${entry.file_name}`;
      const response = await fetch(url);
      if (!response.ok) throw new Error(`${url} -> ${response.status}`);
      const bytes = Buffer.from(await response.arrayBuffer());
      // The lock file is the authority on what this wheel must be. A download
      // that does not match it is not installed, it is an error.
      const digest = sha256(bytes);
      if (digest !== entry.sha256) {
        throw new Error(
          `${entry.file_name} checksum mismatch\n  lock: ${entry.sha256}\n  got:  ${digest}`,
        );
      }
      await writeFile(to, bytes);
    }
    report.push([entry.file_name, (await stat(to)).size, "cdn+verified"]);
  }

  const total = report.reduce((n, [, size]) => n + size, 0);
  console.log(`pyodide ${version} (python ${lock.info.python}) -> vendor/pyodide/`);
  for (const [name, size, origin] of report) {
    console.log(`  ${name.padEnd(52)} ${(size / 1048576).toFixed(2).padStart(7)} MB  ${origin}`);
  }
  console.log(`  ${"TOTAL".padEnd(52)} ${(total / 1048576).toFixed(2).padStart(7)} MB`);
}

await main();
