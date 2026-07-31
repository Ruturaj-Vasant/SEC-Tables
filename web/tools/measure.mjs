/**
 * Measure the bridge on this machine, in a real browser.
 *
 * These are observations from one device, not performance guarantees: a cold
 * start is dominated by compiling ~9 MB of wasm, which varies with CPU, browser
 * build and whether the assets are cached, and the size ceiling below is where
 * *this* Chromium stopped, not a specification.
 *
 * Run: npm run measure   (writes measurements.json and prints a table)
 */
import { chromium } from "@playwright/test";
import { spawn, execSync } from "node:child_process";
import { gzipSync } from "node:zlib";
import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { cpus, totalmem, platform, release, arch } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = join(WEB, "..");
const PORT = Number(process.env.PORT ?? 5297);
const BASE = `http://127.0.0.1:${PORT}`;

const mib = (n) => Number((n / 1048576).toFixed(2));

// --- what a browser has to download -----------------------------------------

function assetSizes() {
  const entries = [];
  const add = (label, path, group) => {
    const bytes = readFileSync(path);
    entries.push({
      label,
      group,
      bytes: bytes.length,
      gzipBytes: gzipSync(bytes).length,
    });
  };
  const pyodide = join(WEB, "vendor", "pyodide");
  for (const name of ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]) {
    add(name, join(pyodide, name), "pyodide");
  }
  for (const name of readdirSync(pyodide).filter((f) => f.startsWith("lxml"))) {
    add(name, join(pyodide, name), "lxml");
  }
  for (const name of readdirSync(pyodide).filter((f) => f.startsWith("micropip"))) {
    add(name, join(pyodide, name), "micropip");
  }
  for (const name of readdirSync(join(WEB, "public", "py")).filter((f) => f.endsWith(".whl"))) {
    add(name, join(WEB, "public", "py", name), "sec-tables wheel");
  }
  add("worker.js", join(WEB, "dist", "worker.js"), "bridge");
  return entries;
}

// --- the run ----------------------------------------------------------------

const CASES = JSON.parse(readFileSync(join(WEB, "test", "expected", "cases.json"), "utf8"));

