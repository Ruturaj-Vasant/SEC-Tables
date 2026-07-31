/**
 * Rebuild the pinned sec-tables wheel from the repository and record its
 * checksum, so the pin can be re-derived rather than trusted.
 *
 * hatchling zeroes timestamps inside the archive, so a rebuild from the same
 * source produces the same bytes and therefore the same digest. If this script
 * prints a digest different from `WHEEL_SHA256` in src/pin.ts, the library
 * changed — that is the check, not a nuisance.
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, readdirSync, copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = join(WEB, "..");
const OUT = join(WEB, "public", "py");
const TMP = join(WEB, ".wheel-build");

mkdirSync(OUT, { recursive: true });
execFileSync("python3", ["-m", "build", "--wheel", "--outdir", TMP], {
  cwd: REPO,
  stdio: "inherit",
});

const wheel = readdirSync(TMP).find((f) => f.endsWith(".whl"));
if (!wheel) throw new Error("no wheel produced");

copyFileSync(join(TMP, wheel), join(OUT, wheel));
const bytes = readFileSync(join(OUT, wheel));
const digest = createHash("sha256").update(bytes).digest("hex");
writeFileSync(join(OUT, `${wheel}.sha256`), `${digest}  ${wheel}\n`);

const pinFile = join(WEB, "src", "pin.ts");
const pinned = /WHEEL_SHA256 =\s*\n?\s*"([0-9a-f]{64})"/.exec(readFileSync(pinFile, "utf8"))?.[1];

console.log(`\n${wheel}`);
console.log(`  size   ${bytes.length} bytes`);
console.log(`  sha256 ${digest}`);
console.log(`  pin.ts ${pinned}  ${digest === pinned ? "MATCH" : "*** MISMATCH — update src/pin.ts ***"}`);
if (digest !== pinned) process.exitCode = 1;
