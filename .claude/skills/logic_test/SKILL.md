---
name: logic_test
description: >-
  Back-end logic equivalence testing for a Tableau source swap. Runs the ORIGINAL
  Athena (Presto) logic extracted from a workbook (.twbx) or data source (.tdsx)
  AND the Snowflake `RECONSTRUCTOR_` view built to replace it, then compares the
  results — row counts, schema/column reconciliation, per-column null rates, numeric
  aggregates (sum/min/max), distinct key counts, a data sample, and optionally a
  row-level reconciliation. Produces a test-outcome report and, when a discrepancy
  is found, traces it to the underlying source tables (via table_mappings.csv) to
  localize where the divergence enters. Use after Phase 2/3 of a tableau-source-swap
  to decide whether to pause or continue, or standalone to validate a view another
  team member already reconstructed. Does not fix discrepancies unless there is an
  obvious translation issue or the user asks. Operates in the Tableau-Reconstructor
  repo.
---

# Logic Test (Athena original vs Snowflake `RECONSTRUCTOR_` view)

Prove that a reconstructed Snowflake view returns the **same data** as the Athena
Custom SQL it replaced. The source-swap engine (`reconstruct.py`) only guarantees the
workbook *rewrite* is structurally sound (connections, metadata-records, bindings);
this skill validates the **translated SQL logic** by running both sides and comparing.
It is the back-end counterpart to the front-end "Tableau open test".

It **reports** outcomes and traces discrepancies to their source tables. It does
**not** auto-fix — except an obvious translation bug (e.g. a `cycle_date` that should
be `LAST_MODIFIED_DATETIME`, a leftover `reporting_suite` filter) or a change the user
explicitly requests.

## Two ways to invoke

1. **After Phase 2/3 of a `tableau-source-swap`** (optional gate). You've translated
   the SQL and deployed the `RECONSTRUCTOR_` view; the Athena SQL is already extracted
   under `Outputs/<Workbook>/`. Run this before or instead of the Phase-4 rewrite to
   decide: if discrepancies are large, **pause the swap** and fix the translation; if
   small/explained, **continue to the front-end rewrite** (the swapped workbook reads
   the SANDBOX view live and simply refreshes if the view later changes, as long as its
   column structure holds).

2. **Standalone** (another team member already did a first-pass reconstruct). The user
   must have:
   - the **Athena-backed** `.twbx`/`.tdsx` staged in `Inputs/` (we extract the original
     logic from it here), and
   - the **`RECONSTRUCTOR_` view already published** in `SANDBOX` (we read it as-is).

   In this mode you run Phase 1's extraction yourself, then the comparison.

## Prerequisites & environment

- Python 3.9+. Install deps once: `pip install -r requirements.txt` (this skill needs
  the DB connectors — pandas, pyathena, snowflake-connector).
- Run scripts from the **repo root**. `reconstructor/logic_test.py` is the only
  logic-test code that queries a DB; it imports the standalone connectors package:
  `from connectors import execute_athena_query, execute_snowflake_query`.
  Credentials load from `.env` (copy `.env.example`; gitignored — never print or commit).
- **Both platforms are queried.** Athena runs the extracted Presto SQL; Snowflake reads
  the view. Snowflake auth is browser OAuth (Okta) and **slow** (first query opens a
  browser, 1–2 min); the engine batches all metrics into **one** aggregate query per
  platform to minimize round trips. Still, run it in the background.
- Translation rules, `table_mappings.csv`, `omniture_mappings.csv`, and naming
  conventions live in `CLAUDE.md`. The trace step reads `table_mappings.csv` directly.

## What to gather up front

1. The `RECONSTRUCTOR_` view FQN(s) in SANDBOX (e.g. `SANDBOX.B2B.RECONSTRUCTOR_<NAME>`).
2. The extracted Athena SQL for each datasource — `Outputs/<Workbook>/<Datasource>.sql`
   (single relation) or the flattened join query (a joined datasource is one view).
   In standalone mode, produce these in Phase 1.
