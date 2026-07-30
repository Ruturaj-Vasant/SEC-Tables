"""End-to-end tests against a real filing, with hand-verified expected values.

The fixture is the Summary Compensation Table from Delta Air Lines' 1997 DEF 14A,
a plain-text SGML submission. Expected numbers were read off the filing by eye,
so this asserts correctness rather than merely stability — a snapshot test would
happily lock in a wrong answer.

It is deliberately a pre-2001 filing: that is the era with no DOM, no XBRL, and
no commercial structured feed, and therefore the era where a regression would be
invisible everywhere else.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import sec_tables as st
from sec_tables.types import Backend

FIXTURES = Path(__file__).parent / "fixtures"
DAL_1997 = FIXTURES / "dal_1997_sct.txt"

# (name, year, salary, bonus, other_annual, restricted_stock, options_sars, ltip, all_other)
EXPECTED = [
    ("Ronald W. Allen", "1997", "562500", "0", "14183", "0", "54000", "0", "20568"),
    ("Ronald W. Allen", "1996", "475000", "532594", "12517", "0", "66000", "0", "15504"),
    ("Ronald W. Allen", "1995", "475000", "560625", "11667", "390000", "89000", "0", "15876"),
    ("Maurice W. Worth", "1997", "333333", "205743", "8639", "0", "21000", "0", "13700"),
    ("Maurice W. Worth", "1996", "282500", "237500", "7123", "0", "26000", "0", "11823"),
    ("Maurice W. Worth", "1995", "251250", "187500", "6375", "124800", "19500", "0", "12064"),
]


@pytest.fixture(scope="module")
def extraction():
    assert DAL_1997.exists(), f"missing fixture {DAL_1997}"
    return st.extract_sct(DAL_1997.read_text(errors="ignore"), date(1997, 9, 19))


class TestDelta1997:
    def test_extraction_succeeds(self, extraction):
        assert extraction.ok
        assert extraction.table is not None

    def test_uses_a_text_backend_not_dom(self, extraction):
        """A tree parser cannot read this table; if the DOM path claims it, the
        result is whatever lxml salvaged from unclosed tags."""
        assert extraction.backend in (Backend.ASCII, Backend.SGML)

    def test_era_is_pre_2006(self, extraction):
        assert extraction.era == st.ERA_PRE_2006

    def test_uses_old_column_set(self, extraction):
        roles = set(extraction.table.roles)
        assert {"other_annual_comp", "restricted_stock_awards", "options_sars", "ltip_payouts"} <= roles
        # Post-2006 columns must not be invented for a 1997 filing.
        assert not ({"non_equity_incentive", "pension_and_nqdc"} & roles)

    def test_row_values_match_the_filing(self, extraction):
        roles = extraction.table.roles
        idx = {r: i for i, r in enumerate(roles)}
        wanted = [
            "name", "year", "salary", "bonus", "other_annual_comp",
            "restricted_stock_awards", "options_sars", "ltip_payouts", "all_other_comp",
        ]
        missing = [w for w in wanted if w not in idx]
        assert not missing, f"missing roles: {missing} (got {roles})"

        got = [tuple(row[idx[w]] for w in wanted) for row in extraction.table.rows]
        for expected in EXPECTED:
            assert expected in got, f"{expected[0]} {expected[1]} not found"

    def test_every_executive_year_is_captured(self, extraction):
        """Five named officers, three years each."""
        rows = extraction.table.rows
        names = {r[0] for r in rows if r[0]}
        assert len(names) == 5, sorted(names)
        assert len(rows) == 15, f"expected 15 person-years, got {len(rows)}"

    def test_title_not_mistaken_for_a_person(self, extraction):
        names = {r[0] for r in extraction.table.rows}
        for bogus in ("President", "President and", "Chief Executive Officer", "Operations"):
            assert bogus not in names

    def test_ascii_provenance_is_flagged(self, extraction):
        """A consumer must be able to tell text-derived rows from DOM-derived."""
        assert {"ascii_source", "sgml_source"} & set(extraction.flags)

    def test_zero_and_missing_are_distinguishable(self, extraction):
        """LTIP is a real 0 here, not an absent value."""
        idx = {r: i for i, r in enumerate(extraction.table.roles)}
        ltip = [r[idx["ltip_payouts"]] for r in extraction.table.rows]
        assert all(v == "0" for v in ltip), ltip


class TestNoFilingDate:
    def test_missing_date_is_flagged_and_still_works(self):
        r = st.extract_sct(DAL_1997.read_text(errors="ignore"))
        assert "no_filing_date" in r.flags
        assert r.ok
        # Era should be recovered from the columns themselves.
        assert r.era == st.ERA_PRE_2006


class TestDegenerateInput:
    @pytest.mark.parametrize("junk", [b"", b"not a filing", b"<html><body><p>hi</p></body></html>"])
    def test_no_table_returns_flags_not_exception(self, junk):
        r = st.extract_sct(junk, date(2020, 1, 1))
        assert not r.ok
        assert "no_table_found" in r.flags

    def test_table_without_compensation_is_rejected(self):
        html = "<table><tr><th>City</th><th>Population</th></tr><tr><td>Paris</td><td>2000000</td></tr></table>"
        r = st.extract_sct(html, date(2020, 1, 1))
        assert not r.ok


DAL_1994 = FIXTURES / "dal_1994_sct.txt"

# Hand-verified against the filing. Delta's 1994 proxy underlines its first
# column three characters in while the names begin at column 0 — a ruler that is
# not in the same horizontal frame as the data it describes.
EXPECTED_1994 = [
    ("Ronald W. Allen", "1994", "475000", "0", "8528", "0", "89000", "0", "18512"),
    ("Ronald W. Allen", "1993", "487500", "0", "7077", "0", "0", "0", "17639"),
    ("Harold C. Alger", "1994", "261250", "0", "4713", "0", "35400", "0", "13416"),
]


class TestDelta1994IndentedRuler:
    """Regression for a silently-wrong extraction found by a live EDGAR fetch.

    Reading the indented ruler at face value truncated the first characters of
    every name — "Ronald W. Allen" became "ald W. Allen" — and merged all five
    executives' titles into one cell. Nothing flagged it: the values still looked
    like values. Correcting the whole ruler by the offset then split every
    numeric column instead ("1994" -> "19"/"94"), so only the first boundary
    moves.
    """

    @pytest.fixture(scope="class")
    def result(self):
        assert DAL_1994.exists()
        return st.extract_sct(DAL_1994.read_text(errors="ignore"), date(1994, 9, 13))

    def test_extracted(self, result):
        assert result.ok and result.era == st.ERA_PRE_2006

    def test_names_are_not_truncated(self, result):
        names = {r[0] for r in result.table.rows}
        assert "Ronald W. Allen" in names
        assert not any(n.startswith("ald ") or n.startswith("arold") for n in names)

    def test_every_executive_is_separate(self, result):
        """The truncation merged all five into one person's title field."""
        names = {r[0] for r in result.table.rows if r[0]}
        assert len(names) >= 5, sorted(names)

    def test_years_are_not_split(self, result):
        idx = result.table.roles.index("year")
        years = {r[idx] for r in result.table.rows}
        assert years <= {"1994", "1993", "1992"}, years

    def test_values_match_the_filing(self, result):
        roles = result.table.roles
        idx = {r: i for i, r in enumerate(roles)}
        wanted = ["name", "year", "salary", "bonus", "other_annual_comp",
                  "restricted_stock_awards", "options_sars", "ltip_payouts", "all_other_comp"]
        assert all(w in idx for w in wanted), roles
        got = [tuple(row[idx[w]] for w in wanted) for row in result.table.rows]
        for expected in EXPECTED_1994:
            assert expected in got, f"{expected[0]} {expected[1]} missing"


