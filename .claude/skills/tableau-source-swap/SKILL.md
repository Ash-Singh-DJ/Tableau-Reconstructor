---
name: tableau-source-swap
description: >-
  End-to-end migration of a Tableau .twbx workbook (or standalone .tdsx data
  source) from AWS Athena (Presto SQL) to Snowflake (ANSI SQL). Extracts each
  datasource's Custom SQL and calculated fields, translates the SQL to Snowflake,
  deploys it as a Snowflake gold view (reusing an existing gold table if one
  already represents that datasource), then rewrites the data connections to point
  at Snowflake while preserving every caption, drill-down hierarchy, and calculated
  field. Produces a new Snowflake-backed .twbx/.tdsx. Use when the user wants to
  convert an Athena-backed Tableau dashboard or data source to Snowflake or
  "source-swap"/migrate a workbook. Operates in the Tableau-Reconstructor repo.
---

# Tableau Source Swap (Athena → Snowflake)

Given an Athena-backed Tableau `.twbx` (or standalone `.tdsx` data source), produce
a Snowflake-backed file of the same type. For each SQL datasource the skill extracts
its Custom SQL and fields, ensures a Snowflake gold view exists (reuse or create),
then rewrites the data connection in place to point at that view — keeping every
caption, drill-down hierarchy, calculated field, and worksheet binding intact.

**`.twbx` vs `.tdsx`:** the workflow is identical. A `.twbx` bundles a `.twb`
(root `<workbook>`, many datasources, worksheets); a `.tdsx` bundles a `.tds` whose
root *is* a single `<datasource>` (no worksheets, identified by `formatted-name`
rather than a caption). The engines handle both transparently — pass either path
and the output keeps the input's extension. Everything below applies to both; just
substitute `.tdsx` for `.twbx` in the paths.

## Prerequisites & environment

- Python 3.9+ (`python`). Install deps once: `pip install -r requirements.txt`.
- Run scripts from the **repo root**. The engines are pure stdlib; the deploy
  helper imports the standalone connectors package:
  `from connectors import execute_snowflake_query, execute_athena_query`.
  Credentials load from `.env` (copy `.env.example`; gitignored — never print or commit).
- Snowflake auth is browser OAuth (Okta) and is **slow** (first query opens a
  browser, 1–2 min). Run deploy/verify queries in the background and batch them.
- Translation rules, SQL code style, `table_mappings.csv`, and naming conventions
  live in the repo `CLAUDE.md`. **Follow them exactly** — they are the core of
  translation and are not duplicated here.

## What to gather up front

1. Path to the source `.twbx`/`.tdsx` (Athena, usually extract-backed) — typically
   under `Inputs/`.
2. **For each Athena datasource in the workbook: does a Snowflake gold table/view
   already represent it** (i.e. has this datasource's query already been
   translated and deployed)? Ask the user. For any datasource where one exists,
   reuse it. For the rest, the skill translates and creates a new SANDBOX view.
3. Target schema for any new views (default `SANDBOX.B2B`; name them
   `reconstructor_<name>`).

Phase 1 lists the datasources so you can ask question 2 concretely. Don't ask
about output workbooks — producing one is the skill's job.

---

## Phase 1 — Extract & inventory

Pull each datasource's Custom SQL and field metadata, and list the SQL datasources
so you can ask which already have a gold table.

```bash
PY=python

"$PY" reconstructor/extract_custom_sql.py "Inputs/<workbook>.twbx"      # base Custom SQL per datasource
"$PY" reconstructor/extract_field_metadata.py "Inputs/<workbook>.twbx"  # captions, calc formulas -> fields_*.csv
"$PY" reconstructor/reconstruct.py "Inputs/<workbook>.twbx" --emit-config  # auto-detected datasources + columns + calcs
# (a standalone data source works the same way — pass "Inputs/<source>.tdsx")
```

The skeleton lists every SQL-backed datasource with its base columns and the calc
fields available to materialize. Present that list and ask the user (question 2
above) which datasources already have a Snowflake gold table.

