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

**Third topology — a `.twbx` of published (`sqlproxy`) datasources + side-car
`.tdsx` files.** Some workbooks don't embed their Athena connection at all: their
data datasources are `class="sqlproxy"` *references* to published data sources on
Tableau Server, and the real Athena Custom SQL lives in a separate `.tdsx` per
published source (exported alongside the `.twbx`). Such a `.twbx` has **no Athena
SQL inside it**, so the standard swap (Phase 4) would refuse it. The **embed-collapse**
path (Phase 5) handles this: source-swap each side-car `.tdsx` normally, then embed
the swapped datasource back into the workbook in place of its `sqlproxy` reference,
producing a single self-contained Snowflake-backed `.twbx`. If you open a workbook
and Phase 1's `--emit-config` shows no SQL datasources but you were handed `.tdsx`
files too, you're in this case — jump to Phase 5.

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

**⚠ Join-key columns must stay duplicated in the gold view (do NOT deduplicate).**
When a joined datasource is flattened into one view, the column(s) the JOIN keys on
appear on *both* sides of the ON-clause and must survive as **distinct physical
columns** in the view — the workbook binds each side to its own internal field.
Build the flattened `gold_<Datasource>.sql` so every join-key column is selected
once **per relation** (e.g. `csq.RecordID`, `csq1.record_id AS RECORD_ID`); never
`SELECT DISTINCT` them away or merge them into one. This duplication is load-bearing
downstream: the **production swap** later repoints the view to a Gold table by
matching physical column names 1:1, so if the Gold table drops or merges a
join-key column that rebind breaks. `reconstruct.py` prints the join-key columns and
a "do NOT deduplicate in Gold" warning into `RECONSTRUCTION_NOTES.md` — carry that
warning forward to whoever productionizes the view.

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

**Optional back-end logic test.** Before rewriting the workbook, you can validate that
each new gold view returns the *same data* as the Athena logic it replaced — not just
that its columns line up. Invoke the **`logic_test`** skill (`/logic_test`): it runs the
original Athena SQL and the `RECONSTRUCTOR_` view and compares row counts, null rates,
aggregates, and (optionally) rows, tracing any discrepancy to its source tables. If the
deltas are large, **pause** and fix the translation before Phase 4; if they're small or
explained, **continue** — the swapped workbook reads the SANDBOX view live and just
refreshes if the view later changes (as long as its column structure holds). This gate is
optional and skippable when the Athena source is deprecated/broken (the logic test reports
that as BLOCKED).

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
- **`column_overrides`** (optional, per-datasource) — a map that pins specific
  physical column names, overriding derived `casing`. Key on either the workbook's
  original remote-name or its local field name; value is the exact gold column:
  `"column_overrides": { "record_id": "RECORD_ID_CSQ1", "[record_id]": "RECORD_ID" }`.
  Use it mainly to resolve a **collision** (below) by giving both sides distinct
  gold columns — and make sure the gold view actually exposes those names.
- **Physical-name collisions (joined datasources).** After a join-collapse, two
  source columns can map to the *same* gold physical name (e.g. two `record_id`
  columns from different relations, both → `RECORD_ID`). The single view exposes one
  column per name, so the engine resolves it: it keeps a worksheet-referenced member
  (or, on a tie, the actual join-key column) and **drops** the unreferenced one(s),
  logging the drop in the report + notes. If **two or more colliding columns are both
  worksheet-referenced**, it can't safely drop either and **stops** — add
  `column_overrides` to give them distinct gold columns (and expose both in the view).
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
given); 0 leftover worksheet calc collisions; and **0 stale `<cols>`-map relation
refs** — a join-collapsed datasource whose `<cols>` map still points at a
pre-collapse relation name (`[Custom SQL Query].[col]`) is a broken rebind and fails
here. A pre-existing `[Extract].[Extract]`
ref under an *untouched* datasource (e.g. a Google-Drive Glossary) is fine — just
confirm it's not in a target. (For a `.tdsx` there are no worksheets, so the
worksheet-collision check is trivially 0 — that's expected, not a gap.)

## Phase 5 — Embed-collapse (published `sqlproxy` `.twbx` + side-car `.tdsx`)

Use this INSTEAD of Phase 4 when the workbook's data datasources are `class="sqlproxy"`
references to published sources and you were handed the `.tdsx` for each. The engine
is `reconstructor/embed_collapse.py`; it **reuses** `reconstruct.py` per side-car
(no dialect logic is duplicated) and never touches a database itself. The result is
one self-contained `.twbx` with the data sources **embedded** and Snowflake-backed.

