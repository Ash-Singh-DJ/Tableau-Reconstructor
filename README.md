# Tableau-Reconstructor

Migrate Tableau workbooks from **AWS Athena (Presto SQL)** to **Snowflake (ANSI
SQL)**, and promote Snowflake-backed workbooks from test views to production tables
— preserving every caption, drill-down hierarchy, calculated field, and worksheet
binding.

The repo ships three Claude **skills** that drive the work end-to-end:

| Skill | Direction | Use it to… |
|---|---|---|
| **`tableau-source-swap`** | Athena → Snowflake | Convert an Athena-backed dashboard (or data source) to Snowflake. |
| **`production-swap`** | Snowflake → Snowflake | Repoint a Snowflake dashboard from a test view to its final production table. |
| **`logic_test`** | Athena vs Snowflake | Verify a converted Snowflake view returns the *same data* as the Athena logic it replaced. |

You run these by asking Claude (in this repo) to "source-swap", "production-swap", or
"logic-test" a workbook; the skill handles extraction, translation, deployment,
comparison, and the workbook rewrite. Setup and the human steps around each skill are
below.

## Setup (once)

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in AWS + Snowflake values
```

Snowflake auth is browser OAuth (Okta) — the first query of a session opens a
browser to sign in.

Create an 'Inputs' and 'Outputs' folder in your cloned directory; these are by default gitignored.

---

## Workflow: `tableau-source-swap` (Athena → Snowflake)

The source swap handles **three input shapes**. They share the same
translate-and-deploy core and the same follow-up steps — they differ only in *what
you download and stage* before running the skill. Figure out which one you have, stage
accordingly, then run the skill and follow the shared steps below.

### Which shape do I have?

| Your situation | Path | What to procure |
|---|---|---|
| A workbook whose data sources **embed** their own Athena connection | **A. Embedded-source workbook** | just the `.twbx` |
| A single **standalone data source** (no dashboard), Athena-backed | **B. Standalone data source** | just the `.tdsx` |
| A workbook whose data sources are **published/linked** (they live on Tableau Server, not inside the workbook) | **C. External-source embed-collapse** | the `.twbx` **and** the `.tdsx` for **each** published data source |

Not sure whether you're in A or C? Stage the `.twbx` and ask Claude to source-swap
it — if the workbook holds no Athena SQL of its own (the data sources are published
references), the skill will tell you it needs the side-car `.tdsx` files, i.e. you're
in path C.

### A. Embedded-source workbook (`.twbx`)

1. **Download** the workbook from Tableau Cloud and **stage** it in `Inputs/`.
2. **Run the skill** — ask Claude to source-swap the workbook. It extracts each
   datasource's Custom SQL, translates it to Snowflake, deploys the gold view(s) to
   `SANDBOX`, and writes a Snowflake-backed `.twbx` under
   `Outputs/<Workbook>/Source Swap/`.

### B. Standalone data source (`.tdsx`)

1. **Download/export** the data source and **stage** the `.tdsx` in `Inputs/`.
2. **Run the skill** — same command; the output keeps the `.tdsx` extension. There are
   no worksheets to check, so after opening you confirm the field list and calculated
   fields instead.

### C. External-source embed-collapse (`.twbx` + one `.tdsx` per published source)

Some workbooks don't contain their Athena SQL at all — their data sources are
*references* to data sources published separately on Tableau Server. The real SQL
lives in those published sources, which you download as `.tdsx` files.

1. **Download the workbook** (`.twbx`) **and** the **`.tdsx` for every published data
   source it uses**, then **stage them all together** in `Inputs/`. Getting all the
   side-cars matters — a missing one can't be collapsed.
2. **Run the skill** — ask Claude to source-swap (collapse) the workbook. Claude
   matches each published data source to its `.tdsx` (it will confirm the pairing with
   you), source-swaps each side-car to Snowflake, then **embeds** the swapped sources
   back into the workbook in place of the server references. The result is a single
   self-contained Snowflake-backed `.twbx` under `Outputs/<Workbook>/Collapsed/` —
   with no external/published dependencies.

### Shared follow-up steps (all three paths)

3. **Open the output** in Tableau Desktop (sign in to Snowflake when prompted) and
   confirm the worksheets render (or, for a standalone `.tdsx`, the field list).
4. **Reset the now-live data sources to be extract-backed.** The swap leaves swapped
   sources as *live* Snowflake connections; for each one, in Tableau do
   **Data → [datasource] → Extract Data… → Extract → Save**.
5. **Rename any Custom SQL Athena data sources** to a new, appropriate name — the
   old names describe the retired Athena source and are now misleading.
6. **Review the SANDBOX-based logic** to ensure every column is equivalent to its
   Athena origin — the translated SQL faithfully reproduces the Athena query's columns
   and values. The **`logic_test`** skill (below) automates this comparison.

---

## Workflow: `production-swap` (Snowflake → Snowflake)

Use this **after** a workbook is already Snowflake-backed (e.g. the output of a
source swap) to repoint it from the `SANDBOX` view to its final `B2B_GOLD`
production table.

1. **Stage** the Snowflake-backed workbook in `Inputs/` (or use a prior Source Swap
   output).
2. **Run the skill** — ask Claude to production-swap it, naming the target
   `DB.SCHEMA.TABLE`. It validates columns, repoints the datasource(s), and writes
   the result under `Outputs/<Workbook>/Production Swap/`.
3. **Open and verify** in Tableau, then **refresh the extract** so it pulls from the
   production table instead of the old SANDBOX view.

> **Column requirement.** The production gold table must contain **all** of the
> columns the SANDBOX view exposed. **Extra** columns in the production table are
> fine. **Missing** columns are not — the skill will refuse the swap by default,
> because a missing column would silently break a workbook field binding.
>
> If a column is genuinely missing (or was renamed without a clean mapping), an Opus
> model can still perform the required custom XML surgery on the workbook outside the
> scope of the `production-swap` skill. Be aware this is time-consuming and
> introduces technical risk — prefer fixing the production table so its columns are a
> superset of the view's.

---

## Workflow: `logic_test` (back-end verification)

A source swap guarantees the workbook *rewrite* is structurally sound — connections,
field bindings, and calculated fields all line up. It does **not** by itself prove the
translated SQL returns the **same data** as the Athena query it replaced. `logic_test`
is that proof: it runs the **original Athena logic** and the new Snowflake
`RECONSTRUCTOR_` view side by side and compares them — row counts, columns, per-column
null rates, numeric aggregates (sum/min/max), distinct key counts, and a data sample
(optionally a full row-level reconciliation). When something diverges, it traces the
discrepancy back to the underlying source tables to localize where it entered.

It's the back-end counterpart to the front-end "open the workbook in Tableau" test.
It **reports** — it doesn't silently rewrite your view — though it will flag an obvious
translation bug and can fix it if you ask.

**Two ways to use it:**

- **As a gate during a source swap.** After the gold view is deployed but before (or
  instead of) trusting the rewrite, ask Claude to logic-test it. If the deltas are
  large, pause and fix the translation; if they're small or explained, continue.
- **Standalone, against a view someone already built.** Stage the original
  Athena-backed `.twbx`/`.tdsx` in `Inputs/` and make sure the `RECONSTRUCTOR_` view is
  already published in `SANDBOX`. Then ask Claude to logic-test it — the original logic
  is extracted from the staged file and compared against the live view.

**What you need:** both platforms are queried, so this skill needs working AWS
(Athena) **and** Snowflake credentials in `.env` (unlike the swap engines, which never
touch a database). The first Snowflake query of a session opens a browser to sign in.

> **When the Athena source is gone.** If the original Athena table/query has been
> deprecated or is broken, there's nothing to compare against — the logic test reports
> this as **BLOCKED** rather than pass/fail. That's expected; skip the gate and rely on
> the front-end open test.

---

## Repo structure

```
Tableau-Reconstructor/
├── connectors/         # Athena + Snowflake query helpers (the DB-dependent code)
├── reconstructor/      # the swap engines + extraction/verification tools
├── .claude/skills/     # the three skills: tableau-source-swap, production-swap, logic_test
├── table_mappings.csv  # approved Athena ↔ Snowflake table mappings (shared)
├── Inputs/             # source .twbx workbooks / .tdsx data sources (gitignored)
├── Outputs/            # per-workbook outputs: gold SQL, swapped .twbx/.tdsx, notes
├── CLAUDE.md           # translation rules, SQL style, mappings, conventions
├── requirements.txt
└── .env.example        # credential template (copy to .env; never commit .env)
```

`CLAUDE.md` holds the Presto→Snowflake translation reference (dialect rules, array
handling, the history-table `cycle_date` rule), the SQL code style, and the
table-mapping workflow that the skills rely on. Tableau workbook files (`.twb`,
`.twbx`, `.twbr`) and `.env` are gitignored and never staged.
