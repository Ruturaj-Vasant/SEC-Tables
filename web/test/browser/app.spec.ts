/**
 * The visible workflow, in a real browser.
 *
 * Real React, real proxy (the actual `sec_proxy` server, with only SEC's
 * network seam faked), real Pyodide, real wheel. The filing bytes the app
 * receives are the committed fixtures, so the values asserted here are the ones
 * a person read off the filings by eye — the same numbers `tests/test_golden.py`
 * asserts in Python.
 *
 * The live-SEC path is deliberately not exercised here. It has its own opt-in
 * smoke test, because a suite that depends on SEC being reachable fails for
 * reasons that have nothing to do with the code.
 */
import { test as base, expect, type Page } from "@playwright/test";

/**
 * Chromium logs this when something tries to run a script inside the filing
 * frame and the sandbox refuses. It is the control working, not a fault — and
 * in this suite the thing being refused is Playwright's own injected helper,
 * which it puts into every frame in order to query one. Counted separately
 * rather than ignored: the sandbox test asserts these DO appear.
 */
const SANDBOX_BLOCK = /Blocked script execution in 'blob:.*sandboxed/;

/**
 * Chromium logs every non-2xx fetch and every request to a revoked blob as a
 * console error. Those are the server and the browser reporting facts — a 404
 * for an unknown ticker is the proxy's correct answer — not the application
 * misbehaving, so they are counted apart and asserted where they are expected.
 */
const NETWORK_STATUS = /Failed to load resource/;

