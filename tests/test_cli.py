"""Tests for the source, cache and CLI layers.

No network is touched anywhere here, by design: the network source is a separate
optional module, so the CLI stays fully testable without one.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from sec_tables import cli
from sec_tables.cache import FilingCache, human_bytes
from sec_tables.sources import FilingRef, LocalSource, SourceError, form_to_fs, pick_filing
from sec_tables.types import PROVENANCE_FLAGS, REVIEW_FLAGS, Extraction

FIXTURE = Path(__file__).parent / "fixtures" / "dal_1997_sct.txt"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A miniature corpus in the layout LocalSource expects."""
    root = tmp_path / "data"
    d = root / "DAL" / "DEF_14A"
    d.mkdir(parents=True)
    (d / "1997-09-19_DEF_14A.txt").write_text(FIXTURE.read_text(errors="ignore"))
    (d / "1998-09-18_DEF_14A.txt").write_text(FIXTURE.read_text(errors="ignore"))
    (root / "EMPTY").mkdir()
    return root


class TestFlagClassification:
    def test_review_and_provenance_are_disjoint(self):
        """Conflating them would make every plain-text extraction look broken."""
        assert not (REVIEW_FLAGS & PROVENANCE_FLAGS)

    def test_ascii_source_is_provenance_not_a_warning(self):
        e = Extraction(table=None, candidate=None)
        e.flag("ascii_source")
        assert e.provenance_flags == ["ascii_source"]
        assert e.review_flags == []
        assert not e.needs_review

    def test_needs_review_tracks_review_flags(self):
        e = Extraction(table=None, candidate=None)
        e.flag("ambiguous_selection")
        assert e.needs_review
        assert not e.trustworthy  # also not ok, so definitely not trustworthy


class TestLocalSource:
    def test_lists_filings_for_a_ticker(self, corpus):
        refs = LocalSource(corpus).list_filings("DAL")
        assert len(refs) == 2
        assert {r.filing_date.year for r in refs} == {1997, 1998}

    def test_year_filter(self, corpus):
        refs = LocalSource(corpus).list_filings("DAL", year=1997)
        assert len(refs) == 1 and refs[0].filing_date == date(1997, 9, 19)

    def test_ticker_is_case_insensitive(self, corpus):
        assert LocalSource(corpus).list_filings("dal")

    def test_unknown_ticker_returns_empty_not_error(self, corpus):
        assert LocalSource(corpus).list_filings("NOPE") == []

    def test_missing_root_is_an_error(self, tmp_path):
        with pytest.raises(SourceError):
            LocalSource(tmp_path / "nowhere")

    def test_read_returns_bytes(self, corpus):
        src = LocalSource(corpus)
        ref = src.list_filings("DAL", year=1997)[0]
        # The fixture is the table block itself, below the section heading.
        assert b"NAME AND PRINCIPAL POSITION" in src.read(ref).upper()

    def test_html_preferred_over_txt_for_the_same_date(self, corpus):
        """A text rendition sometimes omits the table while the HTML has it."""
        d = corpus / "DAL" / "DEF_14A"
        (d / "1999-09-17_DEF_14A.txt").write_text("txt")
        (d / "1999-09-17_DEF_14A.html").write_text("<html></html>")
        refs = LocalSource(corpus).list_filings("DAL", year=1999)
        assert refs[0].locator.endswith(".html")

    def test_form_name_spaces_map_to_underscores(self):
        assert form_to_fs("DEF 14A") == "DEF_14A"


class TestPickFiling:
    def test_latest_is_the_default(self, corpus):
        refs = LocalSource(corpus).list_filings("DAL")
        assert pick_filing(refs).filing_date.year == 1998

    def test_earliest_when_asked(self, corpus):
        refs = LocalSource(corpus).list_filings("DAL")
        assert pick_filing(refs, prefer="earliest").filing_date.year == 1997

    def test_empty_is_none_not_an_error(self):
        assert pick_filing([]) is None


