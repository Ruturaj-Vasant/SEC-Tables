/**
 * Hand-verified expected values, copied verbatim from `tests/test_golden.py`.
 *
 * These are the numbers a person read off the filings by eye. They are repeated
 * here rather than derived from the library, because a browser suite that
 * compared the library against itself would happily lock in a wrong answer —
 * which is precisely the failure mode the Python suite's comment warns about.
 *
 * The parity check in `bridge.spec.ts` covers the *whole* output; this file
 * covers the part that is known to be true.
 */
import { test as base, expect, type Page } from "@playwright/test";
import expectedCases from "../expected/cases.json" with { type: "json" };

export { expect, expectedCases };

export type CaseId = keyof typeof expectedCases;

export interface ExtractionResult {
  schemaVersion: number;
  ok: boolean;
  profile: string;
  era: string | null;
  backend: "dom" | "sgml" | "ascii" | "narrative" | null;
  columns: string[];
  rows: string[][];
  flags: string[];
  reviewRequired: boolean;
  provenance: string[];
  metadata: Record<string, unknown>;
  preparationMs: number;
  executionMs: number;
  error?: { kind: string; message: string };
}

/** Pull named columns out of a result, the way the Python tests index roles. */
export function pick(result: ExtractionResult, roles: string[]): string[][] {
  const index = roles.map((role) => {
    const i = result.columns.indexOf(role);
    if (i < 0) throw new Error(`role ${role} not in ${JSON.stringify(result.columns)}`);
    return i;
  });
  return result.rows.map((row) => index.map((i) => row[i]));
}

export function contains(rows: string[][], row: string[]): boolean {
  return rows.some((candidate) => JSON.stringify(candidate) === JSON.stringify(row));
}

// --- tests/test_golden.py :: TestDelta1997 ---------------------------------
export const DAL_1997_ROLES = [
  "name", "year", "salary", "bonus", "other_annual_comp",
  "restricted_stock_awards", "options_sars", "ltip_payouts", "all_other_comp",
];
export const DAL_1997_EXPECTED = [
  ["Ronald W. Allen", "1997", "562500", "0", "14183", "0", "54000", "0", "20568"],
  ["Ronald W. Allen", "1996", "475000", "532594", "12517", "0", "66000", "0", "15504"],
  ["Ronald W. Allen", "1995", "475000", "560625", "11667", "390000", "89000", "0", "15876"],
  ["Maurice W. Worth", "1997", "333333", "205743", "8639", "0", "21000", "0", "13700"],
  ["Maurice W. Worth", "1996", "282500", "237500", "7123", "0", "26000", "0", "11823"],
  ["Maurice W. Worth", "1995", "251250", "187500", "6375", "124800", "19500", "0", "12064"],
];

// --- tests/test_golden.py :: TestDelta1994IndentedRuler --------------------
export const DAL_1994_EXPECTED = [
  ["Ronald W. Allen", "1994", "475000", "0", "8528", "0", "89000", "0", "18512"],
  ["Ronald W. Allen", "1993", "487500", "0", "7077", "0", "0", "0", "17639"],
  ["Harold C. Alger", "1994", "261250", "0", "4713", "0", "35400", "0", "13416"],
];

// --- tests/test_golden.py :: TestDirectorCompensation ----------------------
export const CMP_2024_ROLES = ["name", "fees_earned", "stock_awards", "total"];
export const CMP_2024_EXPECTED = [
  ["Richard P. Dealy", "25625", "198859", "224484"],
  ["Edward C. Dowling, Jr.", "13125", "219493", "232618"],
  ["Eric Ford", "25625", "189720", "215345"],
  ["Jill V. Gardiner", "", "136619", "136619"],
  ["Gareth T. Joyce", "23750", "196311", "220061"],
  ["Melissa M. Miller", "23750", "197902", "221652"],
  ["Joseph E. Reece", "", "400085", "400085"],
  ["Lori A. Walker", "28125", "210082", "238207"],
  ["Paul S. Williams", "37472", "", "37472"],
  ["Amy J. Yoder", "39042", "", "39042"],
];

// --- tests/test_golden.py :: TestBeneficialOwnership -----------------------
export const AZZ_2019_ROLES = ["holder_name", "shares", "percent"];
export const AZZ_2019_EXPECTED = [
  ["BlackRock, Inc.", "3765728", "14.5"],
  ["The Vanguard Group, Inc.", "2619321", "10.04"],
  ["T. Rowe Price Associates, Inc.", "1853610", "7.1"],
  ["Van Berkom & Associates Inc.", "1405056", "5.39"],
];

// --- tests/test_golden.py :: TestAsciiOwnership ----------------------------
export const CVS_1996_ROLES = ["holder_name", "share_class", "shares", "percent"];
export const CVS_1996_EXPECTED = [
  ["FMR Corp.(1)", "Common Stock", "13552054", "12.8"],
  ["Brinson Partners, Inc.(2)", "Common Stock", "6904354", "6.5"],
];

// --- tests/test_golden.py :: TestStackedPersonYearRows ---------------------
export const AAPL_2003_ROLES = ["name", "year", "salary", "bonus"];
export const AAPL_2003_EXPECTED = [
  ["Steven P. Jobs", "2002", "1", "2268698"],
  ["Steven P. Jobs", "2001", "1", "43511534"],
  ["Steven P. Jobs", "2000", "1", ""],
  ["Fred D. Anderson", "2002", "656631", ""],
  ["Fred D. Anderson", "2001", "657039", ""],
  ["Fred D. Anderson", "2000", "660414", ""],
  ["Timothy D. Cook", "2001", "452219", "500000"],
];

/**
 * Every test gets a page whose console is watched.
 *
 * A worker that throws inside `onmessage`, a wasm abort, an unhandled rejection
 * in preparation — none of those necessarily fail an assertion, but all of them
 * print. The suite treats a dirty console as a failure so those cannot pass
 * unnoticed.
 */
export const test = base.extend<{ harness: Page; browserErrors: string[] }>({
  browserErrors: async ({}, use) => {
    await use([]);
  },
  harness: async ({ page, browserErrors }, use) => {
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(`console.error: ${message.text()}`);
    });
    page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
    await page.goto("/index.html");
    await page.waitForFunction(() => document.body.dataset.ready === "true");
    await use(page);
  },
});
