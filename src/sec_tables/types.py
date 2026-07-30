"""Core value types.

Everything here is plain data. No filesystem, no config, no network — the
whole library operates on bytes you already have.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Backend(str, Enum):
    """Which extraction strategy produced a candidate."""

    DOM = "dom"          # parsed HTML tree (lxml)
    SGML = "sgml"        # regex-matched <TABLE> block, unparseable as a tree
    ASCII = "ascii"      # space-aligned plain-text table (pre-2001 EDGAR)
    NARRATIVE = "narrative"  # prose paragraphs, no table present


class Assembly(str, Enum):
    """How raw grid rows map onto output rows.

    Table shapes differ in kind, not degree, so this cannot be one algorithm:

    PERSON_YEAR  one row per executive per fiscal year, with the name printed
                 only on the first of that person's rows and the title stacked
                 beneath it (Item 402(c), 402(f), 402(h), 402(r)).
    HOLDER       one row per beneficial owner, with long names and addresses
                 wrapped over continuation lines and section labels interleaved
                 ("Directors and Executive Officers:") (Item 403).
    PLAIN        the grid is already the answer; emit rows unchanged (Item 402(v),
                 201(d), pay ratio).
    """

    PERSON_YEAR = "person_year"
    HOLDER = "holder"
    PLAIN = "plain"


class HeaderKind(str, Enum):
    """Strength of the identifying header match, best to worst."""

    NAME_PRINCIPAL_POSITION = "npp"
    PRINCIPAL_POSITION = "pp"
    POSITION_ONLY = "pos"
    NONE = "none"

    @property
    def weight(self) -> int:
        return {"npp": 3, "pp": 2, "pos": 1, "none": 0}[self.value]


@dataclass(frozen=True)
class Candidate:
    """One possible table, before selection."""

    snippet: str
    backend: Backend
    header_kind: HeaderKind
    score: int
    index: int = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Candidate(score={self.score}, backend={self.backend.value}, "
            f"header={self.header_kind.value}, chars={len(self.snippet)})"
        )


@dataclass
class Table:
    """A selected and tabulated table."""

    header: list[str]
    rows: list[list[str]]
    roles: list[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.rows), len(self.header))

    def to_csv(self) -> str:
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(self.roles or self.header)
        w.writerows(self.rows)
        return buf.getvalue()

    def to_dataframe(self):
        """Optional pandas view. pandas is not a hard dependency."""
        import pandas as pd

        return pd.DataFrame(self.rows, columns=self.roles or self.header)


REVIEW_FLAGS = frozenset({
    "ambiguous_selection",
    "missing_required_columns",
    "unmapped_columns",
    "era_mismatch",
    "below_score_threshold",
    "suspect_identity_values",
})
"""Flags meaning a human should look before trusting the result."""

PROVENANCE_FLAGS = frozenset({
    "ascii_source",
    "sgml_source",
    "no_filing_date",
    "predates_mandate",
})
"""Flags recording HOW a result was obtained. Not defects.

Kept distinct from REVIEW_FLAGS because conflating them makes every plain-text
extraction look broken — `ascii_source` is the library working as designed on a
1997 filing, not a problem with the answer.
"""


@dataclass
class Extraction:
    """Result of a full extract call, successful or not.

    `table` is None when nothing cleared the score threshold. `flags` records
    every reason a consumer should be cautious — an empty flag list is a
    positive claim, not a default.
    """

    table: Optional[Table]
    candidate: Optional[Candidate]
    era: Optional[str] = None
    flags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.table is not None

    @property
    def backend(self) -> Optional[Backend]:
        return self.candidate.backend if self.candidate else None

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)

    @property
    def review_flags(self) -> list[str]:
        """Flags asking for human review, in the order raised."""
        return [f for f in self.flags if f in REVIEW_FLAGS]

    @property
    def provenance_flags(self) -> list[str]:
        """Flags describing how the result was obtained. Not defects."""
        return [f for f in self.flags if f in PROVENANCE_FLAGS]

    @property
    def needs_review(self) -> bool:
        return bool(self.review_flags)

    @property
    def trustworthy(self) -> bool:
        """Extracted with nothing asking for review.

        Deliberately not called `valid`: nothing here checks the values against
        the filing, so this means "no detected problem", never "correct".
        """
        return self.ok and not self.needs_review
