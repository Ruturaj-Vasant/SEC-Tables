"""Unit tests for the pieces that encode filing-specific knowledge.

Each test here corresponds to a way real filings break naive parsing. They are
written as assertions about behaviour rather than snapshots, so a refactor that
preserves meaning stays green.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from sec_tables import normalize, postprocess, schema, tabulate
from sec_tables.types import Backend, Table


# ------------------------------------------------------------------ schema

class TestSchemaEras:
    def test_pre_2006_has_ltip_and_post_does_not(self):
        pre = schema.roles_for(schema.ERA_PRE_2006)
        post = schema.roles_for(schema.ERA_POST_2006)
        assert "ltip_payouts" in pre and "ltip_payouts" not in post
        assert "options_sars" in pre and "options_sars" not in post
        assert "other_annual_comp" in pre and "other_annual_comp" not in post

    def test_post_2006_has_current_columns_and_pre_does_not(self):
        pre = schema.roles_for(schema.ERA_PRE_2006)
        post = schema.roles_for(schema.ERA_POST_2006)
        for role in ("non_equity_incentive", "pension_and_nqdc", "stock_awards", "option_awards"):
            assert role in post and role not in pre

    @pytest.mark.parametrize(
        "day,expected",
        [
            (date(1997, 9, 19), schema.ERA_PRE_2006),
            (date(2004, 4, 1), schema.ERA_PRE_2006),
            (date(2006, 12, 14), schema.ERA_PRE_2006),
            (date(2007, 3, 1), schema.ERA_TRANSITION),
            (date(2008, 3, 1), schema.ERA_POST_2006),
            (date(2023, 3, 16), schema.ERA_POST_2006),
        ],
    )
    def test_era_boundaries(self, day, expected):
        assert schema.era_for(day) == expected

    def test_unknown_date_is_permissive_not_wrong(self):
        """No date must widen the schema, never silently pick one era."""
        union = set(schema.roles_for(schema.era_for(None)))
        assert {"ltip_payouts", "non_equity_incentive"} <= union

    def test_old_layout_does_not_require_total(self):
        """Item 402(b) mandated no Total column; requiring it would fail every
        pre-2006 filing."""
        required = {c.role for c in schema.PRE_2006_COLUMNS if c.required}
        assert "total" not in required
        assert "total" in {c.role for c in schema.POST_2006_COLUMNS if c.required}


# --------------------------------------------------------------- normalize

class TestHeaderCleaning:
    def test_rejoins_word_split_across_lines(self):
        """Filings wrap headers mid-word: 'COMPEN-\\nSATION'."""
        assert normalize.clean_header("Compen-\n sation") == "compensation"
        assert normalize.clean_header("ALL OTHER\nCOMPEN- SATION") == "all other compensation"

    def test_strips_footnote_markers(self):
        assert normalize.clean_header("Salary(1)") == "salary"
        assert normalize.clean_header("Total ($)(6)") == "total"

    def test_handles_nbsp(self):
        assert normalize.clean_header("All\xa0Other") == "all other"


class TestRoleInference:
    def test_wrapped_header_still_maps(self):
        assert normalize.infer_role("ALL OTHER\nCOMPEN- SATION") == "all_other_comp"

    def test_other_annual_beats_all_other(self):
        assert normalize.infer_role("OTHER ANNUAL COMPENSATION") == "other_annual_comp"
        assert normalize.infer_role("ALL OTHER COMPENSATION") == "all_other_comp"

    def test_era_separates_stock_award_meanings(self):
        pre = normalize.infer_role("RESTRICTED STOCK AWARD(S)", schema.ERA_PRE_2006)
        post = normalize.infer_role("Stock Awards", schema.ERA_POST_2006)
        assert pre == "restricted_stock_awards"
        assert post == "stock_awards"

    def test_era_invalid_role_is_kept_not_discarded(self):
        """An LTIP column under a post-2006 date means the *date* is suspect.

        Dropping the role would hide the contradiction; keeping it lets
        `detect_era_from_roles` catch the era and the caller raise a flag.
        """
        assert normalize.infer_role("LTIP PAYOUTS", schema.ERA_POST_2006) == "ltip_payouts"
        assert normalize.infer_role("LTIP PAYOUTS", schema.ERA_PRE_2006) == "ltip_payouts"

    def test_era_prefers_a_valid_match_when_one_exists(self):
        """'Restricted Stock Awards' is pre-2006 only, so a post-2006 read should
        land on the era-valid `stock_awards` rather than the exclusive role."""
        assert normalize.infer_role("Restricted Stock Awards", schema.ERA_POST_2006) == "stock_awards"

    def test_unknown_header_survives_as_fallback(self):
        role = normalize.infer_role("Special Retention Award")
        assert role and role != "unknown"

    def test_repeated_roles_get_suffixes(self):
        roles = normalize.infer_roles(["Salary", "Salary"])
        assert roles == ["salary", "salary_2"]

    def test_detect_era_from_columns(self):
        assert normalize.detect_era_from_roles(["salary", "ltip_payouts"]) == schema.ERA_PRE_2006
        assert normalize.detect_era_from_roles(["salary", "non_equity_incentive"]) == schema.ERA_POST_2006
        assert normalize.detect_era_from_roles(["salary", "bonus"]) is None


class TestNumberNormalization:
    @pytest.mark.parametrize("raw", ["-", "--", "—", "*", "", "n/a", "N/A", "none"])
    def test_missing_sentinels_are_empty_not_zero(self, raw):
        """A missing payout coerced to 0 biases every downstream mean."""
        assert normalize.normalize_number(raw) == ""

    def test_currency_and_grouping_stripped(self):
        assert normalize.normalize_number("$562,500") == "562500"
        assert normalize.normalize_number(" 1,148,333 ") == "1148333"

    def test_parenthesised_is_negative(self):
        assert normalize.normalize_number("(1,234)") == "-1234"

    def test_real_zero_is_preserved(self):
        assert normalize.normalize_number("0") == "0"


# ---------------------------------------------------------------- tabulate

class TestAsciiGeometry:
    def test_edgar_dash_escape_does_not_shift_columns(self):
        """EDGAR prefixes '- ' to a dash-leading line. Read at face value the
        ruler sits two characters right of the data and every value splits."""
        block = "\n".join([
            "NAME                       YEAR   SALARY",
            "- ------------------------  ----   ------",
            "Jane Q. Smith               1997   562500",
        ])
        grid = tabulate.to_grid(block, Backend.ASCII)
        header, rows = tabulate.split_header(grid)
        assert rows, "no data rows recovered"
        assert rows[0][0].startswith("Jane")
        assert "1997" in rows[0]
        assert "562500" in rows[0]

    def test_rule_lines_are_not_data(self):
        assert tabulate.is_rule_line("- ------   ----")
        assert tabulate.is_rule_line("=======  ====")
        assert not tabulate.is_rule_line("Jane Smith  1997")

    def test_dot_leaders_stripped_from_names(self):
        block = "\n".join([
            "NAME                  YEAR   SALARY",
            "- -------------------  ----   ------",
            "Ronald W. Allen......  1997   562500",
        ])
        rows = tabulate.split_header(tabulate.to_grid(block, Backend.ASCII))[1]
        assert rows[0][0] == "Ronald W. Allen"

    def test_header_band_ends_at_first_data_row(self):
        """Plain-text headers stack many lines deep; a fixed row limit truncates
        them and leaks header text into the data."""
        grid = [
            ["", "", "ANNUAL COMPENSATION"],
            ["", "", "BONUS"],
            ["NAME AND PRINCIPAL POSITION", "YEAR", "SALARY"],
            ["", "", "($)"],
            ["Jane Q. Smith", "1997", "562500"],
        ]
        header, rows = tabulate.split_header(grid)
        assert len(rows) == 1
        assert rows[0][1] == "1997"
        assert "SALARY" in " ".join(header)


class TestDomGeometry:
    def test_colspan_is_expanded(self):
        html = (
            "<table>"
            "<tr><th colspan='2'>Annual</th><th>Total</th></tr>"
            "<tr><th>Salary</th><th>Bonus</th><th>($)</th></tr>"
            "<tr><td>100</td><td>200</td><td>300</td></tr>"
            "</table>"
        )
        grid = tabulate.to_grid(html, Backend.DOM)
        assert all(len(r) == 3 for r in grid), grid

    def test_rowspan_carries_down(self):
        html = (
            "<table>"
            "<tr><th>Name</th><th>Year</th></tr>"
            "<tr><td rowspan='2'>Jane</td><td>1997</td></tr>"
            "<tr><td>1996</td></tr>"
            "</table>"
        )
        grid = tabulate.to_grid(html, Backend.DOM)
        assert grid[2][0] == "Jane"


# ------------------------------------------------------------- postprocess

class TestPersonDetection:
    @pytest.mark.parametrize("text", ["Ronald W. Allen", "Jane Q. Smith", "Maurice W. Worth", "Roy C. Harvey"])
    def test_person_names(self, text):
        assert postprocess.looks_like_person(text)

    @pytest.mark.parametrize("text", [
        "Alexander M. Cutler",   # 'and' inside Alexander
        "Anderson W. Reed",      # 'and' inside Anderson
        "Sandra J. Wilson",      # 'and' inside Sandra
        "Chandler B. Ross",      # 'and' inside Chandler
        "Alessandro Rossi",      # 'and' inside Alessandro
        "Vincent P. Sandusky",   # 'and' inside Sandusky
    ])
    def test_title_words_must_not_match_inside_names(self, text):
        """Substring matching reclassified these executives as job titles and
        attributed their compensation rows to the previous person."""
        assert not postprocess.looks_like_title(text)
        assert postprocess.looks_like_person(text)

    def test_name_with_generational_suffix(self):
        assert postprocess.looks_like_person("Thomas J. Roeck, Jr")
        assert postprocess.looks_like_person("John D. Smith, III")

    @pytest.mark.parametrize("text", [
        "President and", "Chief Executive Officer", "Executive Vice President and",
        "Senior Vice President -- Finance", "Chairman of the Board,",
    ])
    def test_titles_are_not_people(self, text):
        assert not postprocess.looks_like_person(text)
        assert postprocess.looks_like_title(text)


class TestAssembly:
    def test_marker_column_folded(self):
        t = Table(
            header=["$", "Salary", "$", "Bonus"],
            rows=[["", "100", "", "50"], ["", "200", "", "60"]],
            roles=["salary", "salary_2", "bonus", "bonus_2"],
        )
        out = postprocess.merge_marker_columns(t)
        assert out.roles == ["salary", "bonus"]
        assert out.rows[0] == ["100", "50"]

    def test_stacked_name_block_becomes_one_row_per_year(self):
        """The layout that makes 'President and' look like a person's name."""
        t = Table(
            header=["Name and Position", "Year", "Salary"],
            roles=["name_and_position", "year", "salary"],
            rows=[
                ["Ronald W. Allen", "1997", "562500"],
                ["Chairman of the Board,", "", ""],
                ["President", "1996", "475000"],
                ["and Chief Executive Officer", "", ""],
                ["Maurice W. Worth", "1997", "333333"],
            ],
        )
        out = postprocess.assemble_people(t)
        assert out.roles[:2] == ["name", "position"]
        names = [r[0] for r in out.rows]
        assert names == ["Ronald W. Allen", "Ronald W. Allen", "Maurice W. Worth"]
        assert [r[1] for r in out.rows][0] == "Chairman of the Board, President and Chief Executive Officer"
        assert [r[2] for r in out.rows] == ["1997", "1996", "1997"]

    def test_blank_rows_dropped(self):
        t = Table(header=["a"], rows=[["x"], [""], ["y"]], roles=["a"])
        assert postprocess.drop_blank_rows(t).rows == [["x"], ["y"]]


