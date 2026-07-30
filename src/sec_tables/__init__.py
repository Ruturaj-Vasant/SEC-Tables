"""sec-tables — find the right table in an SEC filing and turn it into rows.

Covers 1994 to present by treating pre-2001 ASCII/SGML submissions as a
first-class input rather than an edge case, and versions its output schema
against Regulation S-K instead of flattening incompatible eras into one column
list.

New table types are added as profiles (data), not extractors.
"""
from .api import available_tables, candidates, extract, extract_sct
from .profiles import (
    BENEFICIAL_OWNERSHIP,
    DIRECTOR_COMP,
    SCT,
    SumCheck,
    TableProfile,
    ValueBound,
)
from .schema import (
    ERA_POST_2006,
    ERA_PRE_2006,
    ERA_SINGLE,
    ERA_TRANSITION,
    Column,
    Schema,
    SchemaVersion,
    columns_for,
    era_for,
    roles_for,
)
from .cache import FilingCache, default_cache_dir
from .sources import DEFAULT_FORM, FilingRef, LocalSource, Source, SourceError, pick_filing
from .types import (
    PROVENANCE_FLAGS,
    REVIEW_FLAGS,
    Assembly,
    Backend,
    Candidate,
    Extraction,
    HeaderKind,
    Table,
)

__version__ = "0.3.0"
__all__ = [
    "extract", "extract_sct", "candidates", "available_tables",
    "SCT", "DIRECTOR_COMP", "BENEFICIAL_OWNERSHIP",
    "TableProfile", "ValueBound", "SumCheck",
    "Schema", "SchemaVersion", "Column",
    "era_for", "columns_for", "roles_for",
    "ERA_PRE_2006", "ERA_POST_2006", "ERA_TRANSITION", "ERA_SINGLE",
    "Table", "Extraction", "Candidate", "Backend", "HeaderKind", "Assembly",
    "REVIEW_FLAGS", "PROVENANCE_FLAGS",
    "Source", "LocalSource", "FilingRef", "SourceError", "pick_filing", "DEFAULT_FORM",
    "FilingCache", "default_cache_dir",
]
