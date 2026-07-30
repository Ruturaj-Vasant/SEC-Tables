"""Plain-text and SGML table selection — the pre-2001 EDGAR path.

Before roughly 2001, EDGAR filings are ASCII submissions. Their tables are one
of two things, and neither has a DOM:

  * SGML  — an uppercase <TABLE> block with no closing tags on rows or cells,
            which an HTML tree parser mangles or drops entirely.
  * ASCII — columns aligned with runs of spaces, terminated by blank lines or
            the next all-caps heading. There is no markup at all.

This is the window no structured-data feed reaches: XBRL tagging begins far
later, so these filings are only ever available as text. That makes this module
the reason the library covers 1994 onward instead of 2005 onward.

Ported from a standalone script and decoupled: it takes text, returns scored
candidates, and touches no filesystem.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..profiles import TableProfile
from ..types import Backend, Candidate, HeaderKind

_SGML_TABLE = re.compile(r"(?is)<\s*table\b.*?</\s*table\s*>")
_MONEYISH = re.compile(r"(\$|,|\d)")
_COL_GAP = re.compile(r"\S\s{2,}\S")
_ROW_LIKE = re.compile(r"\$?\s*\d")

# An ASCII candidate has no markup proving it is tabular, so its shape is the
# only evidence. Applied regardless of header strength: a strong header match
# does not mean a table exists. A sentence containing "beneficial owner"
# matched a strong tier, skipped the shape check that was scoped to the weak
# tier only, and was returned as an ownership table containing prose.
_MIN_DATA_ROWS = 2

# An old EDGAR submission marks the end of a table explicitly. Honour it: the
# footnote block that follows (<FN> ... </FN>) is prose about the table, and
# capturing it turns footnote fragments into extra holders.
_TABLE_END = re.compile(r"(?i)</\s*table|<\s*/?\s*fn\b")
_FOOTNOTE_BLOCK = re.compile(r"(?is)<\s*fn\b.*$")


def strip_footnote_block(block: str) -> str:
    """Drop the <FN> footnote block from an SGML table.

    Old EDGAR nests footnotes INSIDE <TABLE>...</TABLE>, so a correctly
    delimited match still carries several paragraphs of prose about the table.
    Left in, those paragraphs tabulate into extra rows and become holders with
    names like "(b)(ii)(G) of such Act, disclosing benefici".

    The closing </TABLE> is preserved, so the result is still a well-formed SGML
    block. Cutting it away too left a snippet that no longer looked like SGML and
    was re-read as plain ASCII with different column geometry.
    """
    if not _FOOTNOTE_BLOCK.search(block):
        return block
    had_close = re.search(r"(?i)</\s*table\s*>", block)
    trimmed = _FOOTNOTE_BLOCK.sub("", block).rstrip()
    return trimmed + ("\n" + had_close.group(0) if had_close else "")
_TAG = re.compile(r"(?s)<[^>]+>")


def blank_tags(line: str) -> str:
    """Blank SGML markup, preserving length so line offsets still align.

    The <S>/<C> column-type row of an old EDGAR table is pure uppercase, so a
    heading test reads it as the start of a new section and stops the capture
    immediately before the first data row.
    """
    return _TAG.sub(lambda m: " " * len(m.group(0)), line)


@dataclass(frozen=True)
class TextConfig:
    """Window sizes for text scanning.

    `window_after` is generous because an ASCII SCT plus its footnotes commonly
    runs past 200 lines, and truncating mid-table loses the later executives.
    """

    window_after: int = 300
    header_lookahead: int = 60
    capture_max: int = 220
    blank_run_ends_table: int = 2
    # How far above the identifying line a stacked header may reach.
    header_block_max: int = 14


DEFAULT = TextConfig()


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _is_heading(line: str) -> bool:
    """All-caps-ish line, which is how ASCII filings mark a new section."""
    s = line.strip()
    if len(s) < 6:
        return False
    letters = sum(c.isalpha() for c in s)
    if not letters:
        return False
    upper = sum(c.isupper() for c in s if c.isalpha())
    return (upper / letters) > 0.7


def _header_kind(text: str, profile: TableProfile) -> HeaderKind:
    """Header strength, from the profile's tiers rather than SCT terms.

    Scored on the same signature used to locate the header, so a strong Item 403
    match ("name" + "beneficial" + "owner") earns the same weight a strong SCT
    match does instead of being permanently demoted to the weakest kind.
    """
    low = text.lower()
    kinds = [HeaderKind.NAME_PRINCIPAL_POSITION, HeaderKind.PRINCIPAL_POSITION]
    for tier_idx, terms in enumerate(profile.text_header_tiers or ()):
        if all(t in low for t in terms):
            return kinds[tier_idx] if tier_idx < len(kinds) else HeaderKind.POSITION_ONLY
    if any(t in low for t in profile.identity_terms):
        return HeaderKind.POSITION_ONLY
    return HeaderKind.NONE


def _row_like_count(text: str) -> int:
    """Lines that look like data: a number plus multi-space column separation."""
    return sum(
        1 for ln in text.splitlines() if _ROW_LIKE.search(ln) and re.search(r"\s{2,}", ln)
    )


_DISQUALIFIED = -999


def score_snippet(snippet: str, backend: Backend, profile: TableProfile) -> tuple[int, HeaderKind]:
    low = snippet.lower()
    kind = _header_kind(snippet, profile)
    if profile.require_tokens and not any(t in low for t in profile.require_tokens):
        return _DISQUALIFIED, kind
    score = kind.weight
    score += sum(1 for t in profile.column_tokens if t in low)

    if backend is Backend.SGML:
        score += 1
        if _MONEYISH.search(snippet):
            score += 1
    else:
        if _row_like_count(snippet) >= 2:
            score += 1
        if _MONEYISH.search(snippet):
            score += 1

    if any(d in low for d in profile.decoy_tokens):
        score -= profile.decoy_penalty
    return score, kind


def _find_ascii_headers(
    lines: list[str], start: int, end: int, cfg: TextConfig, profile: TableProfile
) -> list[tuple[int, HeaderKind]]:
    """Locate header lines using the profile's text header tiers.

    Tolerates a header wrapped over three lines, which is normal in plain-text
    filings. The weakest tier additionally requires supporting column tokens and
    visible column gaps — otherwise ordinary prose containing a single header
    word registers as a table header.
    """
    tiers = profile.text_header_tiers or (("name",),)
    # Strongest tier -> strongest kind; anything past the second tier is weak.
    kinds = [
        HeaderKind.NAME_PRINCIPAL_POSITION,
        HeaderKind.PRINCIPAL_POSITION,
    ]

    out: list[tuple[int, HeaderKind]] = []
    limit = min(start + cfg.header_lookahead, end)
    for j in range(start, limit):
        block = "\n".join(lines[j : min(j + 3, end)]).lower()
        for tier_idx, terms in enumerate(tiers):
            if not all(t in block for t in terms):
                continue
            kind = kinds[tier_idx] if tier_idx < len(kinds) else HeaderKind.POSITION_ONLY
            if kind is HeaderKind.POSITION_ONLY:
                supporting = sum(1 for t in profile.column_tokens if t in block)
                if supporting < 2 or not _COL_GAP.search(block):
                    continue
            out.append((j, kind))
            break
    return out


def _header_block_start(lines: list[str], header_idx: int, max_back: int) -> int:
    """Walk up to the top of the contiguous header block.

    The identifying line ("NAME AND PRINCIPAL POSITION") is the *bottom* of a
    header that can stack seven lines deep — group spans, wrapped sub-labels,
    then a units row. Backing up a fixed two lines truncates it, and a column
    whose only distinguishing word lives on an excluded line (the lone "BONUS"
    above "(INCENTIVE COMPENSATION PLAN)") can no longer be identified at all.

    The block is bounded by a blank line, so stop there.
    """
    k = header_idx
    limit = max(header_idx - max_back, 0)
    while k > limit and lines[k - 1].strip():
        k -= 1
    return k


def _capture(
    lines: list[str],
    header_idx: int,
    end: int,
    cfg: TextConfig,
    profile: TableProfile,
    hard_stops: frozenset[int] = frozenset(),
) -> str:
    """Capture from the top of the header block to the table's natural end.

    Ends on a run of blank lines, a new all-caps heading, or a stop-section
    phrase — the three ways an ASCII table terminates.
    """
    stop_re = re.compile("|".join(profile.stop_sections), re.I) if profile.stop_sections else None
    out: list[str] = []
    blanks = 0
    start = _header_block_start(lines, header_idx, cfg.header_block_max)
    for k in range(start, end):
        if len(out) >= cfg.capture_max:
            break
        if k > header_idx and k in hard_stops:
            break                      # explicit end of table
        line = lines[k]
        out.append(line)
        blanks = blanks + 1 if not line.strip() else 0
        past_header = k > header_idx
        if past_header and blanks >= cfg.blank_run_ends_table and len(out) > 6:
            break
        if past_header and len(out) > 10 and (_is_heading(line) or (stop_re and stop_re.search(line or ""))):
            break
    return "\n".join(out).strip()


def _candidates_around(
    lines: list[str], anchor: int, cfg: TextConfig, profile: TableProfile
) -> list[Candidate]:
    total = len(lines)
    win_start, win_end = max(0, anchor - 10), min(total, anchor + cfg.window_after)
    window = "\n".join(lines[win_start:win_end])

    out: list[Candidate] = []
    for block in _SGML_TABLE.findall(window):
        block = strip_footnote_block(block)
        score, kind = score_snippet(block, Backend.SGML, profile)
        out.append(Candidate(block, Backend.SGML, kind, score, len(out)))

    # Markup-free view for geometry; blanking preserves both line count and
    # column offsets, so indices remain interchangeable with `lines`.
    plain = [blank_tags(ln) for ln in lines]
    stops = frozenset(i for i, ln in enumerate(lines) if _TABLE_END.search(ln))
    for header_idx, kind in _find_ascii_headers(plain, anchor, win_end, cfg, profile):
        snippet = _capture(plain, header_idx, win_end, cfg, profile, stops)
        if not snippet:
            continue
        # No data rows means prose, whatever the header looked like.
        if _row_like_count(snippet) < _MIN_DATA_ROWS:
            continue
        score, _ = score_snippet(snippet, Backend.ASCII, profile)
        out.append(Candidate(snippet, Backend.ASCII, kind, score, len(out)))
    return out


def candidates(text: str, profile: TableProfile, cfg: TextConfig = DEFAULT) -> list[Candidate]:
    """Enumerate scored text candidates.

    Anchors on the profile's title phrase when present. When absent — common in
    older filings that never spell out "Summary Compensation Table" — falls back
    to striding the whole document looking for header shapes.
    """
    lines = _norm(text).splitlines()
    lowered = [ln.lower() for ln in lines]

    anchors = [
        i for i, ln in enumerate(lowered)
        if any(all(w in ln for w in phrase.split()) for phrase in profile.title_phrases)
    ]

    out: list[Candidate] = []
    if anchors:
        for a in anchors:
            out.extend(_candidates_around(lines, a, cfg, profile))
        return out

    plain = [blank_tags(ln) for ln in lines]
    stops = frozenset(i for i, ln in enumerate(lines) if _TABLE_END.search(ln))
    stride = max(20, cfg.header_lookahead // 2)
    for i in range(0, len(plain), stride):
        end = min(len(plain), i + cfg.header_lookahead)
        for header_idx, kind in _find_ascii_headers(plain, i, end, cfg, profile):
            snippet = _capture(plain, header_idx, min(len(plain), header_idx + cfg.window_after), cfg, profile, stops)
            if not snippet:
                continue
            if _row_like_count(snippet) < _MIN_DATA_ROWS:
                continue
            score, _ = score_snippet(snippet, Backend.ASCII, profile)
            out.append(Candidate(snippet, Backend.ASCII, kind, score, len(out)))
    return out


def looks_like_text_filing(raw: str) -> bool:
    """True when a document should go down the text path rather than the DOM path.

    Old EDGAR submissions wrap SGML in <SEC-DOCUMENT>/<TYPE> headers and have
    few or no lowercase HTML structural tags.
    """
    head = raw[:4000].lower()
    if "<sec-document>" in head or "<ims-document>" in head:
        return True
    return ("<html" not in head) and ("<body" not in head)