# --------------------------------------------------- profile generalisation

from datetime import date as _date  # noqa: E402

import sec_tables as st  # noqa: E402
from sec_tables import profiles as _profiles  # noqa: E402
from sec_tables.schema import Schema, SchemaVersion, Column  # noqa: E402
from sec_tables.types import Assembly  # noqa: E402


class TestSchemaGenerality:
    def test_single_version_schema_ignores_date(self):
        s = Schema("x", (SchemaVersion("only", (Column("a", "A"),)),))
        assert s.single_version
        assert s.era_for(None) == "only"
        assert s.era_for(_date(1994, 1, 1)) == "only"
        assert s.era_from_roles(["a"]) is None  # nothing to disambiguate

    def test_mandated_from_marks_a_table_as_not_yet_existing(self):
        """Item 402(r) was created in 2006; a 1997 proxy cannot contain it, and
        counting that as an extraction failure blames the wrong layer."""
        s = _profiles.DIRECTOR_COMP.schema
        assert s.mandated_from is not None
        assert not s.applies_to(_date(1997, 3, 1))
        assert s.applies_to(_date(2010, 3, 1))

    def test_sct_has_no_mandate_cutoff(self):
        assert _profiles.SCT.schema.applies_to(_date(1994, 1, 1))

    def test_required_roles_are_per_era(self):
        sct = _profiles.SCT.schema
        assert "total" in sct.required_roles(schema.ERA_POST_2006)
        assert "total" not in sct.required_roles(schema.ERA_PRE_2006)


