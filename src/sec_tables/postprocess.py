"""Turn a raw grid into one row per executive-year.

Three artefacts of how filings are typeset, each of which corrupts the output if
left alone:

1. **Currency marker columns.** Filers put "$" in its own cell and the amount in
   the next, so every money column arrives as a pair and the header maps both to
   the same role — `salary`, `salary_2`. The marker half is always empty after
   numeric normalization, so it can be identified and folded away.

2. **Blank spacer rows.** Used for vertical rhythm. They carry no data and would
   otherwise become rows of empty strings.

3. **Stacked name/title blocks.** This is the substantive one. The SCT layout is
   one row per fiscal year, with the executive's name on the first of those rows
   and their title continuing on the rows beneath it:

       Roy C. Harvey             2022   1,148,333 ...
       President and             2021   1,095,333 ...
       Chief Executive Officer   2020   1,055,583 ...

   Read naively that is three different people, one of whom is named "President
   and". The name must be carried down across the year rows and the title
   accumulated separately.
"""
from __future__ import annotations

import re

from .types import Assembly, Table

# Words that mark a cell as a job title rather than a person's name. Checked
# before name shape, because "Chief Executive Officer" is capitalised like one.
#
# Matched on WORD BOUNDARIES, not as substrings. Substring matching silently
# reclassified real executives as titles — "and" occurs inside Alexander,
# Anderson, Sandra, Chandler and Alessandro — which attributed their pay rows to
# whoever preceded them in the table. "and" is therefore absent entirely: every
# title fragment containing it ("President and", "and Chief Executive Officer")
# is already caught by a substantive word.
_TITLE_WORDS = (
    "president", "chief", "officer", "cfo", "ceo", "coo", "cto", "cao", "cio",
    "evp", "svp", "vp", "vice", "executive", "chairman", "chairwoman", "chair",
    "director", "treasurer", "secretary", "counsel", "controller", "principal",
    "manager", "managing", "partner", "founder", "senior", "division",
    "general", "interim", "former", "operations", "financial", "finance",
    "legal", "accounting", "administrative", "commercial", "technology",
    "marketing", "officers", "employee", "head",
)
_TITLE_RE = re.compile(r"\b(?:" + "|".join(_TITLE_WORDS) + r")\b", re.IGNORECASE)

# A trailing comma may appear *inside* the sequence, not only at the end:
# "Thomas J. Roeck, Jr" puts one before the generational suffix. Requiring the
# comma only at the end rejects every such name and silently attributes their
# compensation rows to the previous executive.
_NAME_WORD = r"(?:[A-Z]\.|[A-Z][A-Za-z'’\-]+|van|von|der|de|del|da|di|la|le|Jr\.?|Sr\.?|II|III|IV|V)"
_NAME_RE = re.compile(rf"^(?:[A-Z]\.\s*)*[A-Z][A-Za-z'’\-]+,?(?:\s+{_NAME_WORD},?)*[.]?$")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_FOOTNOTE_TAIL = re.compile(r"\s*\((?:[0-9]{1,3}|[a-z])\)\s*$")
_TEXT_ROLES = frozenset({
    "name", "position", "name_and_position", "year", "holder_name",
    "holder_address", "share_class", "section", "is_group", "unknown",
})


def looks_like_title(text: str) -> bool:
    s = text.strip(" ,.")
    if not s:
        return False
    return bool(_TITLE_RE.search(s))


def looks_like_person(text: str) -> bool:
    s = _FOOTNOTE_TAIL.sub("", text).strip()
    if not s or len(s) < 3:
        return False
    if looks_like_title(s):
        return False
    words = s.split()
    if not (1 < len(words) <= 6):
        return False
    return bool(_NAME_RE.match(s))


