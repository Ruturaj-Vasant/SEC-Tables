/**
 * The one test that talks to SEC. Opt-in, never in CI.
 *
 * `SEC_LIVE=1` plus a real proxy:
 *
 *   PROXY_CMD="python3 tools/live_sec.py" SEC_LIVE=1 \
 *     SEC_LIVE_EMAIL=you@example.com npx playwright test live
 *
 * It exists because everything else in this suite is fixture-backed, and a
 * fixture cannot tell you that SEC still answers, that the submissions API
 * still paginates the way it did, or that the 1997 archive path still resolves.
 * It is kept out of the default run because a suite that fails when a regulator
 * is slow is a suite people learn to ignore.
 *
 * If the live rendition ever disagrees with the committed fixture, that is a
 * finding about acquisition — a different document, not a broken parser — and
 * the difference gets investigated and written down rather than absorbed by
 * loosening the expected values.
 */
import { test, expect } from "@playwright/test";

const LIVE = process.env.SEC_LIVE === "1";
const EMAIL = process.env.SEC_LIVE_EMAIL ?? "";

test.describe("live EDGAR", () => {
  test.skip(!LIVE, "set SEC_LIVE=1 and PROXY_CMD to the real proxy to run this");
  test.skip(!EMAIL, "set SEC_LIVE_EMAIL to a real, monitored address — SEC requires one");

  test("DAL 1997 DEF 14A, fetched from SEC and extracted in the browser", async ({ page }) => {
    const problems: string[] = [];
    page.on("pageerror", (e) => problems.push(e.message));

    await page.goto("/app.html");
    await page.waitForFunction(() => document.body.dataset.appReady === "true");

    await page.fill("#email", EMAIL);
    await page.fill("#ticker", "DAL");
    await page.fill("#year", "1997");
    await page.selectOption("#table", "summary_compensation");
    await page.getByRole("button", { name: "Find filing" }).click();

    await expect(page.locator(".filing-frame")).toBeVisible({ timeout: 120_000 });
    const source = await page.locator(".viewer .panel-head a").getAttribute("href");
    expect(source, "the document must come from SEC's archives").toMatch(
      /^https:\/\/www\.sec\.gov\/Archives\/edgar\/data\//,
    );
    // Pre-May-2000, so the complete-submission route rather than a primary document.
    expect(source).toMatch(/\.txt$/);

    await page.getByRole("button", { name: "Extract table" }).click();
    await expect(page.getByTestId("status")).toHaveAttribute(
      "data-status",
      /successful|needs_review/,
      { timeout: 180_000 },
    );

    const rows = await page.locator(".result tbody tr").count();
    const allen = await page.evaluate(() => {
      const table = document.querySelector(".result table")!;
      const columns = [...table.querySelectorAll("thead th")].map((th) => th.textContent ?? "");
      for (const tr of table.querySelectorAll("tbody tr")) {
        const cells = [...tr.querySelectorAll("td")].map((td) => td.textContent ?? "");
        if (cells[0] === "Ronald W. Allen" && cells[columns.indexOf("year")] === "1997") {
          return Object.fromEntries(columns.map((c, i) => [c, cells[i] ?? ""]));
        }
      }
      return {} as Record<string, string>;
    });

    // Reported before asserting, so a difference between the live rendition and
    // the committed fixture is visible as data rather than as a bare failure.
    console.log(`live DAL 1997: ${rows} rows, source ${source}`);
    console.log(`live Allen 1997: ${JSON.stringify(allen)}`);

    expect(allen["salary"]).toBe("562500");
    expect(allen["bonus"]).toBe("0");
    expect(allen["other_annual_comp"]).toBe("14183");
    expect(allen["options_sars"]).toBe("54000");
    expect(allen["all_other_comp"]).toBe("20568");
    expect(problems).toEqual([]);
  });
});
