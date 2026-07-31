/**
 * Run the app locally: the static server and the SEC proxy together.
 *
 * `npm run dev` starts the *real* proxy, so it fetches from SEC using whatever
 * contact address is typed into the form. `npm run dev:fake` starts the fixture
 * proxy instead, which is what the test suite uses and which touches no network.
 */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");
const fake = process.argv.includes("--fake");
const PORT = process.env.PORT ?? "5199";
const PROXY_PORT = process.env.PROXY_PORT ?? "5310";

const children = [
  spawn("node", [join(WEB, "tools", "serve.mjs")], {
    stdio: "inherit",
    env: { ...process.env, PORT, PROXY_TARGET: `http://127.0.0.1:${PROXY_PORT}` },
  }),
  spawn("python3", [join(WEB, "tools", fake ? "fake_sec.py" : "live_sec.py")], {
    stdio: "inherit",
    env: { ...process.env, PROXY_PORT },
  }),
];

console.log(
  `\n  app:   http://127.0.0.1:${PORT}/app.html` +
    `\n  proxy: ${fake ? "fixtures (no network)" : "live SEC"}\n`,
);

const stop = () => children.forEach((c) => c.kill());
process.on("SIGINT", () => (stop(), process.exit(0)));
process.on("SIGTERM", () => (stop(), process.exit(0)));
children.forEach((c) => c.on("exit", () => (stop(), process.exit(1))));