def split_name_title(cell: str) -> tuple[str, str]:
    """Separate a person's name from a title packed into the same cell.

    Filings combine them three ways:
      "Roy C. Harvey<br>President and CEO"     -> a line break
      "D. Christian Koch, Chair, President"    -> a comma
      "Michael P. McMasters (7) Chief Exec..." -> a footnote marker

    Returns (name, title); title is empty when the cell holds only a name.
    """
    text = cell.strip()
    if not text:
        return "", ""

    if "\n" in text:
        head, *rest = [p.strip() for p in text.split("\n") if p.strip()]
        tail = " ".join(rest)
        # A <br> inside this cell means one of two different things, and guessing
        # wrong is silent: "Roy C. Harvey<br>President and CEO" is name-then-title,
        # but "Richard<br>P. Dealy" is a single name wrapped for column width.
        # Treating the second as name-then-title yields a director called
        # "Richard" and sweeps every later name into the title field.
        if tail and looks_like_title(tail):
            return _clean_name(head), tail
        joined = f"{head} {tail}".strip()
        if looks_like_person(joined):
            return _clean_name(joined), ""
        return _clean_name(head), tail

    # A footnote marker often sits exactly at the name/title seam.
    m = re.search(r"\((?:\d{1,3}|[a-z])\)\s*(?=\S)", text)
    if m and looks_like_title(text[m.end():]):
        return _clean_name(text[: m.start()]), text[m.end():].strip()

    # Try each comma as a seam, preferring the earliest that splits cleanly.
    for m in re.finditer(r",\s*", text):
        left, right = text[: m.start()], text[m.end():]
        if not right:
            continue
        if looks_like_person(left) and looks_like_title(right):
            return _clean_name(left), right.strip()

    # No seam: a cell that is all title is a continuation line, not a name.
    if looks_like_title(text) and not looks_like_person(text):
        return "", text
    return _clean_name(text), ""


def _clean_name(text: str) -> str:
    return _FOOTNOTE_TAIL.sub("", text).strip().rstrip(",").strip()


def _role_index(roles: list[str], want: str) -> int | None:
    for i, r in enumerate(roles):
        if r == want:
            return i
    return None


def merge_marker_columns(table: Table) -> Table:
    """Fold `$`-marker columns into the value column they precede."""
    roles, header, rows = list(table.roles), list(table.header), [list(r) for r in table.rows]

    def base(role: str) -> str:
        return role.rsplit("_", 1)[0] if role[-1:].isdigit() else role

    drop: set[int] = set()
    for i, role in enumerate(roles):
        if not role[-1:].isdigit():
            continue
        b = base(role)
        j = _role_index(roles, b)
        if j is None or j in drop:
            continue
        col_j = [(r[j] if j < len(r) else "") for r in rows]
        col_i = [(r[i] if i < len(r) else "") for r in rows]
        j_empty = all(not v.strip() for v in col_j)
        i_empty = all(not v.strip() for v in col_i)
        if j_empty and not i_empty:
            drop.add(j)
            roles[i] = b
        elif i_empty and not j_empty:
            drop.add(i)

    keep = [i for i in range(len(roles)) if i not in drop]
    return Table(
        header=[header[i] for i in keep if i < len(header)],
        rows=[[(r[i] if i < len(r) else "") for i in keep] for r in rows],
        roles=[roles[i] for i in keep],
    )


_MARKER_VALUE = re.compile(r"^(?:\((?:\d{1,3}|[a-z])\)|[%$*†‡]|\(\d{1,3}\)\s*\(\d{1,3}\))$")


def drop_marker_columns(table: Table) -> Table:
    """Drop columns that carry only footnote references or a lone symbol.

    Filings put footnote markers and the "%" sign in their own cells, so a
    two-column pair arrives as (value, "(3)") or (value, "%"). These are
    typography, not data. Only unnamed columns qualify — a column with a real
    header is kept even if its values look marker-like, since dropping a named
    column would silently lose a field.
    """
    roles, header, rows = list(table.roles), list(table.header), table.rows
    if not rows:
        return table

    drop: set[int] = set()
    for i, role in enumerate(roles):
        named = role != "unknown" and not role.startswith("unknown")
        if named:
            continue
        values = [(r[i] if i < len(r) else "").strip() for r in rows]
        present = [v for v in values if v]
        if present and all(_MARKER_VALUE.match(v) for v in present):
            drop.add(i)

    if not drop:
        return table
    keep = [i for i in range(len(roles)) if i not in drop]
    return Table(
        header=[header[i] for i in keep if i < len(header)],
        rows=[[(r[i] if i < len(r) else "") for i in keep] for r in rows],
        roles=[roles[i] for i in keep],
    )