class TestCache:
    def _ref(self):
        return FilingRef("DAL", "DEF 14A", date(1997, 9, 19), "irrelevant")

    def test_roundtrip(self, tmp_path):
        c = FilingCache(tmp_path)
        assert c.get(self._ref()) is None
        c.put(self._ref(), b"hello", suffix=".txt")
        assert c.get(self._ref()) == b"hello"

    def test_disabled_cache_stores_nothing(self, tmp_path):
        c = FilingCache(tmp_path, enabled=False)
        assert c.put(self._ref(), b"hello") is None
        assert c.get(self._ref()) is None

    def test_partial_writes_are_not_served(self, tmp_path):
        """An interrupted fetch must not leave bytes a later run trusts."""
        c = FilingCache(tmp_path)
        p = c.path_for(self._ref(), ".txt")
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.with_suffix(p.suffix + ".part")).write_bytes(b"truncated")
        assert c.get(self._ref()) is None

    def test_empty_payload_is_not_cached(self, tmp_path):
        assert FilingCache(tmp_path).put(self._ref(), b"") is None

    def test_layout_is_browsable_and_reusable_as_a_corpus(self, tmp_path):
        c = FilingCache(tmp_path)
        c.put(self._ref(), b"x", suffix=".txt")
        # The cache dir should itself be a valid LocalSource root.
        assert LocalSource(tmp_path).list_filings("DAL", year=1997)

    def test_human_bytes(self):
        assert human_bytes(512) == "512 B"
        assert human_bytes(2048).endswith("KB")


class TestTwoFilingsOnOneDay:
    """A company can file twice in a day, and the cache must not conflate them.

    (ticker, form, filing_date) is not a filing identity. Delta filed a DEF 14A
    and a second one the same day here; before the filing token was part of the
    path, both resolved to `1997-09-19_DEF_14A.txt` and whichever was fetched
    first answered for the other. Nothing surfaced: the caller asked for filing
    B, received filing A, and both are real filings of the right form on the
    right date.

    The existing multi-filing tests never caught it because every one of them
    used filings on *different* dates.
    """

    ORIGINAL = FilingRef(
        "DAL", "DEF 14A", date(1997, 9, 19),
        "https://www.sec.gov/Archives/edgar/data/27904/0000950144-97-010197.txt",
        cik="27904", accession="0000950144-97-010197",
    )
    AMENDED = FilingRef(
        "DAL", "DEF 14A", date(1997, 9, 19),
        "https://www.sec.gov/Archives/edgar/data/27904/0000950144-97-010198.txt",
        cik="27904", accession="0000950144-97-010198",
    )

    def test_same_day_filings_get_different_paths(self, tmp_path):
        c = FilingCache(tmp_path)
        assert c.path_for(self.ORIGINAL, ".txt") != c.path_for(self.AMENDED, ".txt")

    def test_each_filing_caches_and_returns_its_own_bytes(self, tmp_path):
        c = FilingCache(tmp_path)
        c.put(self.ORIGINAL, b"the original proxy statement", suffix=".txt")
        c.put(self.AMENDED, b"the amended proxy statement", suffix=".txt")

        assert c.get(self.ORIGINAL) == b"the original proxy statement"
        assert c.get(self.AMENDED) == b"the amended proxy statement"

    def test_caching_one_does_not_satisfy_a_lookup_for_the_other(self, tmp_path):
        """The decisive one: a miss must stay a miss."""
        c = FilingCache(tmp_path)
        c.put(self.ORIGINAL, b"the original proxy statement", suffix=".txt")
        assert c.get(self.AMENDED) is None

    def test_the_same_filing_still_hits_the_cache(self, tmp_path):
        """Uniqueness must not have been bought by making every lookup miss."""
        c = FilingCache(tmp_path)
        c.put(self.ORIGINAL, b"bytes", suffix=".txt")
        assert c.get(self.ORIGINAL) == b"bytes"
        # A ref rebuilt from scratch — as the proxy does on a second request —
        # keys the same way.
        rebuilt = FilingRef(
            "DAL", "DEF 14A", date(1997, 9, 19), self.ORIGINAL.locator,
            cik="27904", accession="0000950144-97-010197",
        )
        assert c.get(rebuilt) == b"bytes"

    def test_the_accession_is_recovered_from_the_url_when_not_recorded(self, tmp_path):
        """A ref built before `accession` existed still keys correctly.

        Every archive URL carries the accession number, so the identity is
        recoverable and two same-day filings stay apart without the field.
        """
        c = FilingCache(tmp_path)
        bare_a = FilingRef("DAL", "DEF 14A", date(1997, 9, 19), self.ORIGINAL.locator)
        bare_b = FilingRef("DAL", "DEF 14A", date(1997, 9, 19), self.AMENDED.locator)
        c.put(bare_a, b"a", suffix=".txt")
        assert c.get(bare_b) is None
        assert c.get(bare_a) == b"a"
        assert "0000950144-97-010197" in c.path_for(bare_a, ".txt").name

    def test_a_locator_with_no_accession_still_separates_filings(self, tmp_path):
        """`LocalSource` paths have no accession; a digest keeps them distinct."""
        c = FilingCache(tmp_path)
        a = FilingRef("DAL", "DEF 14A", date(1997, 9, 19), "/corpus/a.txt")
        b = FilingRef("DAL", "DEF 14A", date(1997, 9, 19), "/corpus/b.txt")
        c.put(a, b"aaa", suffix=".txt")
        c.put(b, b"bbb", suffix=".txt")
        assert c.get(a) == b"aaa" and c.get(b) == b"bbb"

    def test_filenames_stay_safe_and_carry_nothing_about_the_requester(self, tmp_path):
        """A path segment is not a place to put a URL, and not a place for a person."""
        c = FilingCache(tmp_path)
        hostile = FilingRef(
            "DAL", "DEF 14A", date(1997, 9, 19),
            "https://www.sec.gov/Archives/../../etc/passwd?who=visitor@example.com",
        )
        name = c.path_for(hostile, ".txt").name
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
        assert "@" not in name and "/" not in name and ".." not in name
        # And the written path really is inside the cache root.
        written = c.put(hostile, b"x", suffix=".txt")
        assert written is not None and tmp_path in written.parents

    def test_both_filings_are_visible_to_localsource(self, tmp_path):
        """The cache stays a browsable corpus, with both same-day filings in it."""
        c = FilingCache(tmp_path)
        c.put(self.ORIGINAL, b"one", suffix=".txt")
        c.put(self.AMENDED, b"two", suffix=".txt")
        found = LocalSource(tmp_path).list_filings("DAL", year=1997)
        assert len(found) == 2, [str(f) for f in found]
        assert {LocalSource(tmp_path).read(f) for f in found} == {b"one", b"two"}


