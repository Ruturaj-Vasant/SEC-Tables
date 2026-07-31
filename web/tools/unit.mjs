/**
 * Compile the TypeScript unit tests and run them under `node --test`.
 *
 * No test framework: the assertions are `node:assert`, the runner is the one
 * built into Node, and esbuild is already here for the app. What these cover is
 * the pure half of the interface — validation, the proxy's error vocabulary,
 * flag partitioning, CSV, the state machine — none of which needs a DOM.
 */
import { build } from "esbuild";
import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(WEB, "dist-test");

rmSync(OUT, { recursive: true, force: true });
await build({
  entryPoints: [join(WEB, "app", "test", "domain.test.ts")],
  outdir: OUT,
  bundle: true,
  format: "esm",
  platform: "node",
  target: "node20",
  external: ["node:*"],
  logLevel: "warning",
});

const result = spawnSync(process.execPath, ["--test", OUT], { stdio: "inherit" });
process.exit(result.status ?? 1);