async function main() {
  const server = spawn("node", [join(WEB, "tools", "serve.mjs")], {
    env: { ...process.env, PORT: String(PORT) },
    stdio: "ignore",
  });
  await new Promise((r) => setTimeout(r, 700));

  const browser = await chromium.launch();
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  page.on("pageerror", (e) => consoleErrors.push(e.message));
  await page.goto(`${BASE}/index.html`);
  await page.waitForFunction(() => document.body.dataset.ready === "true");

  // Cold preparation, measured on a page that has loaded nothing yet.
  const coldStart = Date.now();
  const info = await page.evaluate(() => window.secBridge.prepare());
  const coldPreparationMs = Date.now() - coldStart;

  // Per-fixture: first (cold-runtime) extraction and a warm repeat.
  const perCase = [];
  for (const [id, c] of Object.entries(CASES)) {
    const [first, second] = await page.evaluate(
      (call) => window.secBridge.extractTwice(call),
      { url: c.url, filingDate: c.filingDate, profile: c.profile },
    );
    perCase.push({
      id,
      documentBytes: c.documentBytes,
      backend: first.backend,
      rows: first.rows.length,
      firstMs: Number(first.executionMs.toFixed(1)),
      warmMs: Number(second.executionMs.toFixed(1)),
    });
  }

  // A second, entirely fresh page: cold preparation with the assets already in
  // the HTTP cache, which is what a returning visitor actually pays.
  const page2 = await browser.newPage();
  await page2.goto(`${BASE}/index.html`);
  await page2.waitForFunction(() => document.body.dataset.ready === "true");
  const warmCacheStart = Date.now();
  await page2.evaluate(() => window.secBridge.prepare());
  const cachedPreparationMs = Date.now() - warmCacheStart;
  await page2.close();

  // --- how large a filing this browser will actually take -------------------
  //
  // A real proxy statement is 0.1-5 MB; the sweep runs past that on purpose, to
  // find where the wasm heap or the copy into it gives out rather than to claim
  // a supported size.
  const ceiling = [];
  for (const targetMiB of [1, 4, 8, 16, 32, 64, 128, 256, 512]) {
    const pad = targetMiB * 1048576;
    const started = Date.now();
    try {
      const result = await page.evaluate(
        ({ pad }) =>
          window.secBridge.extractSynthetic(
            pad,
            "/fixtures/cmp_2024_director_comp.html",
            "2024-01-29",
            "director_compensation",
          ),
        { pad },
      );
      ceiling.push({
        targetMiB,
        documentBytes: result.documentBytes,
        documentMiB: mib(result.documentBytes),
        ok: result.ok,
        rows: result.rows.length,
        executionMs: Number(result.executionMs.toFixed(0)),
        wallMs: Date.now() - started,
        error: result.error?.kind ?? null,
      });
    } catch (err) {
      ceiling.push({
        targetMiB,
        documentMiB: mib(pad),
        ok: false,
        failed: String(err).split("\n")[0],
        wallMs: Date.now() - started,
      });
      break; // Past the first hard failure the numbers say nothing useful.
    }
    // Stop once a single document costs more than three minutes: whatever the
    // memory ceiling turns out to be, nobody reaches it before giving up.
    if (Date.now() - started > 180_000) break;
  }

  const machine = {
    os: `${platform()} ${release()} (${arch()})`,
    model: (() => {
      try {
        return execSync("sysctl -n hw.model", { encoding: "utf8" }).trim();
      } catch {
        return "unknown";
      }
    })(),
    cpu: cpus()[0]?.model ?? "unknown",
    cores: cpus().length,
    memoryGiB: Number((totalmem() / 1024 ** 3).toFixed(1)),
    browser: `Chromium ${browser.version()} (Playwright, headless)`,
  };

  const measurements = {
    measuredAt: new Date().toISOString(),
    machine,
    runtime: info,
    assets: assetSizes(),
    coldPreparationMs,
    cachedPreparationMs,
    perCase,
    filingSizeCeiling: ceiling,
    consoleErrors,
  };

  writeFileSync(join(WEB, "measurements.json"), JSON.stringify(measurements, null, 2) + "\n");

  // --- report ---------------------------------------------------------------
  console.log(`\n${machine.model} · ${machine.cpu} · ${machine.cores} cores · ${machine.memoryGiB} GiB`);
  console.log(`${machine.os} · ${machine.browser}`);
  console.log(`pyodide ${info.pyodideVersion} · python ${info.pythonVersion} · lxml ${info.lxmlVersion} · sec-tables ${info.secTablesVersion}\n`);

  console.log("download                                              raw MB   gzip MB");
  const byGroup = {};
  for (const a of measurements.assets) {
    byGroup[a.group] ??= { bytes: 0, gzipBytes: 0 };
    byGroup[a.group].bytes += a.bytes;
    byGroup[a.group].gzipBytes += a.gzipBytes;
    console.log(`  ${a.label.padEnd(50)} ${mib(a.bytes).toFixed(2).padStart(7)}  ${mib(a.gzipBytes).toFixed(2).padStart(8)}`);
  }
  console.log("  " + "-".repeat(68));
  let total = 0, totalGz = 0;
  for (const [group, g] of Object.entries(byGroup)) {
    total += g.bytes; totalGz += g.gzipBytes;
    console.log(`  ${group.padEnd(50)} ${mib(g.bytes).toFixed(2).padStart(7)}  ${mib(g.gzipBytes).toFixed(2).padStart(8)}`);
  }
  console.log(`  ${"TOTAL".padEnd(50)} ${mib(total).toFixed(2).padStart(7)}  ${mib(totalGz).toFixed(2).padStart(8)}`);

  console.log(`\ncold preparation (empty cache): ${coldPreparationMs} ms`);
  console.log(`cold preparation (warm HTTP cache, new page): ${cachedPreparationMs} ms`);

  console.log("\nextraction                        doc KB  backend   rows   first ms   warm ms");
  for (const c of perCase) {
    console.log(
      `  ${c.id.padEnd(24)} ${(c.documentBytes / 1024).toFixed(1).padStart(9)}  ${(c.backend ?? "-").padEnd(8)} ${String(c.rows).padStart(5)} ${String(c.firstMs).padStart(10)} ${String(c.warmMs).padStart(9)}`,
    );
  }

  console.log("\nfiling size sweep       doc MB    ok   rows   exec ms   note");
  for (const c of ceiling) {
    console.log(
      `  ${String(c.targetMiB + " MiB pad").padEnd(18)} ${String(c.documentMiB).padStart(8)}  ${String(c.ok).padEnd(5)} ${String(c.rows ?? "-").padStart(5)} ${String(c.executionMs ?? "-").padStart(9)}   ${c.failed ?? c.error ?? ""}`,
    );
  }

  console.log(`\nconsole errors during measurement: ${consoleErrors.length}`);
  console.log("wrote measurements.json");

  await browser.close();
  server.kill();
}

await main();