class TestProfileDrivenRoles:
    def test_same_header_maps_differently_per_table(self):
        """'Total' is a compensation sum in one table and a share count in
        another; a single global rule list cannot express that."""
        assert normalize.infer_role("Fees Earned or Paid in Cash",
                                    rules=_profiles.DIRECTOR_COMP.role_rules) == "fees_earned"
        # The SCT rule set has no concept of director fees.
        assert normalize.infer_role("Fees Earned or Paid in Cash",
                                    rules=_profiles.SCT.role_rules) != "fees_earned"

    def test_ownership_name_and_address_is_a_name_column(self):
        """Item 403 mandates a combined column; the address rule must not claim it."""
        role = normalize.infer_role("Name and Address of Beneficial Owner",
                                    rules=_profiles.BENEFICIAL_OWNERSHIP.role_rules)
        assert role == "holder_name"

    def test_ownership_percent_beats_shares(self):
        role = normalize.infer_role("Percent of Shares Outstanding",
                                    rules=_profiles.BENEFICIAL_OWNERSHIP.role_rules)
        assert role == "percent"


class TestProfileMetadata:
    def test_every_profile_declares_its_shape(self):
        for name, prof in _profiles.REGISTRY.items():
            assert isinstance(prof.assembly, Assembly), name
            assert prof.role_rules, name
            assert prof.schema is not None, name

    def test_ownership_identity_is_not_a_person(self):
        """'General Motors Co.' trips a job-title test on 'general', and group
        subtotals trip it on 'directors'. Both are valid Item 403 holders."""
        assert _profiles.SCT.identity_is_person
        assert _profiles.DIRECTOR_COMP.identity_is_person
        assert not _profiles.BENEFICIAL_OWNERSHIP.identity_is_person

    def test_aliases_resolve(self):
        assert _profiles.get("sct") is _profiles.SCT
        assert _profiles.get("ownership") is _profiles.BENEFICIAL_OWNERSHIP
        assert _profiles.get("director") is _profiles.DIRECTOR_COMP

    def test_unknown_profile_names_are_rejected(self):
        with pytest.raises(KeyError):
            _profiles.get("no_such_table")


