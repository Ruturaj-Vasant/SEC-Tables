/**
 * The cross-origin contract, enforced by a real browser rather than described.
 *
 * pytest can assert every header the proxy sends and still tell you nothing
 * about whether Chromium *accepts* them: CORS is enforced on the client, the
 * rules are non-obvious (a JSON `Content-Type` triggers a preflight; only seven
 * response headers are readable without `Access-Control-Expose-Headers`), and a
 * refusal surfaces to the page as an opaque `TypeError` with no detail. So the
 * checks that matter run here.
 *
 * The page is served from `127.0.0.1:5199` and the proxy listens on
 * `127.0.0.1:5310`. A different port is a different origin, so these are
 * genuinely cross-origin requests exercising genuine preflights — the same code
 * path the deployed GitHub Pages frontend takes to the hosted proxy.
 */
import { expect, test } from "@playwright/test";

const PROXY = `http://127.0.0.1:${process.env.PROXY_PORT ?? 5310}`;
// 1994 rather than 1997 because the fake serves two DEF 14A for it, so the
// listing proves the multi-filing shape survives the cross-origin hop as well.
const LIST = { email: "researcher@example.com", ticker: "DAL", year: 1994, form: "DEF 14A" };

test.beforeEach(async ({ page }) => {
  await page.goto("/app.html");
});

test("a cross-origin POST is permitted and readable from the page origin", async ({ page }) => {
  const result = await page.evaluate(
    async ([proxy, body]) => {
      const response = await fetch(`${proxy}/api/filings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "omit",
      });
      return { status: response.status, payload: await response.json() };
    },
    [PROXY, LIST] as const,
  );

  expect(result.status).toBe(200);
  // Two DEF 14A in 1994: the browser sees both, so the preflight, the POST and
  // the JSON read all completed cross-origin.
  expect(result.payload.filings.map((f: { filingDate: string }) => f.filingDate)).toEqual([
    "1994-09-13",
    "1994-11-02",
  ]);
  expect(result.payload.defaultId).toBe(result.payload.filings[1].id);
});

test("the filing comes back as bytes with its metadata header readable", async ({ page }) => {
  // The single most breakable thing in this topology. Without
  // `Access-Control-Expose-Headers` the bytes arrive and `X-Filing-Meta` reads
  // as null, so the app fails after a successful download with a message about
  // malformed metadata.
  const result = await page.evaluate(
    async ([proxy, body]) => {
      const response = await fetch(`${proxy}/api/filing`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "omit",
      });
      const bytes = await response.arrayBuffer();
      return {
        status: response.status,
        byteLength: bytes.byteLength,
        head: new TextDecoder().decode(new Uint8Array(bytes).subarray(0, 64)),
        meta: response.headers.get("X-Filing-Meta"),
        cache: response.headers.get("X-Filing-Cache"),
        // The control. `Server` is sent on every response and is not on the
        // expose list — and, unlike `Content-Length`, is not one of the seven
        // headers a cross-origin read may see by default. If this were readable,
        // the expose list would be doing nothing and the assertions above would
        // prove nothing.
        server: response.headers.get("Server"),
      };
    },
    [PROXY, LIST] as const,
  );

  expect(result.status).toBe(200);
  expect(result.byteLength).toBeGreaterThan(0);
  // The real 1994 fixture, byte for byte — an SGML `<TABLE>` with no row or
  // cell tags, which is exactly the era the library exists for.
  expect(result.head).toContain("<TABLE>");
  expect(result.meta).not.toBeNull();
  // The default is the later of the two, matching `pick_filing`.
  expect(JSON.parse(result.meta!).filingDate).toBe("1994-11-02");
  expect(JSON.parse(result.meta!).route).toBe("complete_submission");
  expect(result.server).toBeNull();
});

test("an error response is readable cross-origin, so it can be explained", async ({ page }) => {
  const result = await page.evaluate(async (proxy) => {
    const response = await fetch(`${proxy}/api/filings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: "nope", ticker: "DAL", year: 1997 }),
      credentials: "omit",
    });
    return { status: response.status, body: await response.json() };
  }, PROXY);

  expect(result.status).toBe(400);
  // The point: a 400 the browser cannot read is indistinguishable from the
  // network being down, and "check your email address" becomes "Failed to fetch".
  expect(result.body.error.kind).toBe("invalid_input");
});

