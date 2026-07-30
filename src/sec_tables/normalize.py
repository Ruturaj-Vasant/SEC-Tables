"""Header text -> canonical role.

Two things make this harder than a dict lookup:

1. Filing headers are typographically damaged. Columns wrap mid-word across
   lines ("Compen- sation"), carry footnote markers ("Salary(1)"), use nbsp,
   and stack a group label above a sub-label ("Annual Compensation" / "Salary").
2. The same words mean different roles in different contexts. "Stock Awards"
   under Item 402(c) is grant-date fair value; the pre-2006 equivalent is
   "Restricted Stock Award(s)". And "Total" means something different in an
   ownership table than in a compensation one.

So the rules are *data*, supplied per table type, not a single global list.
Within a rule set order matters: the most specific patterns are tested first,
because "stock" alone would otherwise capture "Securities Underlying Options/SARs".
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional, Sequence

from .schema import SCT_SCHEMA

_FOOTNOTE = re.compile(r"\(\s*[0-9a-z]{1,3}\s*\)")
_HYPHEN_WRAP = re.compile(r"(\w)-\s+(\w)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_EMPTY_PARENS = re.compile(r"\(\s*\)")
_WS = re.compile(r"\s+")

PLACEHOLDER_HEADERS = frozenset({"", "$", "—", "–", "-", "%", "(", ")"})

# A rule set: ordered (role, patterns) pairs. First match wins.
RoleRules = tuple[tuple[str, tuple[str, ...]], ...]


def clean_header(raw: str | None) -> str:
    """Repair a raw header cell into comparable lowercase text."""
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace("\xa0", " ").replace("\n", " ")
    # Rejoin words split by a line break before collapsing whitespace, or
    # "Compen- sation" becomes "compen- sation" and never matches.
    s = _HYPHEN_WRAP.sub(r"\1\2", s)
    s = _FOOTNOTE.sub(" ", s)
    s = s.replace("$", " ").replace("/", " ")
    # "Total ($)(6)" leaves "( )" behind once the marker and footnote are gone;
    # an empty bracket is not part of the label.
    s = _EMPTY_PARENS.sub(" ", s)
    return _WS.sub(" ", s).strip(" ()").lower()


# ---------------------------------------------------------------------------
# Rule sets, one per table type.
# ---------------------------------------------------------------------------

SCT_ROLE_RULES: RoleRules = (
    ("name_and_position", (r"name and .*position", r"name .*principal position", r"name & .*position")),
    ("position", (r"principal position", r"\bposition\b", r"\btitle\b", r"occupation")),
    ("name", (r"\bname\b", r"named executive")),
    ("year", (r"fiscal year", r"\byear\b", r"\bfy\b")),
    ("salary", (r"\bsalary\b", r"base salary")),
    ("bonus", (r"\bbonus\b",)),
    # Pre-2006 exclusives. "other annual" must beat "all other".
    ("other_annual_comp", (r"other annual",)),
    ("ltip_payouts", (r"\bltip\b", r"long.?term incentive plan payout", r"\bpayouts?\b")),
    ("restricted_stock_awards", (r"restricted stock", r"restricted share")),
    ("options_sars", (r"securities underlying", r"options?\s*/?\s*sars", r"\bsars\b", r"number of options")),
    # Post-2006 exclusives.
    ("non_equity_incentive", (r"non.?equity incentive", r"non.?equity")),
    ("pension_and_nqdc", (r"change in pension", r"pension value", r"deferred compensation earnings", r"\bnqdc\b")),
    ("stock_awards", (r"stock awards?",)),
    ("option_awards", (r"option awards?",)),
    # Generic tails.
    ("all_other_comp", (r"all other",)),
    ("total", (r"\btotal\b",)),
)

# Item 402(r). Distinguished from the SCT by "fees earned or paid in cash",
# which no compensation table for officers carries.
DIRECTOR_COMP_ROLE_RULES: RoleRules = (
    ("fees_earned", (r"fees earned", r"paid in cash", r"\bfees\b", r"retainer")),
    ("name_and_position", (r"name and .*position", r"\bname\b", r"\bdirector\b")),
    ("year", (r"fiscal year", r"\byear\b")),
    ("non_equity_incentive", (r"non.?equity incentive", r"non.?equity")),
    ("pension_and_nqdc", (r"change in pension", r"pension value", r"deferred compensation earnings", r"\bnqdc\b")),
    ("stock_awards", (r"stock awards?", r"share awards?")),
    ("option_awards", (r"option awards?",)),
    ("all_other_comp", (r"all other",)),
    ("total", (r"\btotal\b",)),
)

# Item 403. Note "percent" precedes "shares": a header reading "Percent of
# Shares Outstanding" is a percentage, not a share count.
OWNERSHIP_ROLE_RULES: RoleRules = (
    # A combined "Name and Address of Beneficial Owner" column is primarily a
    # name — downstream work keys on the holder. It must be tested before the
    # bare address rule, which would otherwise claim it.
    ("holder_name", (r"name and address", r"name of beneficial", r"name and principal address")),
    ("holder_address", (r"^address", r"\baddress\b(?!.*\bname\b)")),
    ("percent", (r"percent", r"\bpct\b", r"% of", r"of class")),
    ("shares", (r"amount and nature", r"\bshares?\b", r"beneficially owned", r"\bamount\b", r"number of")),
    ("share_class", (r"title of class", r"\bclass\b", r"series")),
    ("holder_name", (r"name .*beneficial owner", r"\bname\b", r"beneficial owner", r"\bholder\b")),
)

_compiled_cache: dict[int, tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]] = {}


def compile_rules(rules: RoleRules):
    """Compile and memoise a rule set (rule sets are frozen tuples)."""
    key = id(rules)
    got = _compiled_cache.get(key)
    if got is None:
        got = tuple((role, tuple(re.compile(p) for p in pats)) for role, pats in rules)
        _compiled_cache[key] = got
    return got


def infer_role(
    header: str | None,
    era: Optional[str] = None,
    *,
    rules: Optional[RoleRules] = None,
    allowed: Optional[Iterable[str]] = None,
) -> str:
    """Map one header cell to a canonical role.

    `allowed` restricts the acceptable roles (normally a schema era's column set).
    When only `era` is given, the SCT schema supplies it — a convenience for the
    default table type.

    Returns a snake_case fallback rather than raising, so an unrecognised column
    survives into the output instead of being silently dropped.
    """
    text = clean_header(header)
    if text in PLACEHOLDER_HEADERS:
        return "unknown"

    ruleset = compile_rules(rules if rules is not None else SCT_ROLE_RULES)
    if allowed is not None:
        allowed_set: Optional[set[str]] = set(allowed)
    elif era is not None:
        allowed_set = set(SCT_SCHEMA.roles_for(era))
    else:
        allowed_set = None

    blocked: Optional[str] = None
    for role, patterns in ruleset:
        if not any(p.search(text) for p in patterns):
            continue
        if allowed_set is not None and role not in allowed_set and role not in ("name", "position"):
            # The schema says this column cannot exist here. Prefer a later,
            # valid match — but do not discard the information: a 2008-dated
            # filing with an LTIP column usually means the date is wrong, which
            # `Schema.era_from_roles` exists to catch.
            blocked = blocked or role
            continue
        return role

    if blocked:
        return blocked
    fallback = _NON_ALNUM.sub("_", text).strip("_")
    return fallback or "unknown"


def infer_roles(
    headers: Sequence[str],
    era: Optional[str] = None,
    *,
    rules: Optional[RoleRules] = None,
    allowed: Optional[Iterable[str]] = None,
) -> list[str]:
    """Map a header row, disambiguating repeats with a positional suffix."""
    allowed_set = set(allowed) if allowed is not None else None
    out: list[str] = []
    counts: dict[str, int] = {}
    for h in headers:
        role = infer_role(h, era, rules=rules, allowed=allowed_set)
        if role in counts and role != "unknown":
            counts[role] += 1
            role = f"{role}_{counts[role]}"
        else:
            counts.setdefault(role, 1)
        out.append(role)
    return out


def detect_era_from_roles(roles: list[str]) -> Optional[str]:
    """SCT-specific convenience wrapper over `Schema.era_from_roles`."""
    return SCT_SCHEMA.era_from_roles(roles)


_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_MISSING = frozenset({"", "-", "--", "—", "–", "*", "**", "n/a", "na", "nm", "none", "not applicable"})


def normalize_number(text: str | None) -> str:
    """Strip currency/grouping from a numeric cell.

    Sentinels for absent values ("-", "*", "n/a", "") return empty string rather
    than 0 — a missing payout coerced to zero silently biases every downstream
    mean, and in an ownership table it understates concentration.
    """
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).strip()
    if s.lower() in _MISSING:
        return ""
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").replace("\xa0", " ").strip()
    m = _NUM.search(s)
    if not m:
        return ""
    val = m.group(0)
    return f"-{val}" if negative and not val.startswith("-") else val
