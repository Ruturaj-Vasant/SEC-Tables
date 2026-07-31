"""Declarative table profiles.

A profile is the complete specification of one disclosure table: how to find it,
how to name its columns, what shape its rows take, and what would make a result
implausible. `api.py` reads a profile and knows nothing about any specific table.

That is the point. Supporting a new SEC table means adding a profile — data — not
writing another extractor. Anything in `api.py` that mentions a particular table
by name is a bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import schema as _schema
from .normalize import (
    DIRECTOR_COMP_ROLE_RULES,
    OWNERSHIP_ROLE_RULES,
    SCT_ROLE_RULES,
    RoleRules,
)
from .schema import Schema
from .types import Assembly


@dataclass(frozen=True)
class ValueBound:
    """A believable range for a numeric role, used by the benchmark."""

    role: str
    low: float
    high: float


@dataclass(frozen=True)
class SumCheck:
    """A stated total and the components that should add up to it."""

    total_role: str
    component_roles: tuple[str, ...]
    tolerance: float = 0.02  # filings round; footnoted add-backs are common


@dataclass(frozen=True)
class TableProfile:
    name: str

    # --- selection -------------------------------------------------------
    title_phrases: tuple[str, ...]
    column_tokens: tuple[str, ...]
    decoy_tokens: tuple[str, ...] = ()
    decoy_penalty: int = 4
    # Tokens the header MUST contain, if any. A hard gate, not a score nudge.
    # Some tables are defined by a mandated column no lookalike carries:
    # Item 402(r) requires "Fees Earned or Paid in Cash", which no Summary
    # Compensation Table has. Scoring alone cannot separate them, because the
    # two tables share every trailing column and the SCT is usually longer.
    require_tokens: tuple[str, ...] = ()
    stop_sections: tuple[str, ...] = ()
    token_cap: int = 8
    title_bonus: int = 4
    caption_bonus: int = 4
    min_score: int = 4
    identity_terms: tuple[str, ...] = ("name", "principal", "position")

    # How this table's header row is recognised in a PLAIN-TEXT filing, as tiers
    # ordered strongest-first. Every term in a tier must be present within a
    # three-line window. Tier 0 -> strongest header kind, tier 1 -> medium,
    # tier 2+ -> weak (which additionally requires supporting column tokens).
    #
    # This must be per-table. It was hardcoded to the SCT's
    # name/principal/position, which meant an Item 403 header — "Name and
    # Address of Beneficial Owner", "Percent of Class" — was unrecognisable, so
    # ownership could never be extracted from any pre-2001 ASCII filing.
    text_header_tiers: tuple[tuple[str, ...], ...] = (
        ("name", "principal", "position"),
        ("principal", "position"),
        ("position",),
    )

    # --- naming and shape ------------------------------------------------
    role_rules: RoleRules = SCT_ROLE_RULES
    # Name of a shipped empirical header map in `data/`, consulted BEFORE
    # role_rules. Built by inventorying headers that actually occur, so it knows
    # phrasings no hand-written pattern would anticipate.
    header_map: Optional[str] = None
    schema: Optional[Schema] = None
    assembly: Assembly = Assembly.PLAIN

    # Roles that must NOT be numerically normalized (identity/label columns).
    text_roles: frozenset[str] = frozenset(
        {"name", "position", "name_and_position", "year", "holder_name",
         "holder_address", "share_class", "section", "is_group", "unknown",
         "ignore"}
    )

    # --- plausibility (consumed by bench/measure.py) ---------------------
    identity_role: Optional[str] = None
    # Whether the identity column holds a person's name. False for Item 403,
    # whose holders are institutions and group subtotals: "General Motors Co."
    # trips a job-title test on "general", and "All directors and executive
    # officers as a group" trips it on both — yet both are valid holders.
    identity_is_person: bool = True
    value_bounds: tuple[ValueBound, ...] = ()
    sum_check: Optional[SumCheck] = None
    year_role: Optional[str] = None

    def era_for(self, filing_date) -> Optional[str]:
        return self.schema.era_for(filing_date) if self.schema else None

    def allowed_roles(self, era: Optional[str]) -> Optional[frozenset[str]]:
        """Roles the schema permits, or None to accept anything."""
        if self.schema is None or era is None:
            return None
        return frozenset(self.schema.roles_for(era))

    def applies_to(self, filing_date) -> bool:
        """Whether this disclosure was required as of a filing date."""
        return self.schema.applies_to(filing_date) if self.schema else True

    def required_roles(self, era: Optional[str]) -> frozenset[str]:
        if self.schema is None or era is None:
            return frozenset()
        return self.schema.required_roles(era)


# ---------------------------------------------------------------------------
# Item 402(c) / 402(b) — Summary Compensation Table
# ---------------------------------------------------------------------------
SCT = TableProfile(
    name="summary_compensation",
    title_phrases=("summary compensation table",),
    column_tokens=(
        "salary", "bonus", "stock", "option", "non-equity", "non equity",
        "incentive", "pension", "all other", "total", "compensation",
        "awards", "cash", "year", "fiscal year", "sars",
    ),
    # Item 402(d) grants-of-plan-based-awards and 402(f) outstanding-equity
    # tables share almost every token with the SCT and sit adjacent to it.
    decoy_tokens=(
        "grant", "grant date", "estimated future payouts", "exercise price",
        "expiration date", "number of securities underlying unexercised",
        "fees earned", "paid in cash",
    ),
    stop_sections=(
        r"grants of plan[- ]based awards",
        r"outstanding equity awards",
        r"option exercises and stock vested",
        r"pension benefits",
        r"nonqualified deferred compensation",
        r"director compensation",
        r"change[- ]in[- ]control",
        r"severance pay plan",
        r"retirement plans",
        r"certain relationships",
        r"security ownership",
    ),
    role_rules=SCT_ROLE_RULES,
    schema=_schema.SCT_SCHEMA,
    assembly=Assembly.PERSON_YEAR,
    identity_role="name",
    year_role="year",
    value_bounds=(ValueBound("salary", 1_000, 50_000_000),),
    sum_check=SumCheck(
        "total",
        ("salary", "bonus", "stock_awards", "option_awards",
         "non_equity_incentive", "pension_and_nqdc", "all_other_comp"),
    ),
)


# ---------------------------------------------------------------------------
# Item 402(r) — Director Compensation
# ---------------------------------------------------------------------------
DIRECTOR_COMP = TableProfile(
    name="director_compensation",
    title_phrases=("director compensation",),
    column_tokens=(
        "fees earned", "paid in cash", "stock awards", "option awards",
        "non-equity", "incentive", "pension", "all other", "total", "year",
        "compensation", "awards", "retainer",
    ),
    # The SCT is the mirror-image decoy: same trailing columns, different
    # leading ones. Salary and bonus are what officers get, not directors.
    decoy_tokens=("named executive", "principal position", "salary", "bonus", "grant date"),
    stop_sections=(
        r"summary compensation table",
        r"security ownership",
        r"certain relationships",
        r"equity compensation plan information",
        r"audit fees",
    ),
    identity_terms=("name", "fees", "total"),
    text_header_tiers=(
        ("name", "fees", "cash"),
        ("fees", "paid"),
        ("fees",),
    ),
    require_tokens=("fees earned", "paid in cash", "annual retainer"),
    role_rules=DIRECTOR_COMP_ROLE_RULES,
    schema=_schema.DIRECTOR_COMP_SCHEMA,
    assembly=Assembly.PERSON_YEAR,
    identity_role="name",
    value_bounds=(ValueBound("fees_earned", 0, 5_000_000),),
    sum_check=SumCheck(
        "total",
        ("fees_earned", "stock_awards", "option_awards",
         "non_equity_incentive", "pension_and_nqdc", "all_other_comp"),
    ),
)


# ---------------------------------------------------------------------------
# Item 403 — Security Ownership of Certain Beneficial Owners and Management
# ---------------------------------------------------------------------------
BENEFICIAL_OWNERSHIP = TableProfile(
    name="beneficial_ownership",
    title_phrases=(
        "security ownership of certain beneficial owners",
        "beneficial ownership",
        "security ownership",
    ),
    column_tokens=(
        "name", "address", "beneficial owner", "amount and nature",
        "shares", "percent", "class", "of class", "beneficially owned",
        "title of class",
    ),
    decoy_tokens=("salary", "bonus", "fees earned", "grant date", "principal position"),
    stop_sections=(
        r"summary compensation table",
        r"equity compensation plan information",
        r"section 16\(a\)",
        r"certain relationships",
        r"director compensation",
    ),
    identity_terms=("name", "percent", "class"),
    text_header_tiers=(
        ("name", "beneficial", "owner"),
        ("beneficial", "own"),
        ("percent", "class"),
        # No single-word tier: "percent" alone appears throughout ordinary proxy
        # prose and identifies nothing.
    ),
    require_tokens=("beneficial", "percent of class", "of class", "beneficially owned"),
    role_rules=OWNERSHIP_ROLE_RULES,
    header_map="ownership_header_roles",
    schema=_schema.OWNERSHIP_SCHEMA,
    assembly=Assembly.HOLDER,
    identity_role="holder_name",
    identity_is_person=False,
    # A percentage above 100 is not automatically wrong — overlapping
    # beneficial ownership genuinely produces it — so the bound is generous
    # and exists only to catch a shares column misread as a percentage.
    value_bounds=(ValueBound("percent", 0, 100),),
)


REGISTRY: dict[str, TableProfile] = {
    p.name: p for p in (SCT, DIRECTOR_COMP, BENEFICIAL_OWNERSHIP)
}

# Short aliases for the CLI.
ALIASES: dict[str, str] = {
    "sct": "summary_compensation",
    "comp": "summary_compensation",
    "director": "director_compensation",
    "dircomp": "director_compensation",
    "ownership": "beneficial_ownership",
    "bo": "beneficial_ownership",
}


def get(name: str) -> TableProfile:
    key = ALIASES.get(name, name)
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(
            f"unknown profile {name!r}; available: {sorted(REGISTRY)} "
            f"(aliases: {sorted(ALIASES)})"
        ) from None


def available() -> list[str]:
    return sorted(REGISTRY)