3. The reconstruct config JSON if one exists (`--config`) — it lets the engine tag
   **materialized calc columns** (which exist in the view but not in the Athena SQL, so
   they aren't false "missing" flags) and honor any `column_overrides` renames.
4. A **key column** per datasource if known (`--key`), for distinct-count and (with
   `--deep`) row-level reconciliation. For a join-collapsed view, the join key is a
   natural choice.

---

## Phase 1 — Extract the Athena logic (standalone mode; skip if you just ran a source-swap)

Pull the original Custom SQL. The flattened variant is what maps 1:1 to a single view:

```bash
PY="C:/Users/singhaishwarya/.venv/Scripts/python.exe"   # the repo venv; bare python won't work

"$PY" reconstructor/extract_custom_sql.py "Inputs/<workbook>.twbx"           # base SQL per relation
"$PY" reconstructor/extract_custom_sql_advanced.py "Inputs/<workbook>.twbx"  # flattened join per datasource
```

Use the base `<Datasource>.sql` for a single-relation datasource, or the flattened
query (from the advanced extractor) for a joined one — it must match the SQL the
`RECONSTRUCTOR_` view was built from. If the source SQL includes Athena-only syntax that
was translated for Snowflake (e.g. `cycle_date`, `DATE_PARSE`), that's fine — the Athena
side runs the ORIGINAL Presto SQL as-is; only the Snowflake side runs the translated view.

## Phase 2 — Run the comparison

```bash
"$PY" reconstructor/logic_test.py \
    --athena-sql "Outputs/<Workbook>/<Datasource>.sql" \
    --sf-fqn SANDBOX.B2B.RECONSTRUCTOR_<NAME> \
    --config "Outputs/<Workbook>/reconstruct_config.json" \
    --match "<datasource caption>" \
    --key <KEY_COLUMN> \
    --label "<Datasource>" \
    --out-json "Outputs/<Workbook>/Logic Test/<Datasource>.json" \
    --out-md   "Outputs/<Workbook>/Logic Test/<Datasource>.md"
```

Run in the background (OAuth latency). Repeat per datasource/view.

**Default battery** (one aggregate query per platform, plus a sample):
- **Row count** + delta% → PASS `<1%` / CONDITIONAL `1–5%` / INVESTIGATE `>5%`
  (threshold tunable via `--delta-threshold`).
- **Schema / column reconciliation** — Athena output columns matched to the view's
  columns by normalized name (casefold + space/hyphen→underscore, so an UPPERCASE gold
  column matches a lowercase field). Reports Athena-only (missing), Snowflake-only
  (extra), and Snowflake calc-only (expected materialized calcs, from `--config`).
- **Per-column null rate** on both platforms; flags a null-rate gap wider than the
  row-count delta explains (possible data loss / masking).
- **Numeric sum/min/max** per numeric column; flags a SUM divergence beyond threshold.
- **Distinct count** on each `--key`.
- A **5-row sample** from each platform (inspect for encoding/truncation/masking).

**`--deep`** adds a **row-level reconciliation**: full pull of both result sets, aligned
on the matched columns and diffed on `--key` (rows only-in-Athena, only-in-Snowflake, and
same-key-but-changed). Guarded by `--deep-max-rows` (default 500000) — if either side is
larger, it skips with a note rather than pulling a huge result. Use it on smaller
dimension-like views or when aggregates disagree and you need the offending rows.

**`--sf-sql PATH`** (instead of `--sf-fqn`) compares against a not-yet-deployed view's
SELECT, wrapped as a subquery. No `INFORMATION_SCHEMA` types, so numeric detection falls
back to the sample.

## Phase 3 — Interpret & (auto-)trace discrepancies

Read the report / JSON. The engine assigns an **overall status**:
- **PASS** — no MEDIUM/HIGH issues; logic agrees within thresholds.
- **CONDITIONAL** — MEDIUM issues (1–5% row delta, a null-rate gap, extra columns). Often
  explainable (a history-table dedup, a known narrower Snowflake date range — see notes in
  `table_mappings.csv`). Document the reason.
- **INVESTIGATE** — HIGH issues (>5% row delta, a SUM mismatch, row-level diffs). Dig in.
- **BLOCKED** — the Athena source couldn't be queried (see below).

When status is CONDITIONAL/INVESTIGATE, the engine **auto-traces**: it parses the source
tables out of the Athena SQL, maps each to Snowflake via `table_mappings.csv` (handling
the omniture 1:many `reporting_suite` split), and row-count-probes each on **both**
platforms. Use the "Source-Table Trace" section to localize the divergence — e.g. a table
whose Athena vs Snowflake counts already differ is the likely origin, versus a divergence
introduced by a JOIN or a translated predicate. `--no-trace` disables it.

**Common, usually-benign causes** (confirm, then document — don't "fix"):
- History-table **dedup** — Snowflake keeps 1 row/entity/date where Athena had intraday
  snapshots (systematic row-count delta).
- **Narrower Snowflake date range** for some silver tables (noted in `table_mappings.csv`).
- **PII masking** in Snowflake samples — note it, not a defect.

**Fix only** an obvious translation bug (surface it, then correct the gold SQL and
redeploy via `deploy_view.py`, then re-run this test) or a change the user requests:
- `cycle_date` mapped to `source_file_partition_date` instead of `LAST_MODIFIED_DATETIME`
  (15–28% vs 0.2–0.7% divergence — see `CLAUDE.md`).
- A leftover `reporting_suite` predicate on an omniture view that's already suite-scoped.
- A missing array-handling component (NULL-polluted arrays / failed `ARRAY_SIZE`).

## Phase 4 — Decision (source-swap mode) / handoff

- **After a source-swap:** if the view PASSes or CONDITIONAL-with-documented-reason,
  **continue** to the Phase-4 workbook rewrite. If INVESTIGATE, **pause** — fix the
  translation and re-run before rewriting. (The user makes the call; present the report.)
- **Standalone:** deliver the per-datasource report(s) and the overall verdict so the
  team member who built the view can act.

Point the user at the `Logic Test/` reports. State plainly what PASSed, what's
CONDITIONAL and why, and what needs investigation. Do **not** claim logic equivalence
beyond what the comparison covers (aggregates + optional row-level diff on the key).

## When comparison isn't possible (BLOCKED)

Some Athena sources are **deprecated or broken** — you only find out by trying. If the
Athena SQL errors, the engine returns **BLOCKED**: it records the Athena error, still
profiles the Snowflake view (row count + columns) so the run isn't wasted, and exits
non-zero. In that case the back-end test can't validate this datasource — say so, and
fall back to the front-end Tableau open test for it. Don't treat BLOCKED as a failure of
the reconstruction.

## Reference files

- `reconstructor/logic_test.py` — the comparison engine this skill drives (uses connectors).
- `reconstructor/extract_custom_sql.py` / `extract_custom_sql_advanced.py` — extract the
  Athena logic (base / flattened-join).
- `reconstructor/deploy_view.py` — redeploy a gold view after an obvious-translation fix.
- `table_mappings.csv` / `omniture_mappings.csv` — source-table + column mappings (trace).
- `CLAUDE.md` — translation rules, history-table `cycle_date` rule, omniture conventions.