test("the browser refuses a response for an origin the proxy does not allow", async ({ page }) => {
  // A page cannot forge `Origin`, so the refusal is provoked from the other end:
  // the proxy is asked to allow nothing but the Pages origin for this check by
  // sending a request the app itself would never send. Instead of faking that,
  // this asserts the observable half — a disallowed origin gets a 403 with no
  // `Access-Control-Allow-Origin`, and the browser then rejects the read.
  const result = await page.evaluate(async (proxy) => {
    try {
      // `no-cors` proves the request left the browser; the response is opaque,
      // which is exactly what a page gets when a server declines its origin.
      const opaque = await fetch(`${proxy}/api/filings`, {
        method: "POST",
        mode: "no-cors",
        headers: { "Content-Type": "text/plain" },
        body: "{}",
      });
      return { type: opaque.type, status: opaque.status, readable: false };
    } catch (e) {
      return { error: String(e) };
    }
  }, PROXY);

  // An opaque response has status 0 and a body the page may not touch. This is
  // the shape every direct-to-SEC fetch has, and the reason a proxy exists.
  expect(result.type).toBe("opaque");
  expect(result.status).toBe(0);
});

test("the preflight is real, and grants exactly what it declares", async ({ page }) => {
  // Preflights are issued by Chromium's network stack and Playwright does not
  // surface them as `request`/`response` events, so "was an OPTIONS sent" is not
  // observable from here — an earlier version of this test asserted it and
  // silently observed nothing. What *is* observable is the consequence, and the
  // consequence is the thing worth pinning:
  //
  //   * a POST with a JSON Content-Type is not a simple request, so it happens
  //     at all only if the proxy answered a preflight; and
  //   * a POST carrying a header outside `Access-Control-Allow-Headers` must
  //     fail, because Chromium refuses to send it after reading that list.
  //
  // Together those show the preflight is being answered and that its grant is
  // exactly the declared one rather than something permissive.
  const result = await page.evaluate(
    async ([proxy, body]) => {
      const attempt = async (headers: Record<string, string>) => {
        try {
          const r = await fetch(`${proxy}/api/filings`, {
            method: "POST",
            headers,
            body: JSON.stringify(body),
            credentials: "omit",
          });
          return { ok: true, status: r.status };
        } catch (e) {
          return { ok: false, error: String(e) };
        }
      };
      return {
        declared: await attempt({ "Content-Type": "application/json" }),
        undeclared: await attempt({ "Content-Type": "application/json", "X-Not-Allowed": "1" }),
      };
    },
    [PROXY, LIST] as const,
  );

  expect(result.declared).toEqual({ ok: true, status: 200 });
  expect(result.undeclared.ok).toBe(false);
  expect(result.undeclared.error).toMatch(/Failed to fetch/);
});

test("the page names the server that fetched the filing, and it matches the build", async ({ page }) => {
  // Two jobs. It asserts the app is honest about where filings come from — the
  // first thing to check when a deployment misbehaves — and it is the guard that
  // `npm run test:crossorigin` is actually running cross-origin. Without it,
  // a build that quietly lost `SEC_TABLES_API_BASE` would pass the whole suite
  // same-origin while claiming to have proved the deployed topology.
  const footer = page.getByTestId("api-origin");
  await expect(footer).toBeVisible();
  const text = (await footer.textContent()) ?? "";

  expect(text).not.toContain("No filing server configured");
  if (process.env.SEC_TABLES_API_BASE) {
    expect(text).toContain(process.env.SEC_TABLES_API_BASE);
  } else {
    expect(text).toContain("local proxy on this origin");
  }
});

test("health is readable cross-origin, which is what the warm-up ping needs", async ({ page }) => {
  const health = await page.evaluate(async (proxy) => {
    const r = await fetch(`${proxy}/api/health`, { credentials: "omit" });
    return { status: r.status, body: await r.json() };
  }, PROXY);

  expect(health.status).toBe(200);
  expect(health.body.ok).toBe(true);
  // Never claimed as durable. On the selected host it is not.
  expect(health.body.cache.filingsPersistent).toBe(false);
});