class TestHolderAssembly:
    def test_section_labels_do_not_become_holders(self):
        t = Table(
            header=["Name", "Shares", "Percent"],
            roles=["holder_name", "shares", "percent"],
            rows=[
                ["Directors and Executive Officers:", "", ""],
                ["Jane Q. Smith", "1000", "1.2"],
                ["All directors and executive officers as a group", "5000", "6.0"],
            ],
        )
        out = postprocess.assemble_holders(t)
        names = [r[0] for r in out.rows]
        assert "Directors and Executive Officers:" not in names
        assert "Jane Q. Smith" in names
        assert out.rows[0][2] == "Directors and Executive Officers"  # section carried

    def test_group_rows_are_marked(self):
        t = Table(
            header=["Name", "Shares"],
            roles=["holder_name", "shares"],
            rows=[
                ["Jane Q. Smith", "1000"],
                ["All directors and executive officers as a group", "5000"],
            ],
        )
        out = postprocess.assemble_holders(t)
        idx = out.roles.index("is_group")
        assert [r[idx] for r in out.rows] == ["0", "1"]

    def test_address_split_out_of_the_name_cell(self):
        t = Table(
            header=["Name and Address", "Shares"],
            roles=["holder_name", "shares"],
            rows=[["BlackRock, Inc.\n55 East 52nd Street\nNew York, NY 10055", "22267957"]],
        )
        out = postprocess.assemble_holders(t)
        assert out.rows[0][0] == "BlackRock, Inc."
        assert "55 East 52nd Street" in out.rows[0][1]

    def test_wrapped_holder_name_is_joined(self):
        t = Table(
            header=["Name", "Shares"],
            roles=["holder_name", "shares"],
            rows=[["Massachusetts Financial", ""], ["Services Company", "1234"]],
        )
        out = postprocess.assemble_holders(t)
        assert out.rows[0][0] == "Massachusetts Financial Services Company"