class TestLegacyCacheEntries:
    """Pre-token files are left alone and no longer trusted.

    `1997-09-19_DEF_14A.txt` could have come from any filing of that form on
    that date. The cache cannot tell which, so reading it would mean sometimes
    returning the wrong filing's bytes with nothing to indicate it. The file is
    not deleted — it is someone's corpus, and `LocalSource` still reads it — but
    the filing it holds is fetched once more under a name that identifies it.
    """

    REF = FilingRef(
        "DAL", "DEF 14A", date(1997, 9, 19),
        "https://www.sec.gov/Archives/edgar/data/27904/0000950144-97-010197.txt",
        accession="0000950144-97-010197",
    )

    def _write_legacy(self, root):
        legacy = root / "DAL" / "DEF_14A" / "1997-09-19_DEF_14A.txt"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"ambiguous legacy bytes")
        return legacy

    def test_a_legacy_entry_is_not_served(self, tmp_path):
        self._write_legacy(tmp_path)
        assert FilingCache(tmp_path).get(self.REF) is None

    def test_a_legacy_entry_is_not_destroyed(self, tmp_path):
        legacy = self._write_legacy(tmp_path)
        c = FilingCache(tmp_path)
        c.put(self.REF, b"freshly fetched", suffix=".txt")
        assert legacy.read_bytes() == b"ambiguous legacy bytes"
        assert c.get(self.REF) == b"freshly fetched"


