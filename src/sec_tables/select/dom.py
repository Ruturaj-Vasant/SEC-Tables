"""DOM-backed table selection for parseable HTML filings.

Two independent strategies, both returning scored candidates rather than a
single answer, so the chain can compare across backends:

`score_candidates` enumerates every <table> and ranks it on header tokens,
identity terms, money shape, decoy penalty, caption and title proximity.

`xpath_candidates` matches the identifying header row structurally, which is
precise when it fires and silent when the filing wraps its header across
sibling rows.
"""
from __future__ import annotations

import re

from lxml import html as LH

from ..profiles import TableProfile
from ..types import Backend, Candidate, HeaderKind

_MONEY = re.compile(r"\$|\b\d{3,}\b")
_LOWER = "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'"
# Low enough that no bonus can lift it over any profile's min_score.
_DISQUALIFIED = -999


def _norm(text: str | None) -> str:
    return " ".join(text.replace("\xa0", " ").lower().split()) if text else ""


def parse(html_bytes: bytes | str):
    """Parse to an lxml tree, or None if the document is not tree-parseable."""
    try:
        tree = LH.fromstring(html_bytes)
    except Exception:
        return None
    return tree if tree is not None else None


def _header_text(table, max_rows: int = 12) -> str:
    bits: list[str] = []
    for tr in table.xpath(".//tr")[:max_rows]:
        for cell in tr.xpath("./td|./th"):
            bits.append(_norm(" ".join(cell.itertext())))
    return " ".join(bits)


def _header_kind(text: str, profile: TableProfile) -> HeaderKind:
    has = {t: (t in text) for t in ("name", "principal", "position")}
    if has["name"] and has["principal"] and has["position"]:
        return HeaderKind.NAME_PRINCIPAL_POSITION
    if has["principal"] and has["position"]:
        return HeaderKind.PRINCIPAL_POSITION
    if has["position"] or any(t in text for t in profile.identity_terms):
        return HeaderKind.POSITION_ONLY
    return HeaderKind.NONE


def _title_bonus_indices(tree, tables, profile: TableProfile) -> set[int]:
    """Tables that contain, or immediately follow, the profile's title text."""
    by_id = {id(t): i for i, t in enumerate(tables)}
    bonus: set[int] = set()
    for phrase in profile.title_phrases:
        xp = (
            f"//*[contains(translate(normalize-space(string(.)), {_LOWER}), "
            f"'{phrase}')]"
        )
        try:
            nodes = tree.xpath(xp)
        except Exception:
            continue
        for node in nodes:
            for anc in node.iterancestors():
                if anc.tag == "table":
                    if (i := by_id.get(id(anc))) is not None:
                        bonus.add(i)
                    break
            nxt = node.xpath("following::table[1]")
            if nxt and (i := by_id.get(id(nxt[0]))) is not None:
                bonus.add(i)
    return bonus


def score_table(table, profile: TableProfile, index: int, title_bonus: set[int]) -> tuple[int, HeaderKind]:
    header = _header_text(table)
    full = _norm(" ".join(table.itertext()))
    kind = _header_kind(header, profile)

    # A mandated column that no lookalike carries disqualifies outright, rather
    # than merely losing points it might win back on token count.
    if profile.require_tokens and not any(t in header for t in profile.require_tokens):
        return _DISQUALIFIED, kind

    score = 0
    if kind is HeaderKind.NAME_PRINCIPAL_POSITION:
        score += 7
    elif kind is HeaderKind.PRINCIPAL_POSITION:
        score += 5
    elif kind is HeaderKind.POSITION_ONLY:
        score += 2

    score += min(len({t for t in profile.column_tokens if t in header}), profile.token_cap)

    if _MONEY.search(full):
        score += 2

    if any(d in header for d in profile.decoy_tokens):
        score -= profile.decoy_penalty

    for phrase in profile.title_phrases:
        cap = f"./caption[contains(translate(normalize-space(string(.)), {_LOWER}), '{phrase}')]"
        try:
            if table.xpath(cap):
                score += profile.caption_bonus
                break
        except Exception:
            pass

    if index in title_bonus:
        score += profile.title_bonus

    return score, kind


def score_candidates(html_bytes: bytes | str, profile: TableProfile) -> list[Candidate]:
    tree = parse(html_bytes)
    if tree is None:
        return []
    tables = tree.xpath("//table")
    if not tables:
        return []
    title_bonus = _title_bonus_indices(tree, tables, profile)
    out: list[Candidate] = []
    for i, table in enumerate(tables):
        score, kind = score_table(table, profile, i, title_bonus)
        out.append(
            Candidate(
                snippet=LH.tostring(table, encoding="unicode", method="html"),
                backend=Backend.DOM,
                header_kind=kind,
                score=score,
                index=i,
            )
        )
    return out


def _identity_xpath(terms: tuple[str, ...]) -> str:
    def has(term: str, axis: str = "") -> str:
        return (
            f"{axis}.//text()[contains(translate(., {_LOWER}), '{term}')]"
        )

    primary = terms[0]
    rest = terms[1:]
    if not rest:
        return f"//tr[{has(primary)}]"
    same = " and ".join(has(t) for t in rest)
    nextrow = " and ".join(has(t, "following-sibling::tr[1]") for t in rest)
    return f"//tr[{has(primary)} and (({same}) or ({nextrow}))]"


def xpath_candidates(html_bytes: bytes | str, profile: TableProfile) -> list[Candidate]:
    """Structural match on the identifying header row.

    Header cells are frequently split across two sibling <tr>s, so the pattern
    accepts the qualifying terms on the matched row or the one after it.
    """
    tree = parse(html_bytes)
    if tree is None:
        return []
    try:
        rows = tree.xpath(_identity_xpath(profile.identity_terms))
    except Exception:
        return []

    seen: set[bytes] = set()
    out: list[Candidate] = []
    for tr in rows:
        table = tr.getparent()
        while table is not None and getattr(table, "tag", None) != "table":
            table = table.getparent()
        if table is None:
            continue
        key = LH.tostring(table)[:400]
        if key in seen:
            continue
        seen.add(key)
        header = _header_text(table)
        out.append(
            Candidate(
                snippet=LH.tostring(table, encoding="unicode", method="html"),
                backend=Backend.DOM,
                header_kind=_header_kind(header, profile),
                # XPath match is evidence of identity but says nothing about
                # column fit, so borrow the scorer rather than inventing a value.
                score=score_table(table, profile, 0, set())[0],
                index=len(out),
            )
        )
    return out