For each datasource, from `fields_*.csv`, classify calculated fields:
**materializable** (row-level: CASE WHEN, string concat, IN-list, null handling,
simple renames) vs skip (aggregations SUM/COUNTD/RANK, LOD `{FIXED ...}`,
parameter-dependent, or referencing other calcs). Only materializable calcs become
SQL columns. `reconstructor/extract_custom_sql_advanced.py` can auto-append them
and comment out the rest — use it to bootstrap.

**Joined/unioned datasources.** A datasource may join or union several Custom SQL
relations (the base extractor emits one `.sql` per relation; the advanced extractor
**flattens them into one query** — a CTE per relation plus the reconstructed JOINs,
with the ON-clauses translated to SQL — so the whole datasource becomes a *single*
gold view). Review that flattened query: the JOIN/ON translation is best-effort and
the relation-level SQL still needs the standard Phase 2 dialect translation. If the
join mixes Custom SQL with a **non-SQL leaf** (a Google Drive / Excel sheet, a
published extract), the advanced extractor flags it with a WARNING and the source
swap (Phase 4) will **refuse** that datasource — the standard engine won't fold a
non-SQL source into a Snowflake view. Those cases are rare; an Opus model can build
a bespoke swap (collapse the SQL leaves into the view, keep the non-SQL leaf as a
remaining federated join). Either resolve it bespoke or omit that datasource from
the config to swap the rest.

## Phase 2 — Translate (only datasources without an existing gold table)

For each datasource lacking a gold table, translate its Athena/Presto SQL to
Snowflake ANSI SQL **per the repo `CLAUDE.md` rules** (dialect table, array
3-component rule, `cycle_date` → `LAST_MODIFIED_DATETIME`, leading commas, tabs,
unquoted functions). Resolve table references via `table_mappings.csv` (edit the
CSV directly — there is no helper API); detect new mappings and **get user
sign-off** before using them. Append the materializable calc fields as trailing
SELECT columns. Save as `Outputs/<Workbook>/gold_<Datasource>.sql`.
**No logic changes — syntax only.**

Because you're building a new view, you choose its output column names. Either keep
standard UPPERCASE physical names (simplest), or rename to the workbook's lowercase
internal field names — this choice sets `casing` in Phase 4.

## Phase 3 — Deploy / confirm the gold views

For datasources you just translated: create the view as `CREATE OR REPLACE VIEW`
in the target schema (`reconstructor_<name>`). Use the deploy helper, which runs
the DDL and dumps the columns you'll need in Phase 4:

```bash
"$PY" reconstructor/deploy_view.py "Outputs/<Workbook>/gold_<Datasource>.sql" \
    --fqn SANDBOX.B2B.RECONSTRUCTOR_<NAME>
```

For datasources with a pre-existing gold table: just confirm it.

Either way, for **every** target gold view, you need the column names/order/types
and a `COUNT(*)` smoke test (the helper prints both via `--fqn`). Run these in the
background (OAuth latency). Record the column names + types — Phase 4 needs them for
`casing` and the calc binding types.

## Phase 4 — Rewrite the workbook connections

Fill in the config skeleton from Phase 1 and run the reconstructor. The config maps
each datasource to its gold view by its `match` value — the datasource caption for a
`.twbx`, or the `formatted-name` for a standalone `.tdsx` (the `--emit-config`
skeleton pre-fills whichever applies):

```json
{
  "connection": { "server": "...", "dbname": "SANDBOX", "schema": "B2B",
                  "warehouse": "B2B_S_WH", "role": "B2B_ANALYST_PRIVILEGED",
                  "authentication": "oauth" },
  "datasources": [
    { "match": "<datasource caption>", "view": "RECONSTRUCTOR_<NAME>",
      "casing": "upper",
      "calc_bindings": [
        ["GOLD_PHYSICAL_COL", "[Calculation_<original_id>]", "string"]
      ] }
  ]
}
```

