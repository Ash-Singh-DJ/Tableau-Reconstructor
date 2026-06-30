---
name: production-swap
description: >-
  Repoint an already-Snowflake-backed Tableau .twbx (or standalone .tdsx data
  source) from one Snowflake table to another — typically swapping a SANDBOX view
  for a final production table (B2B_GOLD or another production schema). Validates
  that the production table's columns are a superset of the columns the workbook
  binds (a seamless swap), and if a column was renamed in production, rewrites the
  affected metadata-records via a supplied column map. Preserves extract mode,
  calculated fields, drill-down hierarchies, and every worksheet binding — only the
  table target and database/schema context change. Use when the user wants to point
  a Snowflake dashboard at a production/gold table, "production swap" or "promote" a
  workbook from SANDBOX, or move a workbook from a test view to its final table.
  Operates in the Tableau-Reconstructor repo. This is NOT an Athena→Snowflake
  migration — for that use tableau-source-swap.
---

# Production Swap (Snowflake → Snowflake table repoint)

Given a Tableau `.twbx` (or standalone `.tdsx` data source) that is **already
Snowflake-backed** (e.g. pointing at a `SANDBOX` view), repoint one or more of its
datasources at a final production table and produce a new file of the same type.
This is a narrow, surgical edit: the connection stays Snowflake, scaffolding is
untouched, and the only changes are the table target, the database/schema context,
and (optionally) remote-names for columns that were renamed in production.

**`.twbx` vs `.tdsx`:** identical workflow. A `.tdsx` bundles a `.tds` whose root is
a single `<datasource>` (no worksheets; identified by `formatted-name` rather than a
caption). The engine handles both — pass either path and the output keeps the
input's extension. Substitute `.tdsx` for `.twbx` in the paths below.

If the source workbook is Athena-backed, this is the wrong skill — use
`tableau-source-swap` to migrate it first, then this skill to promote it.

## Prerequisites & environment

- Python 3.9+ (`python`). Install deps once: `pip install -r requirements.txt`.
- Run scripts from the **repo root**. The engine is pure stdlib; this skill queries
  Snowflake separately through the standalone connectors package:
  `from connectors import execute_snowflake_query`.
  Credentials load from `.env` (copy `.env.example`; gitignored — never print or commit).
- Snowflake auth is browser OAuth (Okta) and is **slow** (first query opens a
  browser, 1–2 min). Run the column-discovery query in the background.
- Engine: `reconstructor/production_swap.py`. It does NOT query Snowflake itself;
  this skill queries `INFORMATION_SCHEMA` and passes the column sets to the engine.

## What to gather up front

1. Path to the source `.twbx`/`.tdsx` (already Snowflake-backed) — typically under
   `Inputs/` or a prior `Source Swap` output.
2. **For each datasource to repoint: the target production table** as fully
   qualified `DB.SCHEMA.TABLE` (e.g. `B2B_GOLD.HIGH_VALUE_CUSTOMER.PROFILER_FULL_GOLD`).
   Only datasources the user names are swapped; the rest are left untouched.
3. Whether any columns were **renamed** between the current table and the production
   table. If so, get the `old_name → new_name` mapping. (If unsure, Phase 2's
   column comparison surfaces the candidates.)

## Phase 1 — Inventory the workbook

List the Snowflake datasources, their current table targets, and the columns each
one binds:

```bash
PY=python

"$PY" reconstructor/production_swap.py "Inputs/<workbook>.twbx" --list
```

`--list` shows every Snowflake-backed datasource with its `current table` and bound
columns (base + any materialized calc columns, which are now physical). Present this
and confirm with the user which datasource(s) map to which production table. (Pass a
`.tdsx` here exactly the same way; it lists its single datasource.)

`--emit-config` prints a config skeleton (each datasource pre-filled with its
current table and bound columns as `_`-prefixed hints) to fill in. The `match` value
is the datasource caption for a `.twbx`, or the `formatted-name` for a `.tdsx` — the
skeleton pre-fills whichever applies.

## Phase 2 — Discover production columns & compare (the validation gate)

For each target production table, query its actual columns. Run in the background
(OAuth latency):

```python
from connectors import execute_snowflake_query
df = execute_snowflake_query("""
    SELECT column_name, data_type, ordinal_position
    FROM <DB>.INFORMATION_SCHEMA.COLUMNS
    WHERE table_schema = '<SCHEMA>' AND table_name = '<TABLE>'
    ORDER BY ordinal_position
""")
```