class TestMarkerColumns:
    def test_footnote_only_columns_are_dropped(self):
        t = Table(
            header=["Shares", "", "Percent", ""],
            roles=["shares", "unknown", "percent", "unknown_2"],
            rows=[["1000", "(2)", "1.2", "%"], ["2000", "(3)", "2.4", "%"]],
        )
        out = postprocess.drop_marker_columns(t)
        assert out.roles == ["shares", "percent"]
        assert out.rows[0] == ["1000", "1.2"]

    def test_named_columns_are_never_dropped(self):
        """A real column is kept even if its values look marker-like."""
        t = Table(header=["Shares"], roles=["shares"], rows=[["%"]])
        assert postprocess.drop_marker_columns(t).roles == ["shares"]


class TestAssemblyDispatch:
    def test_plain_assembly_is_identity(self):
        t = Table(header=["a", "b"], roles=["x", "y"], rows=[["1", "2"]])
        assert postprocess.clean(t, Assembly.PLAIN).rows == [["1", "2"]]

    def test_person_year_works_without_a_year_column(self):
        """Director compensation is one row per director for a single year, so
        there is no year column to trigger row emission on."""
        t = Table(
            header=["Name", "Fees", "Total"],
            roles=["name_and_position", "fees_earned", "total"],
            rows=[["Jane Q. Smith", "280000", "440004"],
                  ["John A. Doe", "153723", "313727"]],
        )
        out = postprocess.assemble_people(t)
        assert [r[0] for r in out.rows] == ["Jane Q. Smith", "John A. Doe"]
        assert out.roles[:2] == ["name", "position"]


