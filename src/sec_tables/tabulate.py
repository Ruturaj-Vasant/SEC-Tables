"""Selected snippet -> header + rows.

Three shapes need handling, and they fail in different ways:

  * DOM tables: colspan/rowspan must be expanded or columns misalign, and the
    header is often stacked over two or three rows that need merging.
  * SGML blocks: rows have no closing tags, so cells are recovered by regex.
  * ASCII tables: columns are delimited by runs of spaces at *stable offsets*.
    Splitting each line independently on whitespace destroys the alignment, so
    column boundaries are inferred once from the whole block.
"""
from __future__ import annotations

import re

from lxml import html as LH

from .normalize import PLACEHOLDER_HEADERS, clean_header
from .types import Backend

_SGML_ROW = re.compile(r"(?is)<\s*(?:tr|row)\b[^>]*>(.*?)(?=<\s*(?:tr|row)\b|</\s*table)")
_SGML_CELL = re.compile(r"(?is)<\s*(?:td|th|c)\b[^>]*>(.*?)(?=<\s*(?:td|th|c)\b|$)")
_TAGS = re.compile(r"(?s)<[^>]+>")


def _text(el) -> str:
    """Cell text with <br> preserved as a newline.

    Modern filings put an executive's name and their title in one cell separated
    by a <br>. Flattening that to a space produces a single field reading
    "Giovanni Caforio, M.D. Chairman and Chief Executive Officer", which is
    neither a usable name nor a usable title. The break is real structure, so it
    survives here and is split downstream.
    """
    parts: list[str] = []
    for node in el.iter():
        if node.tag == "br":
            parts.append("\n")
        if node is not el and node.text:
            parts.append(node.text)
        elif node is el and node.text:
            parts.append(node.text)
        if node is not el and node.tail:
            parts.append(node.tail)
    if not parts:
        parts = list(el.itertext())
    return clean_lines(" ".join(parts))


def clean_raw(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split())


