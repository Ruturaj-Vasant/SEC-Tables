"""Coverage and plausibility measurement over a local filing corpus.

This does not claim accuracy — that needs hand-labelled ground truth. What it
measures is (a) how often a table is found at all, stratified by era, and (b) how
often the result is internally *implausible*, which is a lower bound on the error
rate that needs no labels:

  * a row whose components do not sum to its stated Total (post-2006 only, where
    Total is mandated)
  * a name that is really a job title
  * a salary outside any believable range
  * a year inconsistent with the filing date

Reported per era, because the eras have different backends and different failure
modes, and a single blended number hides the only thing worth knowing.

Usage:
    python bench/measure.py --root /path/to/data --limit 400
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import sys
from datetime import datetime, timezone
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sec_tables as st  # noqa: E402
from sec_tables import profiles as _profiles  # noqa: E402
from sec_tables.postprocess import looks_like_title  # noqa: E402

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Shared with the CLI so both agree on what counts as a warning.
from sec_tables.types import REVIEW_FLAGS  # noqa: E402


def era_bucket(d: date) -> str:
    if d.year <= 2000:
        return "1994-2000"
    if d.year <= 2005:
        return "2001-2005"
    if d.year <= 2010:
        return "2006-2010"
    return "2011+"


def find_filings(root: Path, forms=("DEF_14A", "DEF 14A")) -> list[tuple[Path, date]]:
    out: list[tuple[Path, date]] = []
    for form in forms:
        for path in root.glob(f"*/{form}/*"):
            if path.suffix.lower() not in (".html", ".htm", ".txt"):
                continue
            m = DATE_RE.search(path.name)
            if not m:
                continue
            try:
                out.append((path, date(*(int(g) for g in m.groups()))))
            except ValueError:
                continue
    return out


# Per-profile textual evidence that the table exists in the document at all.
# NOTE: this shares vocabulary with the extractor, so the resulting figure is
# extraction YIELD, not independently measured recall. See MASTER_PROMPT.md.
_HEADER_EVIDENCE = {
    "summary_compensation": ("principal position", "name and principal", "name of executive"),
    "director_compensation": ("fees earned", "paid in cash", "annual retainer"),
    "beneficial_ownership": ("beneficial owner", "percent of class", "beneficially owned"),
}


def document_contains_table(raw: bytes, profile_name: str) -> bool:
    """Whether the document plausibly contains an SCT at all.

    Many plain-text renditions mention the Summary Compensation Table in prose
    but omit the table itself — the intro paragraph runs straight into footnote
    (1). Counting those as extraction failures understates coverage and points
    debugging at the wrong layer: the fix is re-acquiring the document, not
    changing the parser. Absence of the mandated identifying header is the test.
    """
    evidence = _HEADER_EVIDENCE.get(profile_name)
    if not evidence:
        return True  # no evidence rule: assume present, never inflate yield
    text = raw.decode("utf-8", errors="ignore").lower()
    return any(h in text for h in evidence)


def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_plausibility(table, era: str, filing_year: int, prof) -> list[str]:
    """Internal-consistency problems, driven entirely by the profile.

    Nothing here knows which table it is looking at. A profile declares its
    identity column, its year column, believable numeric ranges, and whether it
    has a stated total that its components should sum to; this walks that spec.

    Empty list means nothing detectable — NOT that the row is correct.
    """
    problems: list[str] = []
    idx = {r: i for i, r in enumerate(table.roles)}

    def col(row, role):
        i = idx.get(role)
        return row[i] if i is not None and i < len(row) else ""

    if prof.identity_role and prof.identity_is_person and prof.identity_role in idx:
        group_i = idx.get("is_group")
        for row in table.rows:
            # Group subtotal rows legitimately read as titles ("All directors and
            # executive officers as a group"); they are aggregates, not names.
            if group_i is not None and (row[group_i] if group_i < len(row) else "") == "1":
                continue
            value = col(row, prof.identity_role)
            if value and looks_like_title(value):
                problems.append("name_is_a_title")
                break

    if prof.year_role and prof.year_role in idx:
        for row in table.rows:
            y = _f(col(row, prof.year_role))
            # A proxy reports the fiscal years up to the filing year.
            if y is not None and not (filing_year - 12 <= y <= filing_year + 1):
                problems.append("year_out_of_range")
                break

    for bound in prof.value_bounds:
        if bound.role not in idx:
            continue
        for row in table.rows:
            v = _f(col(row, bound.role))
            if v is not None and v != 0 and not (bound.low <= v <= bound.high):
                problems.append(f"{bound.role}_implausible")
                break

    sc = prof.sum_check
    if sc and sc.total_role in idx:
        for row in table.rows:
            total = _f(col(row, sc.total_role))
            parts = [_f(col(row, c)) for c in sc.component_roles if c in idx]
            if total is None or total <= 0 or not parts or any(p is None for p in parts):
                continue
            s_ = sum(p for p in parts if p is not None)
            if abs(s_ - total) > max(sc.tolerance * total, 1.0):
                problems.append("total_mismatch")
                break

    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--table", default="summary_compensation",
                    help=f"profile: {sorted(_profiles.REGISTRY)} or an alias")
    args = ap.parse_args()
    prof = _profiles.get(args.table)

    filings = find_filings(args.root)
    if not filings:
        print(f"no filings under {args.root}", file=sys.stderr)
        return 1

    # Stratify so the small early era is not swamped by the large recent one.
    by_era: dict[str, list] = defaultdict(list)
    for path, d in filings:
        by_era[era_bucket(d)].append((path, d))

    rng = random.Random(args.seed)
    per_era = max(1, args.limit // max(1, len(by_era)))
    sample: list[tuple[Path, date]] = []
    for era, items in by_era.items():
        rng.shuffle(items)
        sample.extend(items[:per_era])

    # Per-filing outcomes. A fixed seed does not reproduce a sample once the
    # corpus changes, so the manifest records exactly which documents were used
    # and what each produced — otherwise a number cannot be re-derived or
    # disputed later.
    manifest_rows: list[dict] = []

    stats: dict[str, Counter] = defaultdict(Counter)
    backends: dict[str, Counter] = defaultdict(Counter)
    flags: dict[str, Counter] = defaultdict(Counter)
    problems: dict[str, Counter] = defaultdict(Counter)
    rows_found: dict[str, int] = defaultdict(int)

    for path, d in sorted(sample):
        bucket = era_bucket(d)
        stats[bucket]["filings"] += 1
        try:
            raw = path.read_bytes()
        except OSError:
            stats[bucket]["unreadable"] += 1
            continue

        try:
            r = st.extract(raw, profile=prof, filing_date=d)
        except Exception as exc:  # a crash is a result worth counting
            stats[bucket]["crashed"] += 1
            flags[bucket][f"exception:{type(exc).__name__}"] += 1
            continue

        for f in r.flags:
            flags[bucket][f] += 1

        row = {
            "path": str(path.relative_to(args.root)) if args.root in path.parents else str(path),
            "sha256": hashlib.sha256(raw).hexdigest()[:16],
            "bytes": len(raw),
            "filing_date": d.isoformat(),
            "era": bucket,
            "extracted": bool(r.ok),
            "backend": r.backend.value if r.backend else None,
            "schema_era": r.era,
            "flags": list(r.flags),
            "rows": len(r.table.rows) if r.table else 0,
        }

        if not r.ok:
            row["outcome"] = "not_applicable" if "predates_mandate" in r.flags else (
                "missed" if document_contains_table(raw, prof.name) else "table_absent")
            manifest_rows.append(row)
            if "predates_mandate" in r.flags:
                stats[bucket]["not_applicable"] += 1
            elif document_contains_table(raw, prof.name):
                stats[bucket]["missed"] += 1
            else:
                stats[bucket]["table_absent"] += 1
            continue

        stats[bucket]["extracted"] += 1
        backends[bucket][r.backend.value] += 1
        rows_found[bucket] += len(r.table.rows)

        probs = check_plausibility(r.table, r.era, d.year, prof)
        review = REVIEW_FLAGS & set(r.flags)
        if probs:
            stats[bucket]["implausible"] += 1
            for p in probs:
                problems[bucket][p] += 1
        else:
            stats[bucket]["clean"] += 1
        # Strict: no implausibility AND no flag asking for review.
        if not probs and not review:
            stats[bucket]["strict"] += 1

        row["outcome"] = "strict" if (not probs and not review) else (
            "clean" if not probs else "implausible")
        row["problems"] = probs
        row["review_flags"] = sorted(review)
        manifest_rows.append(row)

    order = ["1994-2000", "2001-2005", "2006-2010", "2011+"]
    print(f"\ntable: {prof.name}   corpus: {len(filings)} filings found · {len(sample)} sampled\n")
    hdr = (f"{'era':11s} {'n':>5s} {'absent':>7s} {'avail':>6s} {'found':>6s} "
           f"{'yield':>7s} {'clean':>6s} {'clean%':>7s} {'strict':>7s} {'strict%':>8s} {'rows':>6s}")
    print(hdr)
    print("-" * len(hdr))
    totals = Counter()
    for era in order:
        s = stats.get(era)
        if not s:
            continue
        n, found, clean = s["filings"], s["extracted"], s["clean"]
        absent = s["table_absent"] + s["not_applicable"]
        avail = n - absent
        totals.update(s)
        strict = s["strict"]
        print(
            f"{era:11s} {n:5d} {absent:7d} {avail:6d} {found:6d} "
            f"{(found/avail if avail else 0):6.1%} {clean:6d} "
            f"{(clean/found if found else 0):6.1%} {strict:7d} "
            f"{(strict/found if found else 0):7.1%} {rows_found[era]:6d}"
        )
    n, found, clean = totals["filings"], totals["extracted"], totals["clean"]
    absent = totals["table_absent"] + totals["not_applicable"]
    avail = n - absent
    print("-" * len(hdr))
    strict = totals["strict"]
    print(
        f"{'ALL':11s} {n:5d} {absent:7d} {avail:6d} {found:6d} "
        f"{(found/avail if avail else 0):6.1%} {clean:6d} "
        f"{(clean/found if found else 0):6.1%} {strict:7d} "
        f"{(strict/found if found else 0):7.1%} {sum(rows_found.values()):6d}"
    )
    print("\n  absent = table not in the document, or not yet mandated at that date")
    print("  yield   = found / documents that do contain it (NOT independently measured recall)")
    print("  clean%  = share with no detectable value-level implausibility")
    print("  strict% = ALSO no flag requesting review (ambiguous selection, missing/unmapped columns)")
    print("            strict% is the number to trust; clean% flatters results whose")
    print("            columns were never identified.")

    print("\nbackend used, by era")
    for era in order:
        if era in backends:
            print(f"  {era:11s} {dict(backends[era])}")

    print("\nimplausibility detected (lower bound on error)")
    for era in order:
        if problems.get(era):
            print(f"  {era:11s} {dict(problems[era].most_common())}")
    if not any(problems.values()):
        print("  none detected")

    print("\ntop flags, by era")
    for era in order:
        if flags.get(era):
            print(f"  {era:11s} {dict(flags[era].most_common(6))}")

    if args.json_out:
        corpus_fingerprint = hashlib.sha256(
            "|".join(sorted(str(p) for p, _ in filings)).encode()
        ).hexdigest()[:16]
        args.json_out.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "table": prof.name,
            "package_version": st.__version__,
            "python": platform.python_version(),
            "seed": args.seed,
            "limit": args.limit,
            "corpus_root": str(args.root),
            "corpus_files": len(filings),
            "corpus_fingerprint": corpus_fingerprint,
            "sampled": len(sample),
            "manifest": manifest_rows,
            "stats": {k: dict(v) for k, v in stats.items()},
            "backends": {k: dict(v) for k, v in backends.items()},
            "problems": {k: dict(v) for k, v in problems.items()},
            "flags": {k: dict(v) for k, v in flags.items()},
        }, indent=2))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
