# sec-tables

Find the right table in an SEC filing and turn it into rows — **1994 to present**.

Three disclosure tables supported today, each defined as *data* rather than code:
Summary Compensation (Item 402(c)/(b)), Director Compensation (402(r)) and
Beneficial Ownership (403).

Extracting a Summary Compensation Table is not a parsing problem, it is a
*selection* problem. A proxy statement contains hundreds of tables, several of
which look almost exactly like the one you want. This library scores every
candidate, picks one, and tells you how confident it is.

```bash
pip install git+https://github.com/Ruturaj-Vasant/SEC-Tables.git
export SEC_USER_AGENT="Your Project you@example.com"   # SEC requires a contact

sec-tables DAL --year 2023 --table sct -o delta.csv
```

It downloads the filing, finds the table, and writes CSV. Now the same command
against a **1994** filing — a plain-text submission with no HTML at all:

```bash
sec-tables DAL --year 1994 --table sct -o delta-1994.csv
```
```
[ok] delta-1994.csv — 15 rows · 10 columns · 6 people · era=pre2006
     DAL DEF 14A 1994-09-13 via sgml
     provenance: sgml_source (SGML <TABLE> block with no row or cell tags)
```
```csv
name,position,year,salary,bonus,other_annual_comp,restricted_stock_awards,options_sars,ltip_payouts,all_other_comp
Ronald W. Allen,"Chairman of the Board, President and Chief Executive Officer",1994,475000,0,8528,0,89000,0,18512
Ronald W. Allen,"Chairman of the Board, President and Chief Executive Officer",1993,487500,0,7077,0,0,0,17639
Ronald W. Allen,"Chairman of the Board, President and Chief Executive Officer",1992,516667,0,,365625,75000,0,
```

Note the empty `other_annual_comp` for 1992: the filing leaves that cell blank, and
a blank is preserved as blank rather than coerced to `0`.

Those columns are the pre-2006 layout — `other_annual_comp`, `restricted_stock_awards`,
`ltip_payouts` — because that is what Item 402 mandated in 1994. Ask for a 2023
filing and you get the post-2006 columns instead.

## In Python

```python
from datetime import date
from pathlib import Path
import sec_tables as st

result = st.extract_sct(Path("1994-09-13_DEF_14A.txt").read_bytes(), date(1994, 9, 13))

result.ok            # True
result.era           # 'pre2006'
result.backend       # Backend.ASCII  — no DOM existed to parse
result.flags         # ['ascii_source'] — provenance, not a problem
result.table.roles   # ['name', 'position', 'year', 'salary', 'bonus', ...]
print(result.table.to_csv())
```

Any supported table, same call:

```python
st.available_tables()
# ['beneficial_ownership', 'director_compensation', 'summary_compensation']

st.extract(document, profile="director",  filing_date=date(2023, 3, 16))
st.extract(document, profile="ownership", filing_date=date(2023, 3, 16))
```

Bytes or text, local file or download — the result is identical either way.

## Why this exists

Three things it does that a table parser does not.

**1. It reads plain-text filings, not just HTML.** Before ~2001 EDGAR filings are
ASCII: columns aligned with runs of spaces, or SGML `<TABLE>` blocks with no row
or cell tags at all. There is no DOM to walk, and XBRL tagging arrives more than
a decade later, so these documents are only ever available as text. That is why
coverage starts at 1994 rather than 2005.

**2. It versions its schema against Regulation S-K.** The SCT's columns are
mandated by Item 402, and they changed once — the SEC's 2006 amendments, effective
for fiscal years ending on or after 2006-12-15. Collapsing both eras into one
column list silently discards three real columns from every pre-2006 filing:

| pre-2006 (Item 402(b)) | post-2006 (Item 402(c)) |
| --- | --- |
| Other Annual Compensation | *(gone)* |
| Restricted Stock Award(s) | Stock Awards |
| Securities Underlying Options/SARs | Option Awards |
| LTIP Payouts | *(gone)* |
| *(no mandated Total)* | Non-Equity Incentive Plan Comp |
| | Change in Pension Value & NQDC |
| | **Total** (required) |

The era comes from the filing date and is cross-checked against the columns
actually present, so a wrong or amended date raises `era_mismatch` instead of
producing a table with impossible columns.

**3. It reports its own uncertainty.** `flags` is part of every result, not a log
line. A table that looks clean but is wrong is worse than one that admits
ambiguity.

| flag | meaning |
| --- | --- |
| `ambiguous_selection` | top two candidates tied on score |
| `unmapped_columns` | a header did not map to a known role |
| `missing_required_columns` | the era's mandatory columns are absent |
| `era_mismatch` | filing date and columns disagree |
| `ascii_source` / `sgml_source` | derived from plain text, not a DOM |
| `no_filing_date` | era could not be pinned from a date |
| `below_score_threshold` | best candidate was too weak to trust |
| `suspect_identity_values` | the name column holds addresses or footnote text — row alignment drifted |

## Measured behaviour

200 filings per table from a local corpus of 2,462, stratified by era, seed fixed
(`python bench/measure.py --root <corpus> --limit 200 --table sct`):

