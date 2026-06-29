# CLAUDE.md — Tableau-Reconstructor

This repo migrates **Tableau workbooks** off AWS Athena (Presto SQL) and onto
Snowflake (ANSI SQL), and promotes Snowflake-backed workbooks from test views to
production tables. It is a distilled, standalone extraction of the dashboard-
migration tooling built in the B2B `Presto-to-ANSI` project, packaged so any team
member can run the same workflow without that project's wider baggage.

Two Claude **skills** drive the work end-to-end (see `.claude/skills/`):

1. **`tableau-source-swap`** — Athena → Snowflake. Extract each datasource's Custom
   SQL + calc fields, translate to Snowflake, deploy as a gold view, then rewrite
   the workbook's connections to Snowflake while preserving every caption, drill-
   down hierarchy, and calculated field. Produces a Snowflake-backed `.twbx`.
2. **`production-swap`** — Snowflake → Snowflake. Repoint an already-Snowflake-backed
   workbook from one table (e.g. a `SANDBOX` view) to a final production table
   (`B2B_GOLD` or another prod schema). No dialect change; a surgical table repoint.

## Repo layout

```
Tableau-Reconstructor/
├── connectors/                 # standalone DB query helpers (the ONLY third-party-
│   ├── __init__.py             #   dependent code in the repo)
│   └── db.py                   #   execute_athena_query / execute_snowflake_query
├── reconstructor/              # the swap engines + extraction tools (pure stdlib)
│   ├── reconstruct.py          #   Athena→Snowflake source swap (config-driven)
│   ├── production_swap.py      #   Snowflake→Snowflake table repoint
│   ├── extract_custom_sql.py           # base Custom SQL per datasource
│   ├── extract_custom_sql_advanced.py  # base + translatable calc fields as columns
│   ├── extract_field_metadata.py       # captions / calc formulas / SQL-col maps → CSV
│   ├── verify_output.py        #   static, config-driven verification of a swap
│   └── deploy_view.py          #   deploy a gold view + smoke test (uses connectors)
├── .claude/skills/             # the two orchestration skills
│   ├── tableau-source-swap/SKILL.md
│   └── production-swap/SKILL.md
├── table_mappings.csv          # approved Athena ↔ Snowflake table mappings (shared)
├── Inputs/                     # source .twbx/.twb workbooks
├── Outputs/                    # per-workbook outputs (gold SQL, swapped .twbx, notes)
├── requirements.txt
├── .env.example                # credential template (copy to .env; never commit .env)
└── README.md
```

**Key design point:** the swap engines (`reconstructor/`) are **pure Python standard
library** (`zipfile`, `xml.etree`, `json`, `re`, `csv`) — they never touch a
database. Only `deploy_view.py` and any discovery scripts query Snowflake, and they
do so through the standalone **`connectors`** package:

```python
from connectors import execute_athena_query, execute_snowflake_query
```

## Environment Setup

- **Python**: use any Python 3.9+ (`python`). Install deps: `pip install -r requirements.txt`.
- **Credentials**: copy `.env.example` → `.env` and fill in your AWS + Snowflake
  values. `.env` is gitignored — **never commit or print it**.
- **Snowflake auth** is browser OAuth (Okta) by default and is **slow** — the first
  query opens a browser (1–2 min). Run deploy/verify queries in the background and
  batch them. Set `SNOWFLAKE_AUTHENTICATOR=externalbrowser` (default) or supply a
  password via `SNOWFLAKE_PASSWORD` with a non-browser authenticator.

## SQL Code Style

**ALWAYS use this style when writing translated SQL:**
- **Leading commas** in SELECT column lists
- **Unquoted functions**: `max()`, `sum()`, `concat()` (NOT `"max"()`)
- **Tab indentation** (not spaces)
- **Clean CTE structure**: comma on the same line as the closing parenthesis
- **Minimal parentheses** in JOINs

```sql
WITH cte_name AS (
	SELECT
		column1
		, column2
		, max(column3) AS max_col
	FROM table
	WHERE condition
	GROUP BY 1, 2
), another_cte AS (
	...
)
SELECT ...
```

## Presto → Snowflake Translation Reference

| Presto | Snowflake | Notes |
|--------|-----------|-------|
| `DATE_ADD('day', -1, date)` | `DATEADD(DAY, -1, date)` | Uppercase units, no quotes |
| `DATE_DIFF('day', d1, d2)` | `DATEDIFF(day, d1, d2)` | No quotes on unit |
| `date('2021-07-01')` | `'2021-07-01'::DATE` or `TO_DATE()` | |
| `DATE '2023-01-01'` | `'2023-01-01'::DATE` | |
| `DATE_PARSE(col, '%m/%d/%Y')` | `TO_DATE(col, 'MM/DD/YYYY')` | %m→MM, %d→DD, %Y→YYYY |
| `TRIM(BOTH FROM col)` | `TRIM(col)` | |
| `regexp_extract(str, pat, 1)` | `REGEXP_SUBSTR(str, pat, 1, 1, 'e', 1)` | Must include `'e'` for capture groups |
| `regexp_replace()` with lambda | `INITCAP()` | For title case specifically |
| `array[1]` | `array[0]` | 1-based → 0-based indexing |
| `CARDINALITY(array)` | `ARRAY_SIZE(array)` | |
| `array_sort(array_agg(col))[1]` | `ARRAY_AGG(col) WITHIN GROUP (ORDER BY col)[0]` | |
| `cycle_date` (history tables) | `LAST_MODIFIED_DATETIME` | See history table rules below |
| `date_trunc`, `GREATEST`, `COALESCE`, `CONCAT` | Same | Compatible, no change |