- **`casing`** = how the gold view names its BASE columns vs the workbook's
  lowercase internal field names:
  - `upper` — gold has UPPERCASE physical names (`PRIMARY_NAME`); the engine
    decouples case to bind `[primary_name]`. Normal case for a standard view.
  - `lower` — the view renames columns to the lowercase internal names (identity).
  - `exact` — names already match exactly.
- **`calc_bindings`** — one `[gold_physical_col, original_calc_internal_id,
  local_type]` per *materialized* calc. The internal id is the `tableau_name` from
  `fields_*.csv`; `local_type` ∈ string|integer|real|boolean|date. The engine binds
  that gold column to the original calc field so the workbook keeps the field.
- Base bindings are **auto-derived** from the workbook's metadata-records — don't
  list them.
- Datasources omitted from the config are left untouched (Glossary, Parameters,
  live or Google-Drive sources).

Run it:

```bash
"$PY" reconstructor/reconstruct.py "Inputs/<workbook>.twbx" \
    --config "Outputs/<Workbook>/reconstruct_config.json" \
    -o "Outputs/<Workbook>/Source Swap/<workbook> - Source Swap.twbx" \
    --notes "Outputs/<Workbook>/Source Swap/RECONSTRUCTION_NOTES.md"
```

(`--template <a Snowflake-connected .twbx>` is an optional refinement that clones
authentic per-type metadata-record shapes; omit it and the engine derives shapes
from the input workbook itself.)

Then **statically verify** the output:

```bash
"$PY" reconstructor/verify_output.py \
    "Outputs/<Workbook>/Source Swap/<workbook> - Source Swap.twbx" \
    --config "Outputs/<Workbook>/reconstruct_config.json" \
    --input "Inputs/<workbook>.twbx"
```

It confirms: no `athena` substring remains; 0 `.hyper` entries; 0 leftover
`type="text"` relations in target datasources; every target relation points at its
gold view; metadata-record counts match (base + calc, asserted when `--input` is
given); 0 leftover worksheet calc collisions. A pre-existing `[Extract].[Extract]`
ref under an *untouched* datasource (e.g. a Google-Drive Glossary) is fine — just
confirm it's not in a target. (For a `.tdsx` there are no worksheets, so the
worksheet-collision check is trivially 0 — that's expected, not a gap.)

## Output & handoff

The produced file connects **live** to Snowflake (extracts are stripped; offline
`.hyper` regeneration isn't possible). The real gate is the **Tableau open test**,
which only the user can run:

1. Open the `Source Swap` `.twbx`/`.tdsx` (prompts for Snowflake OAuth sign-in). A
   `.tdsx` opens as a data source (Connect → confirm fields), not a workbook.
2. Confirm worksheets render, hierarchies + calc fields are present, visuals match.
   (A `.tdsx` has no worksheets — confirm the field list and calc fields instead.)

Always surface these two manual follow-ups to the user — they cannot be done in the
file rewrite and are easy to forget:

- **Re-extract any source that was extract-backed.** The swap leaves the swapped
  datasources as *live* Snowflake connections. For each one that was originally
  extract-backed, in Tableau do **Data → [datasource] → Extract Data… → Extract →
  Save** to restore `.hyper` performance.
- **Rename the swapped data sources.** Many Athena-era sources carry Athena-specific
  names (e.g. database/Custom-SQL artifacts) that are now misleading once they point
  at Snowflake. Remind the user to rename each swapped datasource to something
  accurate for its new Snowflake source.

Report the per-datasource binding summary and point to `RECONSTRUCTION_NOTES.md`.
Do not claim success beyond static verification — say the Tableau open test is
pending the user.

## Reference files

- `reconstructor/reconstruct.py` — the engine this skill drives (config-driven).
- `reconstructor/verify_output.py` — static verifier (config-driven; pass `--input`).
- `reconstructor/deploy_view.py` — deploy a gold view + column dump (uses connectors).
- `CLAUDE.md` — translation rules, code style, mappings, naming.