def drop_empty_value_columns(table: Table) -> Table:
    """Drop suffixed duplicate roles that ended up entirely empty.

    `merge_marker_columns` only folds a pair when exactly one side is empty; a
    header split three ways across colspans can leave two empty siblings.
    """
    roles, header, rows = list(table.roles), list(table.header), table.rows
    if not rows:
        return table
    drop = {
        i for i, role in enumerate(roles)
        if role[-1:].isdigit()
        and not any((r[i] if i < len(r) else "").strip() for r in rows)
    }
    if not drop:
        return table
    keep = [i for i in range(len(roles)) if i not in drop]
    return Table(
        header=[header[i] for i in keep if i < len(header)],
        rows=[[(r[i] if i < len(r) else "") for i in keep] for r in rows],
        roles=[roles[i] for i in keep],
    )


def drop_blank_rows(table: Table) -> Table:
    rows = [r for r in table.rows if any(v.strip() for v in r)]
    return Table(header=table.header, rows=rows, roles=table.roles)


def assemble_people(table: Table) -> Table:
    """Collapse stacked name/title blocks into one row per person-year.

    Emits explicit `name` and `position` columns in place of the combined
    `name_and_position` column, since downstream panel work keys on the name.
    """
    roles = list(table.roles)
    # Index 0 is falsy, so `a or b` would discard a name column in the first
    # position — which is where it always is.
    name_i = _role_index(roles, "name_and_position")
    if name_i is None:
        name_i = _role_index(roles, "name")
    year_i = _role_index(roles, "year")
    if name_i is None:
        return table

    # Not every person-shaped table has a year. Item 402(r) director compensation
    # is one row per director for a single fiscal year, so there is no year column
    # to trigger on — fall back to "this row carries numbers". Same stacked
    # name/title problem, same solution, different emit condition.
    value_idx = [
        i for i, r in enumerate(roles)
        if i != name_i
        and r.rsplit("_", 1)[0] not in _TEXT_ROLES
        and r not in _TEXT_ROLES
    ]

    out_roles = ["name", "position"] + [r for i, r in enumerate(roles) if i not in (name_i,)]
    out_rows: list[list[str]] = []

    # Buffer each person's block before emitting. A title's final fragment can
    # appear on a line *after* that person's last year row ("and Chief Executive
    # Officer" trailing the 1995 row), so emitting eagerly would stamp every row
    # with a title that is still being built.
    current_name = ""
    title_parts: list[str] = []
    pending: list[list[str]] = []
    saw_year_for_current = False

    def flush() -> None:
        title = " ".join(title_parts)
        for buffered in pending:
            buffered[1] = title
        out_rows.extend(pending)
        pending.clear()

    for row in table.rows:
        cell = (row[name_i] if name_i < len(row) else "").strip()
        if year_i is not None:
            year = (row[year_i] if year_i < len(row) else "").strip()
            has_data = bool(_YEAR_RE.match(year))
        else:
            has_data = any((row[i] if i < len(row) else "").strip() for i in value_idx)

        if cell:
            cell_name, cell_title = split_name_title(cell)
            # `split_name_title` returns the whole cell as the name when it finds
            # no seam, so the person test still gates starting a new block —
            # otherwise a wrapped parenthetical ("(retired", "effective July 31,
            # 1997)") would open one.
            if cell_name and looks_like_person(cell_name) and (saw_year_for_current or not current_name):
                flush()
                current_name = cell_name
                title_parts = [cell_title] if cell_title else []
                saw_year_for_current = False
            elif current_name:
                for piece in (cell_title, cell_name):
                    if piece and piece not in title_parts:
                        title_parts.append(piece)
            elif cell_name:
                current_name = cell_name
                if cell_title:
                    title_parts.append(cell_title)

        if not has_data:
            continue

        saw_year_for_current = True
        rest = [(row[i] if i < len(row) else "") for i in range(len(roles)) if i != name_i]
        pending.append([current_name, "", *rest])

    flush()

    if not out_rows:
        return table

    out_header = ["Name", "Position"] + [
        (table.header[i] if i < len(table.header) else "")
        for i in range(len(roles)) if i != name_i
    ]
    return Table(header=out_header, rows=out_rows, roles=out_roles)