# ===========================================================================
# Item 402(r) — Director Compensation. Compass Minerals, filed 2024-01-29.
# Values read off the filing; every row's components also sum to its Total.
# ===========================================================================

CMP_2024 = FIXTURES / "cmp_2024_director_comp.html"

EXPECTED_DIRECTORS = [
    ("Richard P. Dealy", "25625", "198859", "224484"),
    ("Edward C. Dowling, Jr.", "13125", "219493", "232618"),
    ("Eric Ford", "25625", "189720", "215345"),
    ("Jill V. Gardiner", "", "136619", "136619"),
    ("Gareth T. Joyce", "23750", "196311", "220061"),
    ("Melissa M. Miller", "23750", "197902", "221652"),
    ("Joseph E. Reece", "", "400085", "400085"),
    ("Lori A. Walker", "28125", "210082", "238207"),
    ("Paul S. Williams", "37472", "", "37472"),
    ("Amy J. Yoder", "39042", "", "39042"),
]


class TestDirectorCompensation:
    """First ground truth for Item 402(r).

    Also a regression for a silent name bug: this filing wraps a director's name
    across a line break ("Richard<br>P. Dealy"). Read as name-then-title that
    yields a director called "Richard" and sweeps every subsequent name into the
    title field — with no flag raised, because the numbers still look like numbers.
    """

    @pytest.fixture(scope="class")
    def result(self):
        assert CMP_2024.exists()
        return st.extract(CMP_2024.read_text(), profile="director", filing_date=date(2024, 1, 29))

    def test_extracted_cleanly(self, result):
        assert result.ok
        assert not result.review_flags, result.flags

    def test_all_ten_directors(self, result):
        assert len(result.table.rows) == 10

    def test_wrapped_names_are_rejoined(self, result):
        names = [r[0] for r in result.table.rows]
        assert "Richard P. Dealy" in names
        assert "Richard" not in names, "name split at its line break"

    def test_values_match_the_filing(self, result):
        idx = {r: i for i, r in enumerate(result.table.roles)}
        got = [
            (r[idx["name"]], r[idx["fees_earned"]], r[idx["stock_awards"]], r[idx["total"]])
            for r in result.table.rows
        ]
        for expected in EXPECTED_DIRECTORS:
            assert expected in got, expected

    def test_every_row_sums_to_its_total(self, result):
        """Independent arithmetic check on the extraction."""
        idx = {r: i for i, r in enumerate(result.table.roles)}
        for row in result.table.rows:
            parts = sum(float(row[idx[c]] or 0) for c in ("fees_earned", "stock_awards"))
            assert abs(parts - float(row[idx["total"]])) < 1.0, row

    def test_uses_the_director_schema_not_the_sct(self, result):
        roles = set(result.table.roles)
        assert "fees_earned" in roles
        assert "salary" not in roles  # that would mean the SCT was selected