```
table                  n absent avail found  yield  clean%  strict  strict%   rows
summary_compensation 200    23   177   175  98.9%   93.1%     119   68.0%   2104
director_compensation200   118    82    74  90.2%   89.2%      45   60.8%    678
beneficial_ownership 200     3   197   176  89.3%   88.1%      87   49.4%   2006
```

- **absent** — the table is not in the document, or was not yet mandated at that
  date. Director Compensation was created by the 2006 amendments, so 1994-2000
  shows 50/50 absent: those proxies disclosed directors' pay in prose. Counting
  that as a failure to extract would blame the parser for a regulation.
- **yield** = found / documents that do contain the table. Not independently
  measured recall — the denominator comes from a phrase heuristic that shares
  vocabulary with the extractor.
- **clean%** — no detectable *value-level* implausibility.
- **strict%** — clean **and** no flag requesting review. **This is an
  automatic-acceptance rate, not an accuracy estimate**: it says what share can be
  used without a human looking, under the current flagging policy. Neither number
  measures correctness.

Backends actually used for the SCT, confirming the text path is load-bearing
rather than decorative:

```
1994-2000   {'sgml': 79, 'ascii': 13, 'dom': 3}
2011+       {'dom': 95}
```

### What these numbers are not

`clean%` is **not accuracy** — it is at most an *upper bound* on it. It counts
results with no inconsistency the checker can detect without ground truth:
components summing to a stated Total, an identity column that reads as a job
title, values outside a believable range, years inconsistent with the filing date.
A result can be entirely wrong and still look clean.

`1 - clean%` is a *detected-suspicion* rate, not a measured error rate: a flagged
row is **suspicious**, not proven wrong. A $1 salary can be real, and footnoted
add-backs legitimately break a Total.

`strict%` counts `ambiguous_selection`, `missing_required_columns`,
`unmapped_columns` and `era_mismatch` against a result. The gap between `clean%`
and `strict%` is large and honest.

On ties: half of all `ambiguous_selection` warnings on pre-2001 SCT filings were
the *same table counted twice* — emitted once as SGML and again for each header
line matched by the sliding window. Those are now de-duplicated by content before
anything is called ambiguous. The other half were materially different tables, and
**whether the right one was picked is unknown** — that needs labelled data, not
interpretation.

Claiming accuracy needs hand-labelled expected output. One case is committed
(`tests/fixtures/dal_1997_sct.txt`, SCT, 1997, ASCII — values read off the filing
by eye). Director Compensation and Beneficial Ownership have **no** hand-verified
fixture yet.

## Command line

```bash
sec-tables DAL --year 1997 --table sct --source local --root ./data -o delta.csv
```
```
[ok] delta.csv — 15 rows · 10 columns · 5 people · era=pre2006
     DAL DEF 14A 1997-09-19 via sgml
     provenance: sgml_source (SGML <TABLE> block with no row or cell tags)
```

CSV goes to stdout by default, the summary to stderr, so it pipes cleanly.
Warnings always print — a warning you can silence protects nobody — and
**provenance is shown separately from warnings**: `ascii_source` on a 1997 filing
means the library did the hard thing correctly, not that something went wrong.

| exit | meaning |
| --- | --- |
| 0 | extracted |
| 1 | no table found |
| 2 | extracted but flagged — **only with `--strict`** |
| 3 | usage error |

`--strict` is opt-in deliberately: a warning that breaks every script gets
suppressed, and then it protects nobody.

Other commands: `--list-tables`, `--list-filings`, `--cache-info`,
`--prefer earliest|latest`, `--no-cache`.

**Network fetching is not enabled yet** — `--source local` reads a directory tree
of `<TICKER>/<FORM>/<DATE>_<FORM>.<ext>`. The SEC source lands behind the same
`Source` interface.

## Install

```bash
pip install git+https://github.com/Ruturaj-Vasant/SEC-Tables.git   # one dependency: lxml

git clone https://github.com/Ruturaj-Vasant/SEC-Tables.git         # for development
cd SEC-Tables && pip install -e '.[dev]'
pytest                                                             # 213 tests, none touch the network
```

Python 3.10+. `pandas` is optional and only needed for `Table.to_dataframe()`.
The EDGAR client uses the standard library, so fetching adds no dependency.

Set a contact identity before fetching — SEC requires one and there is
deliberately no default:

```bash
export SEC_USER_AGENT="Your Project you@example.com"
```

## API

```python
extract(document, *, profile="summary_compensation", filing_date=None,
        normalize_numbers=True, assemble=True) -> Extraction
extract_sct(document, filing_date=None) -> Extraction
candidates(document, profile=...) -> list[Candidate]   # debug a bad selection
```

`document` is `bytes` or `str`. There is no configuration object, no directory
convention and no network access — the caller already has the document.

### Table profiles

Selection is data, not code. A profile declares the tokens that count, the
identifying header, and — most importantly — the **decoy tokens** that mark a
lookalike table:

```python
SCT = TableProfile(
    name="summary_compensation",
    title_phrases=("summary compensation table",),
    column_tokens=("salary", "bonus", "stock", "option", ...),
    decoy_tokens=("grant date", "estimated future payouts", "exercise price", ...),
    decoy_penalty=4,
)
```

