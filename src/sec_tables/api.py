"""Public API.

One call, bytes in, table out. No configuration object, no directory layout, no
network: the caller already has the document, and the library's only job is to
turn it into rows.

Nothing here names a specific table. Everything table-specific — which columns
exist, how headers map to roles, what shape the rows take, what schema versions
apply — comes from the `TableProfile`. If a table name appears in this module,
that is a bug: it means a new disclosure type cannot be added as data.

Quality flags are part of the return value, not a log line. A consumer building a
panel needs to know *which* rows to distrust, and a table that silently looks
clean is worse than one that admits its own ambiguity.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Union

from . import postprocess as _post
from . import profiles as _profiles
from . import tabulate
from .normalize import infer_roles, normalize_number
from .profiles import TableProfile
from .schema import ERA_TRANSITION
from .select import chain
from .types import Backend, Extraction, Table

ProfileArg = Union[str, TableProfile]

DEFAULT_PROFILE = "summary_compensation"


def _resolve(profile: ProfileArg) -> TableProfile:
    return _profiles.get(profile) if isinstance(profile, str) else profile


def _base_roles(roles: list[str]) -> set[str]:
    """Role names with any positional suffix stripped."""
    return {r.rsplit("_", 1)[0] if r[-1:].isdigit() else r for r in roles}


def extract(
    document: bytes | str,
    *,
    profile: ProfileArg = DEFAULT_PROFILE,
    filing_date: Optional[date] = None,
    normalize_numbers: bool = True,
    assemble: bool = True,
) -> Extraction:
    """Extract one table from one filing.

    `filing_date` selects the schema era for profiles whose schema is versioned.
    Omitting it is allowed but widens the accepted column set to the union of all
    versions and raises a flag, because era-blind matching cannot distinguish
    pre-2006 `restricted_stock_awards` from post-2006 `stock_awards`.
    """
    prof = _resolve(profile)
    best, ranked = chain.select(document, prof)

    era = prof.era_for(filing_date)
    result = Extraction(table=None, candidate=best, era=era)
    result.meta["candidates"] = len(ranked)
    result.meta["profile"] = prof.name
    if ranked:
        result.meta["top_score"] = ranked[0].score

    # Only meaningful for a versioned schema; a single-version table needs no date.
    if filing_date is None and prof.schema is not None and not prof.schema.single_version:
        result.flag("no_filing_date")

    # A filing older than the disclosure requirement cannot contain the table.
    # Reported so a caller can tell "not applicable" from "we failed to find it".
    if not prof.applies_to(filing_date):
        result.flag("predates_mandate")

    if best is None:
        result.flag("no_table_found")
        if ranked:
            result.flag("below_score_threshold")
        return result

    gap = chain.margin(ranked)
    result.meta["margin"] = gap
    if gap == 0:
        result.flag("ambiguous_selection")

    grid = tabulate.to_grid(best.snippet, best.backend)
    if not grid:
        result.flag("tabulation_failed")
        return result

    header, rows = tabulate.split_header(grid)
    header, rows = tabulate.drop_empty_columns(header, rows)
    if not rows:
        result.flag("no_data_rows")
        return result

    roles = infer_roles(header, rules=prof.role_rules, allowed=prof.allowed_roles(era))

    # Cross-check the date-derived era against the columns actually present. A
    # 2008-dated filing carrying an LTIP column means the date is wrong, not that
    # the regulation changed back.
    if prof.schema is not None:
        observed = prof.schema.era_from_roles(roles)
        if observed and era is not None:
            if era == ERA_TRANSITION:
                era = observed
                result.era = era
                roles = infer_roles(header, rules=prof.role_rules, allowed=prof.allowed_roles(era))
            elif observed != era:
                result.flag("era_mismatch")
                result.meta["era_from_columns"] = observed
                era = observed
                result.era = era
                roles = infer_roles(header, rules=prof.role_rules, allowed=prof.allowed_roles(era))

    # Split stacked person-year rows BEFORE numbers are cleaned. A cell holding
    # "2,268,698\n43,511,534" is two years of pay; `normalize_number` takes the
    # first match and discards the rest, so normalising first collapses the row
    # to a single value and then repeats it across every year.
    table = _post.explode_stacked_rows(Table(header=header, rows=rows, roles=roles))
    rows, roles = table.rows, table.roles

    if normalize_numbers:
        numeric_idx = {
            i for i, r in enumerate(roles)
            if r not in prof.text_roles and r.rsplit("_", 1)[0] not in prof.text_roles
        }
        rows = [
            [normalize_number(v) if i in numeric_idx else v for i, v in enumerate(r)]
            for r in rows
        ]

    if best.backend is Backend.ASCII:
        result.flag("ascii_source")
    elif best.backend is Backend.SGML:
        result.flag("sgml_source")

    table = Table(header=header, rows=rows, roles=roles)
    if assemble:
        before = len(table.rows)
        table = _post.clean(table, prof.assembly)
        result.meta["rows_before_assembly"] = before
        if not table.rows:
            result.flag("assembly_emptied_table")
            return result

    # Column flags are evaluated against the FINAL roles, not the raw header.
    # Postprocessing legitimately removes footnote-marker and empty duplicate
    # columns, so flagging them as unmapped before that runs reports a problem
    # that no longer exists — and a warning that is not true devalues every
    # other warning.
    # Re-normalise numerically after assembly. Postprocessing can *rename* a
    # column once its values are visible — an Item 403 percentage column headed
    # only "Class*" is recognised by content — and such a column was skipped by
    # the first pass, keeping its "%" and never becoming a number.
    # `normalize_number` is idempotent, so already-clean columns are unaffected.
    if normalize_numbers:
        final_numeric = {
            i for i, r in enumerate(table.roles)
            if r not in prof.text_roles and r.rsplit("_", 1)[0] not in prof.text_roles
        }
        table = Table(
            header=table.header,
            roles=table.roles,
            rows=[
                [normalize_number(v) if i in final_numeric else v for i, v in enumerate(row)]
                for row in table.rows
            ],
        )

    final_roles = list(table.roles)
    unmapped = [
        (h, r) for h, r in zip(table.header, final_roles)
        if r == "unknown" or r.startswith("unknown_")
    ]
    if unmapped:
        result.flag("unmapped_columns")
        result.meta["unmapped"] = [h for h, _ in unmapped]

    # An identity column holding addresses or footnote prose means the row
    # geometry drifted. Which holder a misaligned row belongs to is ambiguous, so
    # it is reported rather than guessed at.
    if prof.identity_role:
        suspect = _post.suspect_identities(table, prof.identity_role)
        if suspect:
            result.flag("suspect_identity_values")
            result.meta["suspect_identities"] = suspect[:5]

    required = prof.required_roles(era)
    if required:
        missing = required - _base_roles(final_roles)
        # `name_and_position` is split into `name` + `position` by person-year
        # assembly, so the composite role is satisfied by its parts.
        if "name_and_position" in missing and {"name", "position"} <= set(final_roles):
            missing = missing - {"name_and_position"}
        if missing:
            result.flag("missing_required_columns")
            result.meta["missing_roles"] = sorted(missing)

    result.table = table
    return result


def extract_sct(document: bytes | str, filing_date: Optional[date] = None) -> Extraction:
    """Convenience wrapper for the Summary Compensation Table."""
    return extract(document, profile="summary_compensation", filing_date=filing_date)


def candidates(document: bytes | str, profile: ProfileArg = DEFAULT_PROFILE):
    """Every scored candidate, ranked. For debugging a bad selection."""
    return chain.select(document, _resolve(profile))[1]


def available_tables() -> list[str]:
    """Registered profile names."""
    return _profiles.available()