# ===========================================================================
# Item 403 — Beneficial Ownership. AZZ Inc., filed 2019-05-28.
# ===========================================================================

AZZ_2019 = FIXTURES / "azz_2019_ownership.html"

EXPECTED_HOLDERS = [
    ("BlackRock, Inc.", "3765728", "14.5"),
    ("The Vanguard Group, Inc.", "2619321", "10.04"),
    ("T. Rowe Price Associates, Inc.", "1853610", "7.1"),
    ("Van Berkom & Associates Inc.", "1405056", "5.39"),
]


class TestBeneficialOwnership:
    """First ground truth for Item 403.

    Also a regression for the name/address seam. This filing wraps the cell
    mid-address ("BlackRock, Inc. 55 East 52nd<br>Street New York, NY"), so
    splitting on the line break produced a holder named
    "BlackRock, Inc. 55 East 52 nd". Where an address begins is a content
    question, not a typography one.
    """

    @pytest.fixture(scope="class")
    def result(self):
        assert AZZ_2019.exists()
        return st.extract(AZZ_2019.read_text(), profile="ownership", filing_date=date(2019, 5, 28))

    def test_extracted_cleanly(self, result):
        assert result.ok
        assert not result.review_flags, result.flags

    def test_holder_shape_not_person_year(self, result):
        roles = set(result.table.roles)
        assert {"holder_name", "shares", "percent"} <= roles
        assert "year" not in roles  # ownership has no year dimension

    def test_values_match_the_filing(self, result):
        idx = {r: i for i, r in enumerate(result.table.roles)}
        got = [
            (r[idx["holder_name"]], r[idx["shares"]], r[idx["percent"]])
            for r in result.table.rows
        ]
        for expected in EXPECTED_HOLDERS:
            assert expected in got, expected

    def test_address_is_not_left_in_the_name(self, result):
        idx = result.table.roles.index("holder_name")
        for row in result.table.rows:
            name = row[idx]
            assert "Street" not in name and " 52 nd" not in name, name

    def test_address_is_captured_separately(self, result):
        idx = {r: i for i, r in enumerate(result.table.roles)}
        blackrock = [r for r in result.table.rows if r[idx["holder_name"]] == "BlackRock, Inc."][0]
        assert "New York" in blackrock[idx["holder_address"]]

    def test_group_rows_are_marked(self, result):
        """Group subtotals are aggregates and must be distinguishable."""
        assert "is_group" in result.table.roles