Without that penalty the scorer reliably picks Item 402(d)'s
grants-of-plan-based-awards table, which shares nearly every token with the SCT
and sits directly next to it.

Scoring alone is not always enough. Director Compensation shares every trailing
column with the SCT, and the SCT is usually the longer table — so it wins on
points. Item 402(r) *mandates* "Fees Earned or Paid in Cash", which no SCT
carries, so that is a hard gate rather than a score nudge:

```python
require_tokens=("fees earned", "paid in cash", "annual retainer")
```

A profile also declares its schema, its header→role rules, its row shape
(`person_year`, `holder`, `plain`), whether its identity column holds a person,
and what would make a row implausible. `api.py` reads all of it and names no
table. Adding a disclosure type is data, not an extractor.

## Design

```
select/dom.py    scored candidates from an lxml tree; plus a structural XPath strategy
select/text.py   ASCII and SGML candidates for pre-2001 filings
select/chain.py  runs all applicable backends, pools candidates, ranks
tabulate.py      snippet -> grid (colspan/rowspan expansion, ASCII column geometry)
normalize.py     header -> canonical role, era-aware; numeric cleaning
postprocess.py   marker columns, blank rows, stacked name/title blocks
schema.py        Item 402 columns, versioned by era
profiles.py      declarative table definitions
api.py           one entry point
```

Backends are pooled, not short-circuited. A filing can be tree-parseable *and*
have its real table only inside an SGML block that the tree parser dropped, so a
first-backend-wins chain returns a confidently wrong answer.

## Filing quirks handled

Each of these was found by running against real documents, and each has a
regression test.

- **EDGAR's dash escape.** A line starting with `-` is written as `- ` so it is
  not read as markup. The ruler line under a header is therefore shifted two
  characters right of the data, and read at face value every value splits —
  `1997` becomes `19` / `97`.
- **SGML position markers.** The `<S>` / `<C>` row is pure uppercase, so a
  heading test reads it as a new section and stops the capture immediately before
  the first data row. Tags are blanked in place, preserving length, because
  deleting them shifts every column to their right.
- **Stacked name/title blocks.** The SCT prints one row per fiscal year with the
  name only on the first. Read naively, `President and` is a person.
- **Name and title in one cell.** Modern filings separate them with `<br>`.
  Flattened to a space you get `Giovanni Caforio, M.D. Chairman and Chief
  Executive Officer` — neither a name nor a title. `<br>` is preserved and split.
- **Title words inside real names.** Matching `"and"` as a substring classifies
  Alex**and**er, **And**erson, S**and**ra, Ch**and**ler and Aless**and**ro as job
  titles, attributing their pay to the previous executive. Matching is on word
  boundaries.
- **Generational suffixes.** `Thomas J. Roeck, Jr` has a comma mid-name.
- **Currency marker columns.** `$` in its own cell means every money column
  arrives as a pair mapping to the same role.
- **Seven-line headers.** A column whose only distinguishing word is on an upper
  line (`BONUS` above `(INCENTIVE COMPENSATION PLAN)`) is unidentifiable if the
  header band is truncated. The band ends at the first data row, not a row count.
- **Missing-value sentinels.** `-`, `*`, `n/a` become empty, never `0`. A missing
  payout coerced to zero biases every downstream mean.
- **Dot leaders.** `Ronald W. Allen..................`

## Known limitations

- **Neither `clean%` nor `strict%` is accuracy.** No labelled validation set yet.
- **Pre-2001 ASCII ownership is partly repaired but still the weakest path**
  (1994-2000 at 31% strict, up from 12%). Continuation lines now attach to the
  correct holder, `<FN>` footnote blocks are excluded, and a column is
  re-identified from its values when the header is truncated. Remaining failures
  raise `suspect_identity_values`, so they are visible rather than silent.
- **`ambiguous_selection` is very common pre-2001** because ASCII candidates tie
  on small integer scores. It makes `strict%` pessimistic there.
- **Only the SCT has a hand-verified fixture.** Director Compensation and
  Beneficial Ownership are measured but not ground-truthed.
- **Ownership yield is weakest in 2001-2005 (51%) and 1994-2000 (78%)** and the
  cause is not yet understood.
- **Ownership holder classification is not ported** — person/institution/group
  typing and the ticker-year metric panel are a separate piece of work. The
  library marks group subtotal rows (`is_group`) but does not classify holders.
- **PDF is not supported.** No non-EDGAR or international documents.
- **No network access, by design.** Bring your own documents.

## Disclaimer

Not affiliated with, endorsed by, or connected to the U.S. Securities and
Exchange Commission. "EDGAR" and "SEC" are used descriptively to identify the
public filing system this library reads.

Filing content is public and the SEC permits its reuse, but extraction is
heuristic. **Verify anything you rely on against the source filing** — the flags
exist precisely because some results should not be trusted unreviewed.

## Licence

MIT — see [LICENSE](LICENSE). Changes in [CHANGELOG.md](CHANGELOG.md).