class TestCLI:
    def _run(self, capsys, *args):
        code = cli.main(list(args))
        return code, capsys.readouterr()

    def test_list_tables(self, capsys):
        code, out = self._run(capsys, "--list-tables")
        assert code == cli.EXIT_OK
        for name in ("summary_compensation", "director_compensation", "beneficial_ownership"):
            assert name in out.err

    def test_extract_to_file(self, capsys, corpus, tmp_path):
        dest = tmp_path / "out.csv"
        code, out = self._run(
            capsys, "DAL", "--year", "1997", "--table", "sct",
            "--source", "local", "--root", str(corpus), "-o", str(dest),
        )
        assert code == cli.EXIT_OK
        text = dest.read_text()
        assert "Ronald W. Allen" in text
        assert "562500" in text

    def test_csv_goes_to_stdout_by_default(self, capsys, corpus):
        code, out = self._run(
            capsys, "DAL", "--year", "1997", "--table", "sct",
            "--source", "local", "--root", str(corpus),
        )
        assert code == cli.EXIT_OK
        assert "Ronald W. Allen" in out.out          # data on stdout
        assert "[ok]" in out.err                     # summary on stderr

    def test_review_flags_are_always_reported(self, capsys, corpus, tmp_path):
        """A quiet warning is the same as no warning.

        Uses a post-2006 table with no Total column — which Item 402(c)
        mandates — so `missing_required_columns` is raised. Deliberately not
        tied to a real filing's incidental flags: de-duplicating candidates
        removed the flag the DAL fixture used to carry, and a test of the
        REPORTING mechanism should not break when extraction improves.
        """
        d = corpus / "XX" / "DEF_14A"
        d.mkdir(parents=True)
        (d / "2020-04-01_DEF_14A.html").write_text(
            "<table>"
            "<tr><th>Name and Principal Position</th><th>Year</th>"
            "<th>Salary</th><th>Bonus</th></tr>"
            "<tr><td>Jane Q. Smith</td><td>2019</td>"
            "<td>$100,000</td><td>$5,000</td></tr>"
            "</table>"
        )
        _, out = self._run(
            capsys, "XX", "--year", "2020", "--table", "sct", "-o", str(tmp_path / "o.csv"),
            "--source", "local", "--root", str(corpus),
        )
        assert "review recommended" in out.err
        assert "missing_required_columns" in out.err
        # The explanation must be present, not just the flag name.
        assert "mandates" in out.err

    def test_provenance_is_not_presented_as_a_problem(self, capsys, corpus):
        _, out = self._run(
            capsys, "DAL", "--year", "1997", "--table", "sct",
            "--source", "local", "--root", str(corpus),
        )
        line = [l for l in out.err.splitlines() if "sgml_source" in l or "ascii_source" in l]
        assert line and "provenance" in line[0]

    def test_strict_exits_nonzero_when_review_is_needed(self, capsys, corpus, tmp_path):
        d = corpus / "YY" / "DEF_14A"
        d.mkdir(parents=True)
        (d / "2020-04-01_DEF_14A.html").write_text(
            "<table>"
            "<tr><th>Name and Principal Position</th><th>Year</th>"
            "<th>Salary</th><th>Bonus</th></tr>"
            "<tr><td>Jane Q. Smith</td><td>2019</td>"
            "<td>$100,000</td><td>$5,000</td></tr>"
            "</table>"
        )
        code, _ = self._run(
            capsys, "YY", "--year", "2020", "--table", "sct", "--strict", "-q",
            "-o", str(tmp_path / "o.csv"),
            "--source", "local", "--root", str(corpus),
        )
        assert code == cli.EXIT_NEEDS_REVIEW

    def test_default_does_not_fail_on_review_flags(self, capsys, corpus):
        code, _ = self._run(
            capsys, "DAL", "--year", "1997", "--table", "sct", "-q",
            "--source", "local", "--root", str(corpus),
        )
        assert code == cli.EXIT_OK

    def test_unknown_table_is_a_usage_error(self, capsys, corpus):
        code, out = self._run(
            capsys, "DAL", "--table", "nope",
            "--source", "local", "--root", str(corpus),
        )
        assert code == cli.EXIT_USAGE
        assert "unknown profile" in out.err

    def test_missing_ticker_is_a_usage_error(self, capsys):
        code, out = self._run(capsys, "--table", "sct")
        assert code == cli.EXIT_USAGE

    def test_local_source_requires_root(self, capsys):
        code, out = self._run(capsys, "DAL", "--source", "local")
        assert code == cli.EXIT_USAGE
        assert "--root" in out.err

    def test_no_filings_found(self, capsys, corpus):
        code, out = self._run(
            capsys, "ZZZZ", "--source", "local", "--root", str(corpus),
        )
        assert code == cli.EXIT_NO_TABLE

    def test_list_filings(self, capsys, corpus):
        code, out = self._run(
            capsys, "DAL", "--list-filings", "--source", "local", "--root", str(corpus),
        )
        assert code == cli.EXIT_OK
        assert "1997-09-19" in out.err and "1998-09-18" in out.err

    def test_prefer_earliest(self, capsys, corpus, tmp_path):
        _, out = self._run(
            capsys, "DAL", "--prefer", "earliest", "-o", str(tmp_path / "o.csv"),
            "--source", "local", "--root", str(corpus),
        )
        assert "1997-09-19" in out.err