# ===========================================================================
# Item 403 in plain text — CVS/Melville, filed 1996-10-08.
# The pre-2001 ASCII path, which is the coverage no structured feed reaches.
# ===========================================================================

CVS_1996 = FIXTURES / "cvs_1996_ownership.txt"

EXPECTED_CVS = [
    ("FMR Corp.(1)", "Common Stock", "13552054", "12.8"),
    ("Brinson Partners, Inc.(2)", "Common Stock", "6904354", "6.5"),
]


class TestAsciiOwnership:
    """Regression for three bugs that made this filing badly wrong and unflagged.

    1. Continuation lines were attached FORWARDS. A holder's address sits on the
       lines *below* its value row, so buffering them and giving them to the next
       holder made Brinson Partners "82 Devonshire Street Boston, MA 02109
       Brinson Partners, Inc.".
    2. Old EDGAR nests <FN> footnotes INSIDE <TABLE>, so a correctly delimited
       SGML match still carried prose that tabulated into extra holders.
    3. "Title of Class" matched the percent rule via the bare phrase "of class",
       labelling the share-class column as a percentage.
    """

    @pytest.fixture(scope="class")
    def result(self):
        assert CVS_1996.exists()
        return st.extract(CVS_1996.read_text(errors="ignore"),
                          profile="ownership", filing_date=date(1996, 10, 8))

    def test_extracted_cleanly(self, result):
        assert result.ok
        assert not result.review_flags, result.flags

    def test_uses_a_text_backend(self, result):
        assert result.backend in (Backend.ASCII, Backend.SGML)

    def test_exactly_three_holders(self, result):
        """Footnote prose previously added six spurious holders."""
        assert len(result.table.rows) == 3

    def test_values_match_the_filing(self, result):
        idx = {r: i for i, r in enumerate(result.table.roles)}
        got = [
            (r[idx["holder_name"]], r[idx["share_class"]], r[idx["shares"]], r[idx["percent"]])
            for r in result.table.rows
        ]
        for expected in EXPECTED_CVS:
            assert expected in got, expected

    def test_address_belongs_to_the_right_holder(self, result):
        idx = {r: i for i, r in enumerate(result.table.roles)}
        by_name = {r[idx["holder_name"]]: r[idx["holder_address"]] for r in result.table.rows}
        assert "82 Devonshire" in by_name["FMR Corp.(1)"]
        assert "LaSalle" in by_name["Brinson Partners, Inc.(2)"]
        # The decisive check: FMR's address must NOT have leaked onto Brinson.
        assert "Devonshire" not in by_name["Brinson Partners, Inc.(2)"]

    def test_share_class_is_not_a_percentage(self, result):
        idx = result.table.roles.index("share_class")
        assert {r[idx] for r in result.table.rows} == {"Common Stock", "Series One ESOP"}

    def test_multiline_holder_name_is_joined(self, result):
        names = [r[0] for r in result.table.rows]
        melville = [n for n in names if n.startswith("Melville")]
        assert melville and "Employee Stock Ownership" in melville[0]