The seamless-swap **contract**: the production table must be a **superset** of the
columns the workbook binds (`--list` output). Extra production columns are fine.

- If every bound column exists in the target → seamless swap, no column map needed.
- If a bound column is **missing**, decide why:
  - It was **renamed** in production → add it to that datasource's `column_map`
    (`"OLD": "NEW"`). The engine rewrites the metadata-record's remote-name/alias to
    the prod name while keeping the workbook's internal `local-name` (so worksheet
    field references stay valid).
  - It is **genuinely absent** → stop and report. Do not swap; the workbook field
    would break. Surface the missing column(s) to the user.

Type differences are reported, not blocking — Tableau reconciles `remote-type` on
refresh. Note any (e.g. `VARCHAR → NUMBER`) in the handoff so the user can sanity-check.

## Phase 3 — Swap

Write the config and a target-columns JSON (the prod columns from Phase 2, keyed by
the same `match` value), then run the engine with validation enabled.

`config.json`:
```json
{
  "connection": {},
  "datasources": [
    { "match": "<datasource caption>",
      "target": "B2B_GOLD.HIGH_VALUE_CUSTOMER.<TABLE>",
      "column_map": { "OLD_NAME": "NEW_NAME" } }
  ]
}
```

- `connection` — usually `{}` (same Snowflake account; warehouse/role/server kept
  from the workbook). Override `warehouse`/`role`/`server` only if production lives
  on a different account.
- `column_map` — omit or `{}` when names match exactly; only list renamed columns.
- Datasources omitted from `datasources` are left untouched.

`target_columns.json` (from Phase 2 — enables the validation gate):
```json
{ "<datasource caption>": { "PRIMARY_NAME": "TEXT", "DUNS_NUMBER": "NUMBER" } }
```

Run it:
```bash
"$PY" reconstructor/production_swap.py "Inputs/<workbook>.twbx" \
    --config "Outputs/<Workbook>/production_config.json" \
    --target-columns "Outputs/<Workbook>/target_columns.json" \
    -o "Outputs/<Workbook>/Production Swap/<workbook> - Production Swap.twbx" \
    --notes "Outputs/<Workbook>/Production Swap/PRODUCTION_SWAP_NOTES.md"
```

The engine refuses (non-zero exit) if any bound column is missing from the target
after applying `column_map` — that's the gate working. Resolve the mapping or fix
the table and re-run; never force a swap past a missing column.

## Phase 4 — Verify

Statically confirm the output:
- Each target datasource's relation table now reads `[DB].[SCHEMA].[TABLE]` (prod),
  and the inner connection `dbname`/`schema` match — both the connection-level
  relation and its object-graph duplicate (the report's `relations repointed` should
  be ≥ 2 per datasource).
- Untouched datasources are byte-identical to the input.
- Extract entries (`.hyper`) preserved if the input had them; drill-paths unchanged.
- Any `column_map` rewrote `remote-name`/`remote-alias` only — `local-name` unchanged.

## Output & handoff

The real gate is the **Tableau open test**, which only the user can run:

1. Open the `Production Swap` `.twbx`/`.tdsx` (prompts for Snowflake OAuth sign-in).
   A `.tdsx` opens as a data source, not a workbook.
2. Confirm worksheets render with no "field is missing" warnings; hierarchies and
   calc fields intact; visuals match the pre-swap workbook. (A `.tdsx` has no
   worksheets — confirm the field list and calc fields instead.)

Always surface this manual follow-up — it can't be done in the file rewrite:

- **Refresh the extract if the source is extract-backed.** Production swap
  preserves extract mode, so an extract still holds the *old* SANDBOX data until
  refreshed. In Tableau: **Data → [datasource] → Extract Data… → Extract → Save**
  (or Refresh) to repopulate from the production table.

Report the per-datasource swap summary and point to `PRODUCTION_SWAP_NOTES.md`. Do
not claim success beyond static verification — the Tableau open test is pending the
user.

## Reference files

- `reconstructor/production_swap.py` — the engine this skill drives. `--list`,
  `--emit-config`, `--config`, `--target-columns`, `--notes`.
- `reconstructor/reconstruct.py` — the *Athena→Snowflake* engine (`tableau-source-swap`).
  Different operation; don't use it for a Snowflake→Snowflake repoint.
- `CLAUDE.md` — repo conventions.