/** Every test watches the console; a dirty one fails the test. */
const test = base.extend<{
  app: Page;
  consoleErrors: string[];
  sandboxBlocks: string[];
  networkErrors: string[];
}>({
  consoleErrors: async ({}, use) => {
    await use([]);
  },
  sandboxBlocks: async ({}, use) => {
    await use([]);
  },
  networkErrors: async ({}, use) => {
    await use([]);
  },
  app: async ({ page, consoleErrors, sandboxBlocks, networkErrors }, use) => {
    page.on("console", (m) => {
      if (m.type() !== "error") return;
      if (SANDBOX_BLOCK.test(m.text())) sandboxBlocks.push(m.text());
      else if (NETWORK_STATUS.test(m.text())) networkErrors.push(m.text());
      else consoleErrors.push(`console.error: ${m.text()}`);
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
    await page.goto("/app.html");
    await page.waitForFunction(() => document.body.dataset.appReady === "true");
    await use(page);
  },
});

const EMAIL = "browser-test@example.com";

async function fill(
  page: Page,
  values: { ticker: string; year: string; table?: string; form?: string; email?: string },
) {
  await page.fill("#email", values.email ?? EMAIL);
  await page.fill("#ticker", values.ticker);
  await page.fill("#year", values.year);
  if (values.form) await page.selectOption("#form", values.form);
  if (values.table) await page.selectOption("#table", values.table);
}

/** Find + fetch, then wait for the document to be on screen. */
async function findFiling(page: Page) {
  await page.getByRole("button", { name: "Find filing" }).click();
  await expect(page.locator(".filing-frame")).toBeVisible({ timeout: 30_000 });
}

async function extract(page: Page) {
  await page.getByRole("button", { name: "Extract table" }).click();
  await expect(page.getByTestId("status")).toHaveAttribute(
    "data-status",
    /successful|needs_review|no_table|failed|throttled/,
    { timeout: 90_000 },
  );
}

/** A row of the rendered table, by the column names in the header. */
async function rowByName(page: Page, name: string): Promise<Record<string, string>> {
  return page.evaluate((wanted) => {
    const table = document.querySelector(".result table")!;
    const columns = [...table.querySelectorAll("thead th")].map((th) => th.textContent ?? "");
    for (const tr of table.querySelectorAll("tbody tr")) {
      const cells = [...tr.querySelectorAll("td")].map((td) => td.textContent ?? "");
      if (cells[0] === wanted) return Object.fromEntries(columns.map((c, i) => [c, cells[i] ?? ""]));
    }
    return {};
  }, name);
}

test.afterEach(async ({ consoleErrors }) => {
  expect(consoleErrors, "the browser console must stay clean").toEqual([]);
});

// ---------------------------------------------------------------------------
// The acceptance flow
// ---------------------------------------------------------------------------

test("DAL 1997 Summary Compensation: the whole workflow, with verified values", async ({
  app,
}) => {
  await fill(app, { ticker: "DAL", year: "1997", table: "summary_compensation" });
  await findFiling(app);

  // The original filing is on screen, in a sandbox, above the table.
  const frame = app.locator(".filing-frame");
  await expect(frame).toHaveAttribute("sandbox", "");
  await expect(app.locator(".viewer .panel-head h2")).toContainText("DAL DEF 14A · 1997-09-19");
  // A pre-2001 submission comes down the complete-submission route.
  await expect(app.locator(".viewer .panel-head p")).toContainText("complete submission");
  // …and its whitespace is preserved, which is what makes the table readable.
  const inFrame = app.frameLocator(".filing-frame");
  await expect(inFrame.locator("pre")).toContainText("NAME AND PRINCIPAL POSITION");

  await extract(app);
  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "successful");

  // Fifteen person-years: five officers, three years each.
  await expect(app.locator(".result tbody tr")).toHaveCount(15);

  // Ronald W. Allen's 1997 row, read off the filing by eye.
  const allen = await rowByName(app, "Ronald W. Allen");
  expect(allen["year"]).toBe("1997");
  expect(allen["salary"]).toBe("562500");
  expect(allen["bonus"]).toBe("0");
  expect(allen["other_annual_comp"]).toBe("14183");
  expect(allen["options_sars"]).toBe("54000");
  expect(allen["all_other_comp"]).toBe("20568");

  // The pre-2006 schema, and text-derived provenance — not a warning.
  await expect(app.locator(".facts")).toContainText("pre-2006");
  await expect(app.locator(".flags.provenance")).toContainText(/ascii_source|sgml_source/);
  await expect(app.locator(".flags.review")).toHaveCount(0);
});

test("the CSV downloads the same canonical rows that are on screen", async ({ app }) => {
  await fill(app, { ticker: "DAL", year: "1997" });
  await findFiling(app);
  await extract(app);

  const [download] = await Promise.all([
    app.waitForEvent("download"),
    app.getByTestId("download-csv").click(),
  ]);
  expect(download.suggestedFilename()).toBe("dal_1997-09-19_summary_compensation.csv");

  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(chunk as Buffer);
  const csv = Buffer.concat(chunks).toString("utf8");

  const lines = csv.trim().split("\n");
  expect(lines).toHaveLength(16); // header + 15 rows
  expect(lines[0]).toContain("name");
  expect(lines[0]).toContain("salary");
  expect(csv).toContain("562500");
  expect(csv).toContain("14183");

  // The header on screen and the header in the file are the same list.
  const onScreen = await app.$$eval(".result thead th", (ths) => ths.map((t) => t.textContent));
  expect(lines[0].split(",").map((c) => c.replace(/^"|"$/g, ""))).toEqual(onScreen);
});

// ---------------------------------------------------------------------------
// The other backends and profiles
// ---------------------------------------------------------------------------

test("a modern HTML filing extracts director compensation through lxml", async ({ app }) => {
  await fill(app, { ticker: "CMP", year: "2024", table: "director_compensation" });
  await findFiling(app);
  await extract(app);

  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "successful");
  await expect(app.locator(".result tbody tr")).toHaveCount(10);
  await expect(app.locator(".facts")).toContainText("dom");
  // The document is rendered as markup, not as escaped text.
  await expect(app.frameLocator(".filing-frame").locator("table").first()).toBeVisible();
  const dealy = await rowByName(app, "Richard P. Dealy");
  expect(dealy["fees_earned"]).toBe("25625");
  expect(dealy["total"]).toBe("224484");
});

test("beneficial ownership keeps the address out of the holder name", async ({ app }) => {
  await fill(app, { ticker: "AZZ", year: "2019", table: "beneficial_ownership" });
  await findFiling(app);
  await extract(app);

  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "successful");
  const blackrock = await rowByName(app, "BlackRock, Inc.");
  expect(blackrock["shares"]).toBe("3765728");
  expect(blackrock["percent"]).toBe("14.5");
  expect(blackrock["holder_address"]).toContain("New York");
});