### CRITICAL: History Table `cycle_date` Replacement

Athena history tables use `cycle_date` as the snapshot date column. Snowflake
equivalents have both `source_file_partition_date` and `LAST_MODIFIED_DATETIME`.

**Always use `LAST_MODIFIED_DATETIME`** — it produces results within 0.2–0.7% of
Athena, while `source_file_partition_date` diverges 15–28%. It is a TIMESTAMP but
works without casting in date comparisons.

### CRITICAL: Array Handling

When translating Presto's `filter()` lambda, ALL THREE components are required:

```sql
-- Presto:
filter(array_agg(CASE WHEN condition THEN value END), x -> x IS NOT NULL)

-- Snowflake (all 3 parts required):
ARRAY_COMPACT(                                    -- (2) removes implicit NULLs from CASE ELSE
    ARRAY_AGG(CASE WHEN condition
        AND value IS NOT NULL                     -- (1) filters explicit source NULLs
        THEN value END)
    WITHIN GROUP (ORDER BY value)
)

-- In UNIONs with sentinel rows, use empty array (not null):
ARRAY_CONSTRUCT()::ARRAY                          -- (3) ARRAY_SIZE(null) fails; this returns 0
```

Omitting any component causes data integrity issues (NULL-polluted arrays, failed
`ARRAY_SIZE` calls).

## Tableau-to-SQL Translation Rules (calculated fields)

`extract_custom_sql_advanced.py` applies these when materializing row-level
Tableau calc fields as SQL columns:

| Tableau | SQL |
|---------|-----|
| `[field_name]` | `field_name` |
| `"string"` | `'string'` |
| `+` (string concat) | `\|\|` |
| `IF ... THEN ... ELSEIF ... END` | `CASE WHEN ... THEN ... WHEN ... END` |
| `ISNULL(field)` | `field IS NULL` |
| `==` | `=` |

**Materializable** (translate): row-level expressions — CASE WHEN, string concat,
IN-list checks, simple column renames, null handling. **Non-translatable** (skip,
keep as Tableau calcs): aggregations (SUM, COUNTD, RANK), LOD expressions
(`{FIXED ...}`), parameter-dependent fields, and fields referencing other calcs.

Note: `>>` (and `>`) in extracted SQL is `>` — an XML entity-encoding artifact from
Tableau's `.twb` format.

## Table Mapping Workflow

1. **Check `table_mappings.csv` first** — never re-ask about a known mapping. It is
   a plain CSV (columns: `athena_database, athena_table, snowflake_database,
   snowflake_schema, snowflake_table, approved_date, notes`); read and edit it
   directly.
2. **Detect new mappings** by comparing table names (fuzzy) and column schemas
   across Athena/Snowflake.
3. **Get user sign-off** before using any new mapping, then add a row to
   `table_mappings.csv`.

### Snowflake Naming Conventions (new test/gold views)

- New views created by the source-swap workflow use the **`reconstructor_`** prefix
  (e.g. `reconstructor_feed_metrics`), deployed to `SANDBOX.B2B` by default.
- Production tables live in `B2B_GOLD` (or another agreed prod schema) and are named
  per the productionization plan, not by this tool.

## Translation Workflow (source swap)

1. Read and understand the source SQL completely before translating.
2. Resolve all table references (check `table_mappings.csv`, detect new mappings).
3. Apply dialect translations per the reference table above.
4. Apply array-handling rules (check for `filter()` in source).
5. Apply naming conventions for new views (`reconstructor_<name>`).
6. **No logic changes** — only syntax translation; preserve business logic exactly.

## Common Pitfalls

1. ❌ Trailing commas / space indentation (use leading commas / tabs)
2. ❌ Quoting function names: `"max"()` (use `max()`)
3. ❌ Omitting any of the 3 array-handling components (IS NOT NULL + ARRAY_COMPACT + ARRAY_CONSTRUCT)
4. ❌ Using `source_file_partition_date` instead of `LAST_MODIFIED_DATETIME` for `cycle_date`
5. ❌ Forgetting `'e'` parameter in `REGEXP_SUBSTR` for capture groups
6. ❌ Using Presto date format codes (`%`) instead of Snowflake (`MM/DD/YYYY`)
7. ❌ Not adjusting array indices from 1-based to 0-based
