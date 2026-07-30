"""Column schemas, versioned against Regulation S-K.

A disclosure table's columns are not a convention to be discovered empirically —
they are mandated, and a mandate can change. So a schema is a *sequence of
versions*, each valid over a date range, and the era is resolved from the filing
date.

The Summary Compensation Table is the case that forces this design. The SEC's
2006 executive-compensation amendments (effective for fiscal years ending on or
after 2006-12-15) replaced the old Annual/Long-Term column groups. Collapsing
both into one column list silently discards three real columns from every
pre-2006 filing: `other_annual_comp`, `options_sars` and `ltip_payouts` simply do
not exist afterwards, and `stock_awards`, `option_awards`, `non_equity_incentive`
and `pension_and_nqdc` do not exist before.

Most tables have exactly one version. They use the same machinery with a single
`SchemaVersion` and no date logic, so nothing downstream needs to special-case
them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

ERA_PRE_2006 = "pre2006"
ERA_POST_2006 = "post2006"
ERA_TRANSITION = "transition"
ERA_SINGLE = "single"  # schemas with only one version


@dataclass(frozen=True)
class Column:
    role: str
    label: str
    numeric: bool = True
    required: bool = False


@dataclass(frozen=True)
class SchemaVersion:
    """One column layout, valid over a (possibly open-ended) date range."""

    era: str
    columns: tuple[Column, ...]
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None

    def covers(self, d: date) -> bool:
        if self.valid_from is not None and d < self.valid_from:
            return False
        if self.valid_to is not None and d > self.valid_to:
            return False
        return True

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(c.role for c in self.columns)

    @property
    def required_roles(self) -> frozenset[str]:
        return frozenset(c.role for c in self.columns if c.required)


@dataclass(frozen=True)
class Schema:
    """A named set of column layouts, resolved by filing date.

    `transition_era` names an optional overlap window in which either layout may
    legitimately appear; its column set is the union, which is permissive rather
    than confidently wrong.
    """

    name: str
    versions: tuple[SchemaVersion, ...]
    transition_era: Optional[str] = None
    transition_from: Optional[date] = None
    transition_to: Optional[date] = None
    # When the disclosure first became required. Filings older than this simply
    # do not contain the table — Item 402(r) director compensation was created by
    # the 2006 amendments, and before that directors' pay was narrative prose.
    # Without this, every pre-2006 proxy scores as a extraction failure for a
    # table that could not have been there.
    mandated_from: Optional[date] = None

    def __post_init__(self) -> None:
        if not self.versions:
            raise ValueError(f"schema {self.name!r} has no versions")

    @property
    def single_version(self) -> bool:
        return len(self.versions) == 1

    def applies_to(self, filing_date: Optional[date]) -> bool:
        """Whether this disclosure was required as of a filing date."""
        if self.mandated_from is None or filing_date is None:
            return True
        return filing_date >= self.mandated_from

    def era_for(self, filing_date: Optional[date]) -> str:
        """Resolve a filing date to an era.

        A single-version schema ignores the date entirely. For multi-version
        schemas an unknown date yields the transition era (union of columns) —
        permissive, never a silent guess at one layout.
        """
        if self.single_version:
            return self.versions[0].era
        if filing_date is None:
            return self.transition_era or ERA_TRANSITION
        if self.transition_era and self.transition_from and self.transition_to:
            if self.transition_from <= filing_date <= self.transition_to:
                return self.transition_era
        for v in self.versions:
            if v.covers(filing_date):
                return v.era
        return self.transition_era or ERA_TRANSITION

    def version(self, era: str) -> Optional[SchemaVersion]:
        for v in self.versions:
            if v.era == era:
                return v
        return None

    def columns_for(self, era: str) -> tuple[Column, ...]:
        if (v := self.version(era)) is not None:
            return v.columns
        # Transition / unknown era: union in a stable order.
        seen: dict[str, Column] = {}
        for ver in self.versions:
            for col in ver.columns:
                seen.setdefault(col.role, col)
        return tuple(seen.values())

    def roles_for(self, era: str) -> tuple[str, ...]:
        return tuple(c.role for c in self.columns_for(era))

    def required_roles(self, era: str) -> frozenset[str]:
        if (v := self.version(era)) is not None:
            return v.required_roles
        # In a transition era, require only what every version requires.
        common = set(self.versions[0].required_roles)
        for ver in self.versions[1:]:
            common &= set(ver.required_roles)
        return frozenset(common)

    def exclusive_roles(self) -> dict[str, frozenset[str]]:
        """Roles unique to one era. Used to detect a misdetected filing date."""
        if self.single_version:
            return {}
        out: dict[str, frozenset[str]] = {}
        for v in self.versions:
            others: set[str] = set()
            for o in self.versions:
                if o.era != v.era:
                    others |= set(o.roles)
            out[v.era] = frozenset(set(v.roles) - others)
        return out

    def era_from_roles(self, roles: list[str]) -> Optional[str]:
        """Infer era from the columns actually present.

        A filing whose columns include `ltip_payouts` is pre-2006 whatever its
        stated date says, which catches misparsed dates and amendments.
        """
        if self.single_version:
            return None
        base = {r.rsplit("_", 1)[0] if r[-1:].isdigit() else r for r in roles}
        hits = {era: len(base & excl) for era, excl in self.exclusive_roles().items()}
        positive = [era for era, n in hits.items() if n]
        return positive[0] if len(positive) == 1 else None


# ---------------------------------------------------------------------------
# Item 402(b) — Summary Compensation Table as in effect ~1993-2006.
# There was no mandated Total column in the old layout, so `total` is present
# but not required.
# ---------------------------------------------------------------------------
PRE_2006_COLUMNS: tuple[Column, ...] = (
    Column("name_and_position", "Name and Principal Position", numeric=False, required=True),
    Column("year", "Year", numeric=False, required=True),
    Column("salary", "Salary"),
    Column("bonus", "Bonus"),
    Column("other_annual_comp", "Other Annual Compensation"),
    Column("restricted_stock_awards", "Restricted Stock Award(s)"),
    Column("options_sars", "Securities Underlying Options/SARs"),
    Column("ltip_payouts", "LTIP Payouts"),
    Column("all_other_comp", "All Other Compensation"),
    Column("total", "Total"),
)

# Item 402(c) — current layout.
POST_2006_COLUMNS: tuple[Column, ...] = (
    Column("name_and_position", "Name and Principal Position", numeric=False, required=True),
    Column("year", "Year", numeric=False, required=True),
    Column("salary", "Salary"),
    Column("bonus", "Bonus"),
    Column("stock_awards", "Stock Awards"),
    Column("option_awards", "Option Awards"),
    Column("non_equity_incentive", "Non-Equity Incentive Plan Compensation"),
    Column("pension_and_nqdc", "Change in Pension Value and NQDC Earnings"),
    Column("all_other_comp", "All Other Compensation"),
    Column("total", "Total", required=True),
)

_TRANSITION_START = date(2006, 12, 15)
_TRANSITION_END = date(2007, 12, 31)

SCT_SCHEMA = Schema(
    name="item402c_summary_compensation",
    versions=(
        SchemaVersion(ERA_PRE_2006, PRE_2006_COLUMNS, valid_to=_TRANSITION_START),
        SchemaVersion(ERA_POST_2006, POST_2006_COLUMNS, valid_from=_TRANSITION_END),
    ),
    transition_era=ERA_TRANSITION,
    transition_from=_TRANSITION_START,
    transition_to=_TRANSITION_END,
)

# Item 402(r) — Director Compensation. Introduced by the same 2006 amendments,
# so there is only one layout and no era logic.
DIRECTOR_COMP_COLUMNS: tuple[Column, ...] = (
    Column("name_and_position", "Name", numeric=False, required=True),
    Column("fees_earned", "Fees Earned or Paid in Cash"),
    Column("stock_awards", "Stock Awards"),
    Column("option_awards", "Option Awards"),
    Column("non_equity_incentive", "Non-Equity Incentive Plan Compensation"),
    Column("pension_and_nqdc", "Change in Pension Value and NQDC Earnings"),
    Column("all_other_comp", "All Other Compensation"),
    Column("total", "Total", required=True),
)

DIRECTOR_COMP_SCHEMA = Schema(
    name="item402r_director_compensation",
    versions=(SchemaVersion(ERA_SINGLE, DIRECTOR_COMP_COLUMNS),),
    mandated_from=_TRANSITION_START,  # created by the 2006 amendments
)

# Item 403 — Security Ownership of Certain Beneficial Owners and Management.
# Holder-level, not person-year; no mandated numeric total.
OWNERSHIP_COLUMNS: tuple[Column, ...] = (
    Column("holder_name", "Name of Beneficial Owner", numeric=False, required=True),
    Column("holder_address", "Address", numeric=False),
    Column("share_class", "Title of Class", numeric=False),
    Column("shares", "Amount and Nature of Beneficial Ownership"),
    Column("percent", "Percent of Class"),
)

OWNERSHIP_SCHEMA = Schema(
    name="item403_beneficial_ownership",
    versions=(SchemaVersion(ERA_SINGLE, OWNERSHIP_COLUMNS),),
)


# ---------------------------------------------------------------------------
# Module-level helpers, kept for the SCT so existing callers and tests read
# naturally. New code should go through a profile's schema.
# ---------------------------------------------------------------------------

def era_for(filing_date: Optional[date]) -> str:
    return SCT_SCHEMA.era_for(filing_date)


def columns_for(era: str) -> tuple[Column, ...]:
    return SCT_SCHEMA.columns_for(era)


def roles_for(era: str) -> tuple[str, ...]:
    return SCT_SCHEMA.roles_for(era)


def is_valid_role(role: str, era: str) -> bool:
    return role in roles_for(era)


def era_exclusive_roles() -> dict[str, frozenset[str]]:
    return SCT_SCHEMA.exclusive_roles()