// ---------------------------------------------------------------------------
// Choosing among several filings
// ---------------------------------------------------------------------------

test("a year with two filings shows both and defaults to the later one", async ({ app }) => {
  await fill(app, { ticker: "DAL", year: "1994" });
  await findFiling(app);

  const chooser = app.locator("#filing");
  await expect(chooser).toBeVisible();
  await expect(chooser.locator("option")).toHaveCount(2);
  await expect(app.locator('label[for="filing"]')).toContainText("2 match this year");
  // `pick_filing`'s default, for its reason: a second proxy usually corrects the first.
  await expect(chooser).toHaveValue(await chooser.locator("option").last().getAttribute("value") ?? "");

  // Choosing the earlier one re-fetches and re-labels the viewer.
  const first = await chooser.locator("option").first().getAttribute("value");
  await chooser.selectOption(first!);
  await expect(app.locator(".viewer .panel-head h2")).toContainText("1994-09-13");
});

// ---------------------------------------------------------------------------
// Failure states
// ---------------------------------------------------------------------------

test("form errors are reported per field before anything is sent", async ({ app }) => {
  await fill(app, { ticker: "", year: "1899", email: "not-an-email" });
  await page_click_find(app);

  await expect(app.locator("#email-error")).toBeVisible();
  await expect(app.locator("#ticker-error")).toBeVisible();
  await expect(app.locator("#year-error")).toContainText("1993");
  // Nothing was fetched, so no document appeared.
  await expect(app.locator(".filing-frame")).toHaveCount(0);
});

async function page_click_find(page: Page) {
  await page.getByRole("button", { name: "Find filing" }).click();
}

test("a ticker SEC does not know is a clear message, not a crash", async ({
  app,
  networkErrors,
}) => {
  await fill(app, { ticker: "ZZZZ", year: "2023" });
  await page_click_find(app);
  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "failed", {
    timeout: 30_000,
  });
  await expect(app.getByTestId("status")).toContainText(/no CIK|no DEF 14A/i);
  // The proxy answered 404, which is the right status for "SEC has no such
  // issuer" — and the app turned it into a sentence rather than a stack trace.
  expect(networkErrors.join(" ")).toContain("404");
});

test("asking for a table the filing does not contain is a result, not an error", async ({
  app,
}) => {
  // A 1997 proxy predates Item 402(r) entirely: directors' pay was prose.
  await fill(app, { ticker: "DAL", year: "1997", table: "director_compensation" });
  await findFiling(app);
  await extract(app);

  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "no_table");
  await expect(app.locator(".result")).toContainText("No table of this type");
});

// ---------------------------------------------------------------------------
// Runtime safety
// ---------------------------------------------------------------------------

test("a filing that hangs extraction is stopped, and the app recovers", async ({ app }) => {
  // The malformed-colspan case: a few hundred bytes that expand the grid until
  // the worker stops responding. It never raises and never returns, so a size
  // limit would not catch it and there is nothing to await.
  await fill(app, { ticker: "HANG", year: "2020" });
  await findFiling(app);

  await app.evaluate(() => {
    (window as any).__secTablesTimeoutMs = 4000;
  });
  await app.getByRole("button", { name: "Extract table" }).click();

  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "failed", {
    timeout: 60_000,
  });
  await expect(app.getByTestId("status")).toContainText(/did not finish/);
  await expect(app.locator(".callout.bad")).toContainText("terminated and rebuilt");

  // And the rebuilt runtime still works: a good filing extracts afterwards.
  await fill(app, { ticker: "DAL", year: "1997", table: "summary_compensation" });
  await findFiling(app);
  await extract(app);
  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "successful");
  await expect(app.locator(".result tbody tr")).toHaveCount(15);
});

// ---------------------------------------------------------------------------
// Presentation
// ---------------------------------------------------------------------------

