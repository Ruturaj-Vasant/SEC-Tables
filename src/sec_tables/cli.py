"""Command line interface.

    sec-tables DAL --year 1997 --table sct -o delta.csv

Design rule: **the warnings are not optional decoration.** A one-liner that hands
someone a clean-looking CSV from an extraction that flagged itself is worse than
no tool at all, so review flags print to stderr every time and `--strict` turns
them into a non-zero exit for scripts.

Provenance is printed separately from warnings. `ascii_source` on a 1997 filing
means the library did the hard thing correctly; presenting it as a problem would
train users to ignore the line that sometimes matters.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from . import profiles as _profiles
from .api import extract
from .cache import FilingCache, default_cache_dir, human_bytes
from .sources import DEFAULT_FORM, FilingRef, LocalSource, SourceError, pick_filing
from .types import Extraction

EXIT_OK = 0
EXIT_NO_TABLE = 1
EXIT_NEEDS_REVIEW = 2  # only with --strict
EXIT_USAGE = 3

_FLAG_HELP = {
    "ambiguous_selection": "two candidate tables tied on score; the pick was decided by tiebreak",
    "missing_required_columns": "a column the regulation mandates was not identified",
    "unmapped_columns": "a header did not map to a known role",
    "era_mismatch": "the filing date and the columns present disagree",
    "below_score_threshold": "the best candidate was too weak to trust",
    "suspect_identity_values": "the name column holds addresses or footnote text; row alignment drifted",
    "ascii_source": "space-aligned plain-text filing (pre-2001 EDGAR)",
    "sgml_source": "SGML <TABLE> block with no row or cell tags",
    "no_filing_date": "no date given, so the schema era could not be pinned",
    "predates_mandate": "this disclosure was not required as of the filing date",
}


def _describe(flag: str) -> str:
    return _FLAG_HELP.get(flag, "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sec-tables",
        description="Find the right table in an SEC filing and turn it into CSV.",
        epilog="Tables: " + ", ".join(sorted(_profiles.REGISTRY)),
    )
    p.add_argument("ticker", nargs="?", help="ticker symbol, e.g. AAPL")
    p.add_argument("--year", type=int, help="filing year")
    p.add_argument(
        "--table", default="summary_compensation",
        help="table to extract (aliases: sct, director, ownership)",
    )
    p.add_argument("--form", default=DEFAULT_FORM, help=f"EDGAR form (default: {DEFAULT_FORM})")
    p.add_argument("-o", "--output", type=Path, help="write CSV here (default: stdout)")

    p.add_argument("--source", choices=("edgar", "local"), default="edgar",
                   help="where filings come from (default: edgar)")
    p.add_argument("--root", type=Path, help="local corpus root (required for --source local)")
    p.add_argument("--cik", help="use this CIK directly, bypassing ticker lookup")
    p.add_argument("--rate", type=float, default=5.0,
                   help="requests/second (SEC ceiling is 10 TOTAL per requester; default 5)")
    # Plain string: nested quotes inside an f-string only parse on 3.12+, and
    # this package supports 3.10.
    p.add_argument("--user-agent",
                   help="SEC User-Agent; defaults to $SEC_USER_AGENT")

    p.add_argument("--cache-dir", type=Path, default=None, help="cache location")
    p.add_argument("--no-cache", action="store_true", help="do not read or write the cache")

    p.add_argument("--prefer", choices=("latest", "earliest"), default="latest",
                   help="which filing to use when a year has several")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if the result asks for review")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress the summary")
    p.add_argument("--list-tables", action="store_true", help="list supported tables and exit")
    p.add_argument("--list-filings", action="store_true",
                   help="list matching filings without extracting")
    p.add_argument("--cache-info", action="store_true", help="show cache statistics and exit")
    p.add_argument("--version", action="version", version=f"sec-tables {__version__}")
    return p


def _print_tables(out) -> int:
    print("Supported tables:\n", file=out)
    aliases: dict[str, list[str]] = {}
    for alias, target in _profiles.ALIASES.items():
        aliases.setdefault(target, []).append(alias)
    for name in sorted(_profiles.REGISTRY):
        prof = _profiles.REGISTRY[name]
        alias = ", ".join(sorted(aliases.get(name, []))) or "-"
        schema = prof.schema
        versions = len(schema.versions) if schema else 0
        mandated = (
            f", mandated from {schema.mandated_from.isoformat()}"
            if schema and schema.mandated_from else ""
        )
        print(f"  {name}", file=out)
        print(f"      aliases   : {alias}", file=out)
        print(f"      row shape : {prof.assembly.value}", file=out)
        print(f"      schema    : {versions} version(s){mandated}", file=out)
    return EXIT_OK


def _report(result: Extraction, ref: FilingRef, dest: str, out) -> None:
    table = result.table
    assert table is not None
    rows, cols = table.shape
    ident = None
    prof = _profiles.get(result.meta.get("profile", "summary_compensation"))
    if prof.identity_role and prof.identity_role in table.roles:
        i = table.roles.index(prof.identity_role)
        ident = len({r[i] for r in table.rows if i < len(r) and r[i]})

    bits = [f"{rows} rows", f"{cols} columns"]
    if ident is not None:
        label = "holders" if prof.assembly.value == "holder" else "people"
        bits.append(f"{ident} {label}")
    if result.era:
        bits.append(f"era={result.era}")
    print(f"[ok] {dest} — {' · '.join(bits)}", file=out)
    print(f"     {ref} via {result.backend.value if result.backend else '?'}", file=out)

    if result.review_flags:
        print("[review recommended]", file=out)
        for f in result.review_flags:
            desc = _describe(f)
            print(f"     - {f}{': ' + desc if desc else ''}", file=out)
    if result.provenance_flags:
        for f in result.provenance_flags:
            desc = _describe(f)
            print(f"     provenance: {f}{' (' + desc + ')' if desc else ''}", file=out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    err = sys.stderr

    if args.list_tables:
        return _print_tables(err)

    cache = FilingCache(args.cache_dir or default_cache_dir(), enabled=not args.no_cache)
    if args.cache_info:
        s = cache.stats()
        print(f"cache: {cache.root}", file=err)
        print(f"  {s['filings']} filings · {s['tickers']} tickers · {human_bytes(s['bytes'])}", file=err)
        return EXIT_OK

    if not args.ticker:
        parser.print_usage(file=err)
        print("error: a ticker is required (or use --list-tables / --cache-info)", file=err)
        return EXIT_USAGE

    try:
        prof = _profiles.get(args.table)
    except KeyError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_USAGE

    if args.source == "local":
        if not args.root:
            print("error: --source local needs --root <corpus directory>", file=err)
            return EXIT_USAGE
        try:
            source = LocalSource(args.root)
        except SourceError as exc:
            print(f"error: {exc}", file=err)
            return EXIT_USAGE
    else:
        from .fetch import FetchError, build_source
        try:
            source = build_source(args.user_agent, rate_per_second=args.rate)
        except FetchError as exc:
            print(f"error: {exc}", file=err)
            return EXIT_USAGE

    list_kwargs = {"form": args.form, "year": args.year}
    if args.source == "edgar" and args.cik:
        list_kwargs["cik"] = args.cik
    try:
        refs = source.list_filings(args.ticker, **list_kwargs)
    except SourceError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_NO_TABLE
    if not refs:
        scope = f" in {args.year}" if args.year else ""
        print(f"error: no {args.form} filings for {args.ticker.upper()}{scope} in {source.name} source", file=err)
        return EXIT_NO_TABLE

    if args.list_filings:
        for r in refs:
            print(f"{r.ticker}  {r.filing_date.isoformat()}  {r.form}  {r.locator}", file=err)
        return EXIT_OK

    ref = pick_filing(refs, prefer=args.prefer)
    assert ref is not None

    data = cache.get(ref)
    if data is None:
        try:
            data = source.read(ref)
        except SourceError as exc:
            print(f"error: {exc}", file=err)
            return EXIT_NO_TABLE
        # A local source is already on disk; caching it again would only
        # duplicate bytes. The cache exists for network fetches.
        if source.name != "local":
            suffix = Path(ref.locator).suffix.lower()
            if suffix not in (".html", ".htm", ".txt"):
                suffix = ".txt"
            cache.put(ref, data, suffix=suffix)

    result = extract(data, profile=prof, filing_date=ref.filing_date)

    if not result.ok:
        print(f"error: no {prof.name} table extracted from {ref}", file=err)
        for f in result.flags:
            desc = _describe(f)
            print(f"     - {f}{': ' + desc if desc else ''}", file=err)
        return EXIT_NO_TABLE

    csv_text = result.table.to_csv()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(csv_text, encoding="utf-8")
        dest = str(args.output)
    else:
        sys.stdout.write(csv_text)
        dest = "stdout"

    if not args.quiet:
        _report(result, ref, dest, err)

    if args.strict and result.needs_review:
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