class TestTextHeaderDetectionIsPerTable:
    """Plain-text header detection must come from the profile, not the SCT.

    It was hardcoded to name/principal/position plus a year+pay guard, so an
    Item 403 header ("Name and Address of Beneficial Owner", "Percent of Class")
    was unrecognisable and ownership could never be extracted from ANY pre-2001
    ASCII filing — 0 candidates, silently. Fixing it took 1994-2000 ownership
    yield from 78% to 100%.
    """

    OWNERSHIP_ASCII = "\n".join([
        "        SECURITY OWNERSHIP OF CERTAIN BENEFICIAL OWNERS",
        "",
        "NAME AND ADDRESS OF                    AMOUNT AND NATURE OF   PERCENT",
        "BENEFICIAL OWNER                       BENEFICIAL OWNERSHIP   OF CLASS",
        "- ----------------------------------    --------------------   --------",
        "Franklin Resources, Inc.                          3,089,353      11.6",
        "Vanguard PRIMECAP Fund                            2,540,000       9.6",
    ])

    def test_ownership_header_found_in_plain_text(self):
        from sec_tables.select import text as textsel
        prof = _profiles.BENEFICIAL_OWNERSHIP
        lines = self.OWNERSHIP_ASCII.splitlines()
        hits = textsel._find_ascii_headers(lines, 0, len(lines), textsel.DEFAULT, prof)
        assert hits, "no ownership header located in a plain-text filing"

    def test_ownership_extracts_from_plain_text(self):
        r = st.extract(self.OWNERSHIP_ASCII, profile="ownership")
        assert r.ok, f"ownership not extracted from ASCII; flags={r.flags}"
        assert r.backend in (Backend.ASCII, Backend.SGML)
        names = [row[0] for row in r.table.rows]
        assert any("Franklin" in n for n in names), names

    def test_sct_terms_do_not_find_an_ownership_header(self):
        """Confirms the old behaviour was genuinely broken, not merely narrow."""
        from sec_tables.select import text as textsel
        lines = self.OWNERSHIP_ASCII.splitlines()
        hits = textsel._find_ascii_headers(
            lines, 0, len(lines), textsel.DEFAULT, _profiles.SCT
        )
        assert not hits, "SCT tiers should not match an Item 403 header"

    def test_every_profile_declares_text_header_tiers(self):
        for name, prof in _profiles.REGISTRY.items():
            assert prof.text_header_tiers, f"{name} has no text_header_tiers"

    def test_header_kind_uses_profile_tiers(self):
        """A strong Item 403 match must earn a strong kind, not be demoted."""
        from sec_tables.select import text as textsel
        from sec_tables.types import HeaderKind
        kind = textsel._header_kind(
            "name and address of beneficial owner percent of class",
            _profiles.BENEFICIAL_OWNERSHIP,
        )
        assert kind is HeaderKind.NAME_PRINCIPAL_POSITION


class TestCandidateDeduplication:
    """The same physical table is emitted more than once and must not read as
    a tie.

    Half of all 'ambiguous_selection' warnings on pre-2001 SCT filings were one
    table counted repeatedly — once as SGML and again for each header line the
    three-line window matched. That is a duplicate-generation problem, not
    genuine ambiguity, and reporting it raised a warning that was not true while
    making the score margin meaningless.
    """

    def test_identical_content_collapses(self):
        from sec_tables.select import chain
        from sec_tables.types import Backend, Candidate, HeaderKind
        a = Candidate("<table><tr><td>x</td></tr></table>", Backend.SGML, HeaderKind.NAME_PRINCIPAL_POSITION, 10, 0)
        b = Candidate("x", Backend.ASCII, HeaderKind.POSITION_ONLY, 10, 1)
        assert chain.content_key(a) == chain.content_key(b)
        assert len(chain._collapse_duplicates([a, b])) == 1

    def test_different_content_is_kept(self):
        from sec_tables.select import chain
        from sec_tables.types import Backend, Candidate, HeaderKind
        a = Candidate("alpha", Backend.ASCII, HeaderKind.POSITION_ONLY, 10, 0)
        b = Candidate("beta", Backend.ASCII, HeaderKind.POSITION_ONLY, 10, 1)
        assert len(chain._collapse_duplicates([a, b])) == 2

    def test_strongest_header_survives_a_collapse(self):
        from sec_tables.select import chain
        from sec_tables.types import Backend, Candidate, HeaderKind
        weak = Candidate("same", Backend.ASCII, HeaderKind.POSITION_ONLY, 10, 0)
        strong = Candidate("same", Backend.SGML, HeaderKind.NAME_PRINCIPAL_POSITION, 10, 1)
        kept = chain._collapse_duplicates([weak, strong])
        assert len(kept) == 1 and kept[0].header_kind is HeaderKind.NAME_PRINCIPAL_POSITION

    def test_real_filing_no_longer_reports_a_false_tie(self):
        """The 1997 fixture used to flag ambiguous_selection with margin 0."""
        from datetime import date as _d
        r = st.extract_sct(
            (Path(__file__).parent / "fixtures" / "dal_1997_sct.txt").read_text(errors="ignore"),
            _d(1997, 9, 19),
        )
        assert r.ok
        assert "ambiguous_selection" not in r.flags
        assert r.meta["margin"] > 0
        # And the answer is unchanged.
        assert r.table.rows[0][0] == "Ronald W. Allen"
        assert r.table.rows[0][3] == "562500"