test("the filing is sandboxed and cannot reach the application", async ({ app, sandboxBlocks }) => {
  await fill(app, { ticker: "CMP", year: "2024", table: "director_compensation" });
  await findFiling(app);

  const frame = app.locator(".filing-frame");
  await expect(frame).toHaveAttribute("sandbox", "");
  await expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
  // An empty sandbox means an opaque origin: no scripts, no same-origin access.
  const reachable = await app.evaluate(() => {
    const iframe = document.querySelector(".filing-frame") as HTMLIFrameElement;
    try {
      return iframe.contentDocument !== null;
    } catch {
      return false;
    }
  });
  expect(reachable, "the page must not be able to read into the filing frame").toBe(false);
  // And the filing's markup is never inlined into this document.
  const inlined = await app.evaluate(() => document.body.innerHTML.includes("Compass Minerals"));
  expect(inlined).toBe(false);

  // Positive evidence rather than an absence: reading inside the frame makes
  // Playwright inject a helper script, and the browser refuses to run it. If
  // this list were empty the sandbox would not be doing anything.
  await app.frameLocator(".filing-frame").locator("body").count().catch(() => 0);
  expect(sandboxBlocks.length, "the sandbox must actually refuse scripts").toBeGreaterThan(0);
});

test("the blob URL is released when the filing changes", async ({ app, networkErrors }) => {
  await fill(app, { ticker: "DAL", year: "1997" });
  await findFiling(app);
  const firstSrc = await app.locator(".filing-frame").getAttribute("src");

  await fill(app, { ticker: "AZZ", year: "2019", table: "beneficial_ownership" });
  await findFiling(app);
  const secondSrc = await app.locator(".filing-frame").getAttribute("src");
  expect(secondSrc).not.toBe(firstSrc);

  // The old blob: URL is revoked, so fetching it now fails.
  const stillThere = await app.evaluate(async (url) => {
    try {
      const response = await fetch(url!);
      return response.ok;
    } catch {
      return false;
    }
  }, firstSrc);
  expect(stillThere, "a revoked blob URL must not still resolve").toBe(false);
});

test("review warnings are visually separate from provenance, on one result", async ({
  app,
}) => {
  // A real 1997 filing that raises both kinds at once: `sgml_source` describes
  // where the answer came from, `ambiguous_selection` and
  // `missing_required_columns` say it should not be used unlooked-at.
  await fill(app, { ticker: "ABCP", year: "1997" });
  await findFiling(app);
  await extract(app);

  await expect(app.getByTestId("status")).toHaveAttribute("data-status", "needs_review");
  const review = app.locator(".flags.review");
  const provenance = app.locator(".flags.provenance");
  await expect(review).toContainText("ambiguous_selection");
  await expect(review).toContainText("missing_required_columns");
  await expect(provenance).toContainText("sgml_source");
  await expect(provenance).toContainText("not a defect");
  // The provenance flag must not appear among the warnings, and vice versa.
  await expect(review).not.toContainText("sgml_source");
  await expect(provenance).not.toContainText("ambiguous_selection");

  // They are told apart visually too, not only by heading.
  const [reviewColour, provenanceColour] = await app.evaluate(() => [
    getComputedStyle(document.querySelector(".flags.review h4")!).color,
    getComputedStyle(document.querySelector(".flags.provenance h4")!).color,
  ]);
  expect(reviewColour).not.toBe(provenanceColour);

  // And the result is not dressed up as a success.
  await expect(app.locator(".callout.review")).toContainText("asks for review");
});

test("nothing on the page claims the result is verified or accurate", async ({ app }) => {
  await fill(app, { ticker: "DAL", year: "1997" });
  await findFiling(app);
  await extract(app);
  const text = (await app.locator("body").innerText()).toLowerCase();
  expect(text).not.toContain("verified");
  expect(text).not.toContain("accurate");
  expect(text).toContain("heuristic");
});

test("the product shell does not invent uploads, branding, or client-only fetching", async ({ app }) => {
  const text = (await app.locator("body").innerText()).toLowerCase();
  expect(await app.locator('input[type="file"]').count()).toBe(0);
  expect(text).not.toContain("ibm");
  expect(text).not.toContain("100% client-side");
  expect(text).not.toContain("file never leaves your device");
  expect(text).toContain("this server downloads the filing");
  expect(text).toContain("python extracts and normalizes it in your browser");
});