_SECTION_LABEL = re.compile(r":\s*$")
_GROUP_PHRASES = (
    "as a group", "all directors", "all executive officers",
    "named executive officers", "all officers",
)


def looks_like_group_row(text: str) -> bool:
    """A subtotal row ("All directors and executive officers as a group").

    These are real Item 403 disclosures, not noise, but they are aggregates and
    must be distinguishable from individual holders or any concentration measure
    computed downstream will double-count.
    """
    low = text.lower()
    return any(p in low for p in _GROUP_PHRASES)


_ADDRESS_HINT = re.compile(
    # A house number followed by a word — "55 East", "100 Vanguard". Written
    # WITHOUT a trailing \b: `\d+\s+\w\b` only matches when the following word is
    # a single character, so "55 East" never matched and the seam fell through to
    # the later "Street", cutting the address in half and leaving a holder called
    # "BlackRock, Inc. 55 East 52 nd".
    r"(?:\b\d{1,6}\s+[A-Za-z]"
    r"|\b(?:street|avenue|ave|road|boulevard|blvd|suite|floor|drive|lane|"
    r"plaza|parkway|way|centre|center|tower|building)\b"
    r"|\bp\.?\s*o\.?\s*box\b"
    r"|\b[A-Z]{2}\s+\d{5}\b)",
    re.IGNORECASE,
)


def _split_holder_address(cell: str) -> tuple[str, str]:
    """Separate a holder name from an address in the same cell.

    Item 403 mandates "Name and Address of Beneficial Owner" as one column, so
    institutional holders arrive as "BlackRock, Inc.<br>55 East 52nd Street<br>
    New York, NY 10055". The name is the first line; everything after it that
    looks like an address is the address.
    """
    # Flatten line breaks BEFORE looking for the seam. A filer may wrap this cell
    # anywhere: "BlackRock, Inc.<br>55 East 52nd Street" breaks between name and
    # address, but "BlackRock, Inc. 55 East 52nd<br>Street New York, NY" breaks
    # mid-address. Splitting on the first break gets the second case wrong and
    # produces a holder named "BlackRock, Inc. 55 East 52 nd". Where the address
    # *starts* is a content question, not a typography one.
    text = " ".join(cell.split())
    if not text:
        return "", ""
    m = _ADDRESS_HINT.search(text)
    if m and m.start() > 0:
        return text[: m.start()].strip(" ,"), text[m.start():].strip()
    # No address signal at all: fall back to the line break, if there was one.
    if "\n" in cell:
        head, *rest = [p.strip() for p in cell.split("\n") if p.strip()]
        return head, " ".join(rest)
    return text, ""


_PROSE_HINT = re.compile(
    r"\b(?:pursuant|schedule 13[gd]|securities and exchange|filed|statement|"
    r"reported|according to|includes?|represents?|consists?)\b",
    re.IGNORECASE,
)


def looks_like_address(text: str) -> bool:
    """Whether an identity value is really a street address.

    Plain-text ownership tables put the holder's address on its own line, and
    when the column geometry drifts those lines surface as separate holders —
    "82 Devonshire Street Boston, MA" appearing alongside real institutions.
    """
    t = text.strip()
    if not t:
        return False
    if re.match(r"^\d{1,6}\s+[A-Za-z]", t):      # opens with a house number
        return True
    m = _ADDRESS_HINT.search(t)
    return bool(m and m.start() == 0)


