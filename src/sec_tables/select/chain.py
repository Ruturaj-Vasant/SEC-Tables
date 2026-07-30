"""Backend fallback chain.

Runs every applicable backend, pools the candidates, and picks the best. Pooling
rather than short-circuiting matters: a filing can be tree-parseable *and* have
its real table only in an SGML block the tree parser silently dropped, so a
first-backend-wins chain would return a confidently wrong answer.

Ties break toward the stronger identifying header, then toward document order —
the SCT precedes its lookalike tables in every filing layout.
"""
from __future__ import annotations

import re

from ..profiles import TableProfile
from ..types import Backend, Candidate
from . import dom, text


def decode(raw: bytes | str) -> str:
    """Decode a filing to text, preserving every byte.

    UTF-8 first, then latin-1 — which cannot fail and is what pre-2001 EDGAR
    submissions actually are. `errors="ignore"` is avoided deliberately: dropping
    undecodable bytes silently shortens lines, and in a space-aligned table every
    dropped character shifts a column boundary.
    """
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def gather(raw: bytes | str, profile: TableProfile) -> list[Candidate]:
    """All candidates from all applicable backends.

    Both backends receive the SAME decoded text. Handing lxml raw bytes while the
    text scanner got a decoded string made the result depend on how the caller
    happened to read the file: identical documents produced different tables, and
    since the CLI reads bytes while the tests read text, the tested path was not
    the shipped one.
    """
    as_text = decode(raw)
    as_bytes: bytes | str = as_text

    out: list[Candidate] = []

    if text.looks_like_text_filing(as_text):
        out.extend(text.candidates(as_text, profile))
        # An old submission can still embed real HTML tables; try the tree too,
        # but only as a supplement.
        out.extend(dom.score_candidates(as_bytes, profile))
        return out

    out.extend(dom.score_candidates(as_bytes, profile))
    out.extend(dom.xpath_candidates(as_bytes, profile))
    if not out:
        # Tree parse failed or held no tables — fall back to text scanning.
        out.extend(text.candidates(as_text, profile))
    return out


def _rank_key(c: Candidate) -> tuple[int, int, int]:
    return (-c.score, -c.header_kind.weight, c.index)


_TAGS = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")


def content_key(candidate: Candidate) -> str:
    """A signature of what a candidate actually contains, ignoring rendering.

    The same physical table is routinely emitted more than once: as an SGML block
    *and* as ASCII, and again for each header line matched inside the scan window,
    since a three-line header test fires on consecutive offsets. Those are not
    competing answers — they are one answer generated several times.

    Stripping markup and collapsing whitespace makes the duplicates identical, so
    they can be collapsed before anything decides the selection was "ambiguous".
    """
    return _WS.sub(" ", _TAGS.sub(" ", candidate.snippet)).strip().lower()


def _collapse_duplicates(group: list[Candidate]) -> list[Candidate]:
    """Keep one candidate per distinct content, preferring the strongest header."""
    seen: dict[str, Candidate] = {}
    for c in group:
        key = content_key(c)
        prev = seen.get(key)
        if prev is None or (c.header_kind.weight, -c.index) > (prev.header_kind.weight, -prev.index):
            seen[key] = c
    return list(seen.values())


def select(raw: bytes | str, profile: TableProfile) -> tuple[Candidate | None, list[Candidate]]:
    """Return (best, ranked) where best clears the profile's minimum score.

    Candidates tied at the top score are de-duplicated by content first. Half of
    all observed "ambiguous" SCT selections in pre-2001 filings were one table
    counted repeatedly, and reporting those as ambiguous both raises a warning
    that is not true and makes the score margin meaningless.
    """
    cands = gather(raw, profile)
    if not cands:
        return None, []
    ranked = sorted(cands, key=_rank_key)

    top = ranked[0].score
    tied = [c for c in ranked if c.score == top]
    if len(tied) > 1:
        collapsed = _collapse_duplicates(tied)
        if len(collapsed) < len(tied):
            rest = [c for c in ranked if c.score != top]
            ranked = sorted(collapsed, key=_rank_key) + rest

    best = ranked[0]
    return (best if best.score >= profile.min_score else None), ranked


def margin(ranked: list[Candidate]) -> int | None:
    """Score gap between the top two candidates.

    A margin of 0 means two tables scored identically and the pick was decided
    by tiebreak alone — worth surfacing as a confidence flag rather than hiding.
    """
    distinct = [c for c in ranked if c.backend is not Backend.NARRATIVE]
    if len(distinct) < 2:
        return None
    return distinct[0].score - distinct[1].score
