"""Generate the expectations the browser suite asserts against.

Runs `src/bridge.py` — the *same* module the worker loads — under local CPython
against every test document, and writes the results to
`test/expected/cases.json`.

That makes the browser assertions a parity check: identical bridge code, two
interpreters (CPython on the host, CPython-on-wasm in the browser), byte-equal
output or the test fails. It is a different and stronger question than "does it
run", and it catches the failure mode that matters most here — a wasm build of
lxml, or a different Python minor version, quietly parsing a filing differently.

Timing fields are excluded: they are measurements, not results.

Run: python3 tools/gen_expected.py

`../src` is put ahead of everything on `sys.path` on purpose: the wheel is built
from the working tree, so the expectations must come from the working tree too.
An installed `sec-tables` in site-packages can be older than the checkout — and
was, which briefly made the browser look wrong when in fact it was the only side
running the current library.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
REPO = WEB.parent
sys.path.insert(0, str(WEB / "src"))
sys.path.insert(0, str(REPO / "src"))

import sec_tables  # noqa: E402
import sec_bridge  # noqa: E402  (path set above)

assert Path(sec_tables.__file__).is_relative_to(REPO / "src"), (
    f"expectations must come from the working tree, not {sec_tables.__file__}"
)

# (case id, document path, url the browser will fetch it from, filing date, profile)
CASES = [
    ("dal_1997_sct", REPO / "tests/fixtures/dal_1997_sct.txt",
     "/fixtures/dal_1997_sct.txt", "1997-09-19", "summary_compensation"),
    ("dal_1994_sct", REPO / "tests/fixtures/dal_1994_sct.txt",
     "/fixtures/dal_1994_sct.txt", "1994-09-13", "summary_compensation"),
    ("aapl_2003_sct_stacked", REPO / "tests/fixtures/aapl_2003_sct_stacked.html",
     "/fixtures/aapl_2003_sct_stacked.html", "2003-03-24", "summary_compensation"),
    ("cmp_2024_director_comp", REPO / "tests/fixtures/cmp_2024_director_comp.html",
     "/fixtures/cmp_2024_director_comp.html", "2024-01-29", "director_compensation"),
    ("azz_2019_ownership", REPO / "tests/fixtures/azz_2019_ownership.html",
     "/fixtures/azz_2019_ownership.html", "2019-05-28", "beneficial_ownership"),
    ("cvs_1996_ownership", REPO / "tests/fixtures/cvs_1996_ownership.txt",
     "/fixtures/cvs_1996_ownership.txt", "1996-10-08", "beneficial_ownership"),
    # Not library fixtures: two real filings kept here to exercise paths the
    # fixtures do not reach. See test/assets/README.md.
    ("alk_1994_sgml_sct", WEB / "test/assets/alk_1994_sgml_sct.txt",
     "/assets/alk_1994_sgml_sct.txt", "1994-03-31", "summary_compensation"),
    ("abcp_1997_review_flags", WEB / "test/assets/abcp_1997_review_flags.txt",
     "/assets/abcp_1997_review_flags.txt", "1997-02-27", "summary_compensation"),
]

TIMING_FIELDS = ("preparationMs", "executionMs")


def main() -> None:
    out = {}
    for case_id, path, url, filing_date, profile in CASES:
        raw = path.read_bytes()
        result = json.loads(sec_bridge.extract_json(raw, filing_date, profile))
        for field in TIMING_FIELDS:
            result.pop(field, None)
        out[case_id] = {
            "url": url,
            "filingDate": filing_date,
            "profile": profile,
            "documentBytes": len(raw),
            "expected": result,
        }
        table = f"{len(result['rows'])} rows x {len(result['columns'])} cols"
        print(f"{case_id:24s} {result['backend'] or '-':9s} {str(result['era']):9s} "
              f"{table:18s} flags={result['flags']}")

    target = WEB / "test/expected/cases.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {target.relative_to(REPO)}  "
          f"(sec-tables {__import__('sec_tables').__version__}, "
          f"python {'.'.join(str(v) for v in sys.version_info[:3])})")


if __name__ == "__main__":
    main()