class TestInputRepresentationInvariance:
    """The same document must give the same table however it was read.

    lxml parses bytes and str differently (encoding detection, entity handling),
    so passing raw bytes to the DOM backend while the text backend got a decoded
    string made results depend on the caller's file-reading style. The CLI reads
    bytes; the golden tests read text — so the tested path was not the shipped
    path, and a fixture could pass while real usage produced different columns.
    """

    FIXTURES = Path(__file__).parent / "fixtures"

    @pytest.mark.parametrize("name,profile,day", [
        ("azz_2019_ownership.html", "ownership", date(2019, 5, 28)),
        ("cmp_2024_director_comp.html", "director", date(2024, 1, 29)),
        ("dal_1997_sct.txt", "sct", date(1997, 9, 19)),
        ("dal_1994_sct.txt", "sct", date(1994, 9, 13)),
    ])
    def test_bytes_and_text_agree(self, name, profile, day):
        p = self.FIXTURES / name
        a = st.extract(p.read_bytes(), profile=profile, filing_date=day)
        b = st.extract(p.read_text(errors="ignore"), profile=profile, filing_date=day)
        assert a.table.roles == b.table.roles, name
        assert a.table.rows == b.table.rows, name
        assert a.flags == b.flags, name

    def test_decode_preserves_undecodable_bytes(self):
        """`errors='ignore'` would shorten the line, shifting every ASCII column."""
        from sec_tables.select.chain import decode
        raw = b"Smith \xa9 Jones  1997  100"
        assert len(decode(raw)) == len(raw)


class TestInlineXbrlDocuments:
    """Modern SEC filings are inline-XBRL XHTML and open with an XML declaration.

    lxml rejects a *str* carrying one ("Unicode strings with encoding declaration
    are not supported"), so normalising input to text made every such filing
    unparseable by the DOM backend. It then fell through to the plain-text path
    and returned SGML fragments full of undecoded `&#160;` entities — a silent
    wrong answer on a large and growing share of filings.
    """

    XHTML = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<body><table>"
        "<tr><th>Name and Principal Position</th><th>Year</th><th>Salary</th><th>Total</th></tr>"
        "<tr><td>Jane Q. Smith Chief Executive Officer</td><td>2023</td>"
        "<td>$100,000</td><td>$150,000</td></tr>"
        "</table></body></html>"
    )

    def test_xml_declaration_does_not_break_dom_parsing(self):
        from sec_tables.select import dom
        assert dom.parse(self.XHTML) is not None

    def test_extracts_via_dom_not_the_text_fallback(self):
        r = st.extract_sct(self.XHTML, date(2024, 1, 11))
        assert r.ok
        assert r.backend is Backend.DOM, "fell through to the plain-text path"

    def test_bytes_and_str_still_agree_for_xhtml(self):
        a = st.extract_sct(self.XHTML.encode(), date(2024, 1, 11))
        b = st.extract_sct(self.XHTML, date(2024, 1, 11))
        assert a.table.rows == b.table.rows


class TestNameAndTitleWithoutASeam:
    """Filings run a name into a title with no separator at all.

    "Tim Cook Chief Executive Officer" arrives as one cell with no line break,
    comma or footnote marker. Reading it as all-title — because it contains title
    words — loses the executive's name and leaves the row anonymous.
    """

    @pytest.mark.parametrize("cell,name,title", [
        ("Tim Cook Chief Executive Officer", "Tim Cook", "Chief Executive Officer"),
        ("Jane Q. Smith Senior Vice President", "Jane Q. Smith", "Senior Vice President"),
    ])
    def test_split_at_the_first_title_word(self, cell, name, title):
        assert postprocess.split_name_title(cell) == (name, title)

    @pytest.mark.parametrize("cell", [
        "Chief Executive Officer",
        "President and",
        "Chairman of the Board,",
    ])
    def test_a_pure_title_stays_a_title(self, cell):
        assert postprocess.split_name_title(cell)[0] == ""

    def test_a_plain_name_is_untouched(self):
        assert postprocess.split_name_title("Ronald W. Allen") == ("Ronald W. Allen", "")