**How it works.** For each `sqlproxy` datasource the engine (1) source-swaps its
side-car `.tdsx` to Snowflake with the standard engine, (2) verifies the pairing
with a field fingerprint, then (3) *grafts* the swapped physical layer
(`<connection>` + `<object-graph>` + metadata-records) onto the workbook's datasource
in place, **keeping its federated `name`** (`sqlproxy.xxxxx`) so every worksheet
binding and calc alias stays valid with zero worksheet rewrites. It strips the
datasource's `<repository-location>` and drops the now-orphaned local extracts.

**Step 5a — Inventory the published datasources.**

```bash
"$PY" reconstructor/embed_collapse.py "Inputs/<workbook>.twbx" --inventory
```

This lists each `sqlproxy` datasource with its `repository_id`, calc-field ids, and
worksheet-referenced base columns — the fingerprint you use to match it to a `.tdsx`.

**Step 5b — Match each `sqlproxy` datasource to its side-car `.tdsx`.** Filenames
rarely match the datasource caption exactly but are usually fuzzy-similar; the
`repository-location/@id` is often shared verbatim between the proxy and its `.tds`
(strongest signal). Resolve the pairing by id/caption similarity and **get user
sign-off** before proceeding. You don't have to get it perfect — the engine
*verifies* every pairing with a field fingerprint (calc-id sets must match exactly;
every referenced base column must exist in the `.tdsx`) and **hard-stops** on a
mismatch, so a wrong guess fails loudly rather than silently.

**Step 5c — Translate + deploy each side-car's gold view.** Each `.tdsx` is a normal
Athena source: run Phases 1–3 on it (extract → translate per `CLAUDE.md` → deploy
`RECONSTRUCTOR_<NAME>` to `SANDBOX.B2B`). A side-car whose datasource JOINs several
Custom SQL relations is flattened into one gold view exactly as in Phase 1's
joined-datasource rules (carry the join-key-duplication warning forward).

**Step 5d — Write the config and collapse.** The config is the Phase 4 config plus a
`"tdsx"` path per datasource; `calc_bindings` is normally **empty** — in collapse
mode the calcs stay as Tableau calcs in the workbook (the logical layer is
preserved), so each gold view need only expose the **base** columns.

```json
{
  "connection": { "server": "...", "dbname": "SANDBOX", "schema": "B2B",
                  "warehouse": "B2B_S_WH", "role": "B2B_ANALYST_PRIVILEGED",
                  "authentication": "oauth" },
  "datasources": [
    { "match": "<sqlproxy datasource caption>",
      "tdsx":  "Inputs/<side-car>.tdsx",
      "view":  "RECONSTRUCTOR_<NAME>",
      "casing": "upper", "calc_bindings": [] }
  ]
}
```

```bash
"$PY" reconstructor/embed_collapse.py "Inputs/<workbook>.twbx" \
    --config "Outputs/<Workbook>/collapse_config.json" \
    -o "Outputs/<Workbook>/Collapsed/<workbook> - Collapsed.twbx"
```

**Step 5e — Statically verify.** Same engine, `--verify` mode, same config:

```bash
"$PY" reconstructor/embed_collapse.py \
    "Outputs/<Workbook>/Collapsed/<workbook> - Collapsed.twbx" \
    --config "Outputs/<Workbook>/collapse_config.json" --verify
```

It confirms, per collapsed datasource: connection class is now `federated` with a
`snowflake` inner class; **no datasource-level `<repository-location>`** and no
`[sqlproxy]` stub relation remain; relations point at the gold view; and the
**resolution invariant** — every worksheet-referenced field resolves to a
metadata-record or a kept calc. Globally: 0 `.hyper`, 0 orphaned `Data/Extracts/`,
0 `athena`. (Workbook/dashboard-level `<repository-location>` — where the *workbook*
was published — is unrelated to the data swap and intentionally left alone.) Then do
the Phase-5 → Output handoff below (the Tableau open test still applies).

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
- `reconstructor/embed_collapse.py` — Phase 5 embed-collapse engine for published
  (`sqlproxy`) `.twbx` + side-car `.tdsx`; reuses `reconstruct.py`. Modes:
  `--inventory`, build (`--config -o`), and `--verify`.
- `reconstructor/deploy_view.py` — deploy a gold view + column dump (uses connectors).
- `CLAUDE.md` — translation rules, code style, mappings, naming.