def clean_lines(s: str) -> str:
    """Collapse whitespace within each line but keep line breaks.

    INTERIOR blanks are preserved. A cell stacking three fiscal years may have an
    empty middle segment — a year with no bonus — and dropping it shifts every
    later value up a row, silently attributing one year's pay to another. Only
    leading and trailing blanks are removed.
    """
    s = s.replace("\xa0", " ")
    lines = [" ".join(ln.split()) for ln in s.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------- DOM

def _dom_grid(table) -> list[list[str]]:
    """Expand a DOM table into a rectangular grid, honouring colspan/rowspan."""
    grid: list[list[str | None]] = []
    pending: dict[tuple[int, int], str] = {}

    for r, tr in enumerate(table.xpath(".//tr")):
        while len(grid) <= r:
            grid.append([])
        row = grid[r]
        c = 0
        for cell in tr.xpath("./td|./th"):
            while (r, c) in pending:
                while len(row) <= c:
                    row.append(None)
                row[c] = pending.pop((r, c))
                c += 1
            try:
                cspan = max(1, int(cell.get("colspan") or 1))
                rspan = max(1, int(cell.get("rowspan") or 1))
            except ValueError:
                cspan = rspan = 1
            val = _text(cell)
            for dc in range(cspan):
                while len(row) <= c + dc:
                    row.append(None)
                row[c + dc] = val
                for dr in range(1, rspan):
                    pending[(r + dr, c + dc)] = val
            c += cspan
        while (r, c) in pending:
            while len(row) <= c:
                row.append(None)
            row[c] = pending.pop((r, c))
            c += 1

    width = max((len(r) for r in grid), default=0)
    return [[(v or "") for v in (r + [None] * (width - len(r)))] for r in grid]


# ---------------------------------------------------------------- SGML

def _sgml_grid(block: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for rawrow in _SGML_ROW.findall(block):
        cells = [clean_raw(_TAGS.sub(" ", c)) for c in _SGML_CELL.findall(rawrow)]
        if not cells:
            stripped = clean_raw(_TAGS.sub(" ", rawrow))
            if stripped:
                cells = re.split(r"\s{2,}", stripped)
        if any(c for c in cells):
            rows.append(cells)
    if rows:
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        return rows
    # Pre-2001 <TABLE> blocks have no <TR>/<TD> at all — only <CAPTION>, <S> and
    # <C> position markers — so the content is really an ASCII table. Blank the
    # tags in place rather than removing them: deleting characters would shift
    # every column to the left of them and destroy the alignment the grid
    # inference depends on.
    return _ascii_grid(_TAGS.sub(lambda m: " " * len(m.group(0)), block))


# ---------------------------------------------------------------- ASCII

_RULE_CHARS = set("-=_ ")
_RULE_RUN = re.compile(r"[-=_]{2,}")


def is_rule_line(line: str) -> bool:
    """A separator line: only dashes/equals/underscores and spaces.

    A leading '- ' is common in EDGAR text (it escapes a line that would
    otherwise look like SGML), so it must not disqualify the line.
    """
    s = line.strip()
    if len(s) < 4:
        return False
    return set(s) <= _RULE_CHARS and any(c in "-=_" for c in s)


#  EDGAR escapes a line that begins with a dash by prefixing "- ", so the
#  submission is not misread as SGML markup. The escape is not part of the
#  table: it shifts that one line two characters right relative to every data
#  row, and a ruler read at face value therefore lands mid-value on every
#  column ("1997" splits into "19" / "97").
_EDGAR_DASH_ESCAPE = re.compile(r"^-\s(?=[-=_])")


def _unescape_rule(line: str) -> str:
    return line[2:] if _EDGAR_DASH_ESCAPE.match(line) else line


def _starts_from_ruler(lines: list[str]) -> list[int] | None:
    """Column starts taken from the widest ruler line.

    EDGAR text tables underline their headers with runs of dashes aligned to the
    columns, e.g. `----   -------   ------------`. That is an explicit statement
    of the column geometry and beats inferring it from whitespace.
    """
    best: list[int] | None = None
    best_n = 0
    for ln in lines:
        if not is_rule_line(ln):
            continue
        starts = [m.start() for m in _RULE_RUN.finditer(_unescape_rule(ln))]
        if len(starts) > best_n:
            best, best_n = starts, len(starts)
    if best and best_n >= 3:
        return sorted(set(best))
    return None


def _align_ruler_to_data(ruler: list[int], body: list[str]) -> list[int]:
    """Shift a ruler that is indented relative to the rows it underlines.

    A ruler line is not always in the same horizontal frame as the data. Delta's
    1994 proxy indents its underline three characters while the names begin at
    column 0, so the runs report [3, 38, 43, ...] for columns that actually start
    at [0, 35, 40, ...].

    Read at face value that truncates the leading characters of the first column
    on every row — "Ronald W. Allen" becomes "ald W. Allen" — and, worse, does so
    *silently*: the values still look like values, so no flag fires.

    Only the FIRST boundary is corrected, not the whole ruler. The indentation
    applies to the leading underline, not to the table's frame: shifting every
    run by the same amount fixes the names and then splits every numeric column
    instead ("1994" -> "19" / "94"). So the first boundary moves left to meet the
    data and the remaining runs, which already align, are left alone.
    """
    if not ruler or not body:
        return ruler or [0]
    data_left = min(len(ln) - len(ln.lstrip()) for ln in body)
    if ruler[0] <= data_left:
        return ruler
    return [data_left, *ruler[1:]]


def _column_starts(lines: list[str], min_gap: int = 2, blank_ratio: float = 0.9) -> list[int]:
    """Infer column boundaries for a space-aligned table.

    Prefers an explicit ruler line. Falls back to positions that are blank in at
    least `blank_ratio` of non-empty lines — a strict all-lines test fails on
    real tables, where deeply indented group headings ("LONG TERM COMPENSATION")
    occupy positions that are otherwise column gutters.
    """
    body = [ln for ln in lines if ln.strip() and not is_rule_line(ln)]
    if not body:
        return [0]

    if (ruler := _starts_from_ruler(lines)) is not None:
        return _align_ruler_to_data(ruler, body)

    width = max(len(ln) for ln in body)
    padded = [ln.ljust(width) for ln in body]
    n = len(padded)
    blank = [
        (sum(1 for p in padded if p[i] == " ") / n) >= blank_ratio
        for i in range(width)
    ]

    starts, run = [0], 0
    for i, is_blank in enumerate(blank):
        if is_blank:
            run += 1
            continue
        if run >= min_gap and i not in starts:
            starts.append(i)
        run = 0
    return sorted(set(starts))


_LEADER_DOTS = re.compile(r"[.…]{2,}\s*$")


def _strip_leaders(cell: str) -> str:
    """Remove dot leaders: `Ronald W. Allen..................` -> the name."""
    return _LEADER_DOTS.sub("", cell).strip()


def _ascii_grid(block: str) -> list[list[str]]:
    lines = [ln.rstrip() for ln in block.splitlines()]
    starts = _column_starts(lines)
    bounds = list(zip(starts, starts[1:] + [10**6]))

    rows: list[list[str]] = []
    for ln in lines:
        if not ln.strip() or is_rule_line(ln):
            continue
        cells = [_strip_leaders(clean_raw(ln[a:b])) for a, b in bounds]
        if not any(cells):
            continue
        rows.append(cells)
    return rows


# ---------------------------------------------------------------- header

def _looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(cells).lower()
    if not joined.strip():
        return False
    hits = sum(
        1 for t in ("name", "position", "year", "salary", "bonus", "total",
                    "stock", "option", "compensation", "percent", "class", "shares")
        if t in joined
    )
    digits = sum(c.isdigit() for c in joined)
    return hits >= 2 and digits <= len(joined) * 0.25


_YEAR_CELL = re.compile(r"^\(?(?:19|20)\d{2}\)?$")
_NUMERIC_CELL = re.compile(r"^[\s$(]*-?[\d,]+(?:\.\d+)?[\s)%]*$")


def _looks_like_data(cells: list[str]) -> bool:
    """A data row: a bare year, or several numeric cells.

    Used to terminate the header band. Counting rows instead fails on plain-text
    filings, whose headers routinely stack seven lines deep (group label, wrapped
    sub-labels, then a units row like `($)  ($)(1)`), while HTML headers are
    usually one or two.
    """
    nonempty = [c.strip() for c in cells if c.strip()]
    if not nonempty:
        return False
    if any(_YEAR_CELL.match(c) for c in nonempty):
        return True
    numeric = sum(1 for c in nonempty if _NUMERIC_CELL.match(c))
    return numeric >= 2 and numeric >= len(nonempty) / 2


def split_header(grid: list[list[str]], max_scan: int = 14) -> tuple[list[str], list[list[str]]]:
    """Find the header band and merge it into one label row.

    Filing headers routinely stack a group label over sub-labels
    ("Annual Compensation" above "Salary"), so all header rows found are joined
    per column rather than only the last one kept. The band ends at the first row
    that looks like data.
    """
    if not grid:
        return [], []

    limit = min(len(grid), max_scan)
    first_data = next((i for i in range(limit) if _looks_like_data(grid[i])), None)

    if first_data is not None and first_data > 0:
        last_header = first_data - 1
    else:
        last_header = -1
        for i, row in enumerate(grid[:limit]):
            if _looks_like_header(row):
                last_header = i

    if last_header < 0:
        width = max(len(r) for r in grid)
        return [f"col_{i}" for i in range(width)], grid

    band = grid[: last_header + 1]
    width = max(len(r) for r in band)
    header: list[str] = []
    for c in range(width):
        parts: list[str] = []
        for row in band:
            val = row[c] if c < len(row) else ""
            v = clean_raw(val)
            if v and v.lower() not in PLACEHOLDER_HEADERS and v not in parts:
                parts.append(v)
        header.append(" ".join(parts))
    return header, grid[last_header + 1 :]


def drop_empty_columns(header: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    if not header:
        return header, rows
    keep = [
        i for i in range(len(header))
        if clean_header(header[i]) not in PLACEHOLDER_HEADERS
        or any((r[i] if i < len(r) else "").strip() for r in rows)
    ]
    return (
        [header[i] for i in keep],
        [[(r[i] if i < len(r) else "") for i in keep] for r in rows],
    )


def blank_tags(text: str) -> str:
    """Replace markup with spaces of equal length.

    Plain-text filings carry SGML position markers — <TABLE>, <CAPTION>, and the
    <S>/<C> column-type row — inside otherwise space-aligned tables. They must be
    removed or they merge into the header text ("BONUS NAME AND PRINCIPAL
    POSITION <S>"), but *deleting* them would shift every column to their right
    and break the alignment the grid inference depends on.
    """
    return _TAGS.sub(lambda m: " " * len(m.group(0)), text)


def to_grid(snippet: str, backend: Backend) -> list[list[str]]:
    if backend is Backend.DOM:
        try:
            el = LH.fromstring(snippet)
        except Exception:
            return []
        table = el if el.tag == "table" else (el.xpath(".//table") or [None])[0]
        return _dom_grid(table) if table is not None else []
    if backend is Backend.SGML:
        return _sgml_grid(snippet)
    return _ascii_grid(blank_tags(snippet))