test("the contact field is honest about the address before it is typed", async ({ app }) => {
  const hint = app.locator("#email-hint");
  // Where it goes, and in what.
  await expect(hint).toContainText("Sent to SEC");
  await expect(hint).toContainText("User-Agent");
  // Whose requirement it is. SEC asks the *requester* to identify itself; it
  // does not ask websites to collect their visitors' addresses, and the page
  // must not imply otherwise.
  await expect(hint).toContainText("design choice");
  await expect(hint).toContainText("not an SEC rule");
  // What is promised, scoped to what this code can actually promise.
  await expect(hint).toContainText("does not intentionally store or log");
  await expect(hint).toContainText(/hosting providers|intermediaries|extensions/);
  // And what is not claimed.
  const text = await hint.innerText();
  expect(text.toLowerCase()).not.toContain("sec requires");
  expect(text.toLowerCase()).not.toContain("guaranteed");
});

test("the address never reaches a URL, and no response echoes it back", async ({ app }) => {
  const urls: string[] = [];
  const bodies: string[] = [];
  app.on("request", (r) => urls.push(r.url()));
  app.on("response", async (r) => {
    if (!r.url().includes("/api/")) return;
    try {
      bodies.push(JSON.stringify(await r.headerValues("x-filing-meta")) + (await r.text()).slice(0, 4000));
    } catch {
      /* a binary filing body is not text; its headers are captured above */
    }
  });

  const address = "privacy-probe@example.com";
  await fill(app, { ticker: "DAL", year: "1997", email: address });
  await findFiling(app);

  // Every API call is a POST, so the address is in a body — which no access log,
  // Referer or browser history records.
  expect(urls.filter((u) => u.includes(address))).toEqual([]);
  expect(bodies.filter((b) => b.includes(address))).toEqual([]);
});

// ---------------------------------------------------------------------------
// Layout and keyboard
// ---------------------------------------------------------------------------

test("the layout reflows on a narrow viewport without horizontal scroll", async ({ app }) => {
  await app.setViewportSize({ width: 390, height: 844 });
  await fill(app, { ticker: "DAL", year: "1997" });
  await findFiling(app);
  await extract(app);

  const overflow = await app.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "the page itself must not scroll sideways").toBeLessThanOrEqual(1);
  // The table is allowed to scroll — inside its own container.
  const tableScrolls = await app.evaluate(() => {
    const box = document.querySelector(".table-scroll") as HTMLElement;
    return getComputedStyle(box).overflowX;
  });
  expect(tableScrolls).toBe("auto");
});

test("the whole form is reachable and operable from the keyboard", async ({ app }) => {
  // The product header is keyboard navigation too. Walk through it before the
  // form rather than making the old, false assertion that the email field is
  // the first interactive element on the page.
  for (const name of ["sec-tables home", "How it works", "Supported tables", "Methodology", "GitHub"]) {
    await app.keyboard.press("Tab");
    await expect(app.getByRole("link", { name })).toBeFocused();
  }
  await app.keyboard.press("Tab");
  await expect(app.locator("#email")).toBeFocused();
  await app.keyboard.type(EMAIL);
  await app.keyboard.press("Tab");
  await expect(app.locator("#ticker")).toBeFocused();
  await app.keyboard.type("DAL");
  await app.keyboard.press("Tab");
  await expect(app.locator("#year")).toBeFocused();
  await app.keyboard.type("1997");

  // Enter submits from a text field, so the form is usable without a mouse.
  await app.keyboard.press("Enter");
  await expect(app.locator(".filing-frame")).toBeVisible({ timeout: 30_000 });
});

test("invalid fields are announced to assistive technology", async ({ app }) => {
  await fill(app, { ticker: "DAL", year: "1997", email: "bad" });
  await page_click_find(app);
  const email = app.locator("#email");
  await expect(email).toHaveAttribute("aria-invalid", "true");
  await expect(email).toHaveAttribute("aria-describedby", /email-error/);
  await expect(app.locator("#email-error")).toHaveAttribute("role", "alert");
});
