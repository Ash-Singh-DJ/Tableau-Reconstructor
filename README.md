# Tableau-Reconstructor

Migrate Tableau workbooks from **AWS Athena (Presto SQL)** to **Snowflake (ANSI
SQL)**, and promote Snowflake-backed workbooks from test views to production tables
— preserving every caption, drill-down hierarchy, calculated field, and worksheet
binding.

The repo ships two Claude **skills** that drive the work end-to-end:

| Skill | Direction | Use it to… |
|---|---|---|
| **`tableau-source-swap`** | Athena → Snowflake | Convert an Athena-backed dashboard to Snowflake. |
| **`production-swap`** | Snowflake → Snowflake | Repoint a Snowflake dashboard from a test view to its final production table. |

You run these by asking Claude (in this repo) to "source-swap" or "production-swap"
a workbook; the skill handles extraction, translation, deployment, and the workbook
rewrite. Setup and the human steps around each skill are below.

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

1. **Download** the workbook from Tableau Cloud.
2. **Stage** it in `Inputs/`.
3. **Run the skill** — ask Claude to source-swap the workbook. It extracts each
   datasource's Custom SQL, translates it to Snowflake, deploys the gold view(s) to
   `SANDBOX`, and writes a Snowflake-backed `.twbx` under `Outputs/<Workbook>/Source Swap/`.
4. **Open the output workbook** in Tableau Desktop (sign in to Snowflake when
   prompted) and confirm the worksheets render.
5. **Reset the now-live data sources to be extract-backed.** The swap leaves swapped
   sources as *live* Snowflake connections; for each one, in Tableau do
   **Data → [datasource] → Extract Data… → Extract → Save**.
6. **Rename any Custom SQL Athena data sources** to a new, appropriate name — the
   old names describe the retired Athena source and are now misleading.
7. **Review the SANDBOX-based logic** to ensure every column is equivalent to its
   Athena origin (i.e. the translated SQL faithfully reproduces the Athena query's
   columns and values).

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

## Repo structure

```
Tableau-Reconstructor/
├── connectors/         # Athena + Snowflake query helpers (the DB-dependent code)
├── reconstructor/      # the swap engines + extraction/verification tools
├── .claude/skills/     # the two skills: tableau-source-swap, production-swap
├── table_mappings.csv  # approved Athena ↔ Snowflake table mappings (shared)
├── Inputs/             # source .twbx workbooks (gitignored)
├── Outputs/            # per-workbook outputs: gold SQL, swapped .twbx, notes
├── CLAUDE.md           # translation rules, SQL style, mappings, conventions
├── requirements.txt
└── .env.example        # credential template (copy to .env; never commit .env)
```

`CLAUDE.md` holds the Presto→Snowflake translation reference (dialect rules, array
handling, the history-table `cycle_date` rule), the SQL code style, and the
table-mapping workflow that the skills rely on. Tableau workbook files (`.twb`,
`.twbx`, `.twbr`) and `.env` are gitignored and never staged.