class TestEmpiricalHeaderMap:
    """Roles come from headers that were observed, not headers that were imagined.

    Hand-written regexes encode what a header OUGHT to look like. Measured against
    an inventory of ~12,000 header occurrences from a real corpus, the regexes and
    the empirical map disagreed on 20.8% of them — and the map was right about the
    distinctions that matter for ownership concentration.
    """

    @pytest.fixture(scope="class")
    def hmap(self):
        from sec_tables.normalize import load_header_map
        m = load_header_map(_profiles.BENEFICIAL_OWNERSHIP.header_map)
        assert m is not None, "ownership header map is not shipped"
        return m

    def test_map_is_shipped_and_loads(self, hmap):
        assert len(hmap) > 100

    @pytest.mark.parametrize("header,role", [
        ("Percent of Voting Power", "percent_voting_power"),
        ("Shares beneficially owned | Right to acquire", "shares_right_to_acquire"),
        ("Options Exercisable Within 60 Days", "shares_right_to_acquire"),
        ("Share Equivalent Units", "stock_units"),
        ("Name and Address of Beneficial Owner", "holder_name"),
        ("Title of Class", "share_class"),
    ])
    def test_distinctions_the_regexes_flattened(self, header, role):
        got = normalize.infer_role(
            header,
            rules=_profiles.BENEFICIAL_OWNERSHIP.role_rules,
            header_map=normalize.load_header_map("ownership_header_roles"),
        )
        assert got == role

    def test_options_are_not_outstanding_shares(self):
        """SEC counts options exercisable within 60 days as beneficially owned,
        but they are NOT outstanding shares. Folding them into `shares`
        overstates every holder's concentration."""
        hm = normalize.load_header_map("ownership_header_roles")
        for h in ("Right to Acquire", "Options Exercisable Within 60 Days"):
            assert normalize.infer_role(
                h, rules=_profiles.BENEFICIAL_OWNERSHIP.role_rules, header_map=hm
            ) != "shares"

    def test_sub_label_wins_in_a_merged_header(self, hmap):
        """A merged header is group-then-sub. Both halves match a pattern, but only
        the second identifies the column. Taking the longest match outright labels
        'Shares beneficially owned | Right to acquire' as plain `shares`."""
        from sec_tables.normalize import clean_header
        assert hmap.lookup(clean_header("Shares beneficially owned Right to acquire")) \
            == "shares_right_to_acquire"

    def test_pipes_are_separators_not_content(self, hmap):
        from sec_tables.normalize import clean_header
        a = hmap.lookup(clean_header("Total | Beneficial | Ownership"))
        b = hmap.lookup(clean_header("Total Beneficial Ownership"))
        assert a == b

    def test_schema_carries_the_new_roles(self):
        roles = set(schema.OWNERSHIP_SCHEMA.roles_for(schema.ERA_SINGLE))
        assert {"shares_right_to_acquire", "percent_voting_power",
                "stock_units", "total"} <= roles

    def test_map_is_consulted_before_the_regexes(self):
        """Without the map these fall through to a hand-written rule."""
        hm = normalize.load_header_map("ownership_header_roles")
        h = "Percent of Voting Power"
        without = normalize.infer_role(h, rules=_profiles.BENEFICIAL_OWNERSHIP.role_rules)
        with_map = normalize.infer_role(h, rules=_profiles.BENEFICIAL_OWNERSHIP.role_rules,
                                        header_map=hm)
        assert with_map == "percent_voting_power"
        assert without != with_map

    def test_sct_is_unaffected(self):
        """Only ownership declares a map; compensation must be untouched."""
        assert _profiles.SCT.header_map is None
        assert _profiles.DIRECTOR_COMP.header_map is None