def looks_like_prose(text: str) -> bool:
    """Footnote text leaking into an identity column.

    Judged on prose markers alone, not length. Legitimate institutional holders
    are routinely long — "Neuberger Berman Group LLC Neuberger Berman Investment
    Advisers LLC Neuberger Berman Equity Funds" is one disclosed holder, and a
    word-count rule flags it as broken.
    """
    return bool(_PROSE_HINT.search(text))


def suspect_identities(table: Table, identity_role: str) -> list[str]:
    """Identity values that are not plausibly names of holders.

    Returned rather than silently repaired: the correct owner of a misaligned
    row is genuinely ambiguous, and inventing one would replace a visible defect
    with an invisible one.
    """
    if identity_role not in table.roles:
        return []
    i = table.roles.index(identity_role)
    bad = []
    for row in table.rows:
        v = (row[i] if i < len(row) else "").strip()
        if v and (looks_like_address(v) or looks_like_prose(v)):
            bad.append(v)
    return bad


def assemble_holders(table: Table) -> Table:
    """Collapse an ownership table to one row per beneficial holder.

    Different shape from a compensation table: there is no year, one row per
    holder rather than per person-year, and holder names and addresses wrap over
    continuation lines. Section labels ("Directors and Executive Officers:") are
    headings, not holders, and must not become rows.

    Adds an `is_group` column, because group subtotals are aggregates of rows
    that may also appear individually.
    """
    roles = list(table.roles)
    name_i = _role_index(roles, "holder_name")
    if name_i is None:
        name_i = _role_index(roles, "name")
    if name_i is None:
        return table

    value_idx = [
        i for i, r in enumerate(roles)
        if r.rsplit("_", 1)[0] in ("shares", "percent") or r in ("shares", "percent")
    ]
    if not value_idx:
        return table

    out_rows: list[list[str]] = []
    name_parts: list[str] = []
    section = ""

    for row in table.rows:
        cell = (row[name_i] if name_i < len(row) else "").strip()
        has_values = any((row[i] if i < len(row) else "").strip() for i in value_idx)

        if cell and not has_values:
            # Either a section heading or the first line of a wrapped name.
            if _SECTION_LABEL.search(cell):
                section = cell.rstrip(": ").strip()
                name_parts = []
            else:
                name_parts.append(cell)
            continue

        if not has_values:
            continue

        combined = " ".join([*name_parts, cell]) if cell else " ".join(name_parts)
        name_parts = []
        full_name, address = _split_holder_address(combined)
        rest = [(row[i] if i < len(row) else "") for i in range(len(roles)) if i != name_i]
        out_rows.append(
            [full_name, address, section, "1" if looks_like_group_row(full_name) else "0", *rest]
        )

    if not out_rows:
        return table

    out_roles = ["holder_name", "holder_address", "section", "is_group"] + [
        r for i, r in enumerate(roles) if i != name_i
    ]
    out_header = ["Holder", "Address", "Section", "Is Group"] + [
        (table.header[i] if i < len(table.header) else "")
        for i in range(len(roles)) if i != name_i
    ]
    return Table(header=out_header, rows=out_rows, roles=out_roles)


_STRATEGIES = {
    Assembly.PERSON_YEAR: assemble_people,
    Assembly.HOLDER: assemble_holders,
    Assembly.PLAIN: lambda t: t,
}


def clean(table: Table, assembly: Assembly = Assembly.PERSON_YEAR) -> Table:
    """Apply the shape-independent cleanups, then the profile's assembly."""
    t = merge_marker_columns(table)
    t = drop_marker_columns(t)
    t = drop_empty_value_columns(t)
    t = drop_blank_rows(t)
    return _STRATEGIES[assembly](t)
