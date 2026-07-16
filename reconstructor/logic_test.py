"""
logic_test.py -- back-end logic equivalence testing for a source swap.

Compares the ORIGINAL Athena (Presto) logic extracted from a Tableau workbook /
data source against the SANDBOX Snowflake `RECONSTRUCTOR_` view that was built to
replace it, and reports whether the two produce the same data. It is the back-end
counterpart to the front-end "Tableau open test": the source-swap engine
(reconstruct.py) only guarantees the *rewrite* is structurally sound; this helper
checks that the *translated SQL* actually returns the same numbers.

It runs BOTH sets of logic and compares the results:
  - Athena side  -- runs the extracted Custom SQL (`--athena-sql`, the raw Presto
                    query, single relation or the flattened join) directly on Athena.
  - Snowflake side -- reads the deployed gold view (`--sf-fqn`), or a not-yet-deployed
                    view's SELECT (`--sf-sql`), on Snowflake.

Comparison battery (per datasource/view):
  - row count + delta%                    (PASS <1% / CONDITIONAL 1-5% / INVESTIGATE >5%)
  - schema / column reconciliation        (matched by normalized name; Athena-only /
                                           Snowflake-only columns; known materialized
                                           calc columns are expected on the SF side)
  - per-column null rate, both platforms
  - numeric per-column sum / min / max
  - distinct count on key columns (--key)
  - a 5-row data sample from each platform
  - (--deep) row-level reconciliation on --key: full pull + key diff in pandas
             (added / removed / changed rows), capped by --deep-max-rows

Discrepancy tracing: when a check fails, the source tables are parsed out of the
Athena SQL, mapped to Snowflake via `table_mappings.csv`, and each is row-count /
null probed on BOTH platforms so the divergence can be localized to the underlying
table(s) / join it enters at. It reports; it does not fix (obvious translation fixes
or user-requested edits are made by the driving skill, not here).

This is a query helper, so -- like deploy_view.py -- it is the ONLY logic-test code
that talks to a database, and it does so through the standalone `connectors`
package. The swap engines themselves never touch a DB.

Usage:
    python logic_test.py \
        --athena-sql "Outputs/<WB>/<Datasource>.sql" \
        --sf-fqn SANDBOX.B2B.RECONSTRUCTOR_<NAME> \
        [--config "Outputs/<WB>/reconstruct_config.json" --match "<caption>"] \
        [--key COL [--key COL2 ...]] \
        [--deep [--deep-max-rows 500000]] \
        [--delta-threshold 1.0] \
        [--label "<Datasource>"] \
        [--out-json PATH] [--out-md PATH] \
        [--no-trace]

Exit code 0 if the comparison ran and PASSED/CONDITIONAL, 1 if it INVESTIGATE-failed
or could not run (e.g. a deprecated/broken Athena source -- see BLOCKED below).
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from connectors import execute_athena_query, execute_snowflake_query

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


# ── name normalization / identifier quoting ──────────────────────────────────
def norm(name):
    """Normalize a column name for cross-platform matching: casefold, trim, and
    fold spaces/hyphens to underscores. Mirrors the source-swap `casing` decoupling
    (a gold view usually UPPERCASEs the workbook's lowercase field names)."""
    return (name or '').strip().lower().replace(' ', '_').replace('-', '_')


def q(name):
    """Double-quote an identifier (identifier syntax is shared by Presto & Snowflake).
    Embedded double quotes are doubled per SQL rules."""
    return '"' + str(name).replace('"', '""') + '"'


def normalize_athena_sql(sql):
    """Undo the entity-encoding artifact in Custom SQL extracted from a `.twb`.

    Tableau stores comparison operators in the .twb with each angle bracket DOUBLED
    (`<` -> `<<`, `>` -> `>>`), so the extracted Presto SQL comes out with `<<=`,
    `>>=`, `<<>>`, and bare `<<`/`>>` where the real query has `<=`, `>=`, `<>`, `<`,
    `>` (see the `>>` note in CLAUDE.md). Sending that to Athena as-is is a syntax
    error -> a FALSE "BLOCKED" verdict. Collapsing every doubled bracket back to a
    single one fixes all five forms at once. Presto has no bit-shift operators, so
    there is no legitimate `<<`/`>>` to protect."""
    return sql.replace('<<', '<').replace('>>', '>')


_NUMERIC_SF_TYPES = ('NUMBER', 'DECIMAL', 'NUMERIC', 'INT', 'INTEGER', 'BIGINT',
                     'SMALLINT', 'TINYINT', 'BYTEINT', 'FLOAT', 'DOUBLE', 'REAL')


def _is_numeric_sf_type(data_type):
    return any(t in (data_type or '').upper() for t in _NUMERIC_SF_TYPES)


# ── config: which SF columns are materialized calcs / explicitly renamed ──────
def load_expected_calc_and_overrides(config_path, match):
    """From the reconstruct config, return (calc_sf_cols, override_pairs) for the
    matched datasource:
      - calc_sf_cols  -- SF physical columns that are MATERIALIZED calc fields. They
        exist in the gold view but NOT in the raw Athena SQL, so they must not be
        flagged as "missing on Athena".
      - override_pairs -- {original_remote_name -> sf_physical} from column_overrides,
        used as name aliases before normalized matching.
    Both empty if no config / no match. Reads the config directly (no DB / no
    workbook needed) -- calc gold columns are `calc_bindings[i][0]`."""
    if not config_path:
        return set(), {}
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    dss = config.get('datasources', [])
    ds_cfg = None
    if match:
        for d in dss:
            if match in (d.get('match'), d.get('name')):
                ds_cfg = d
                break
    elif len(dss) == 1:
        ds_cfg = dss[0]
    if ds_cfg is None:
        return set(), {}
    calc_cols = {entry[0] for entry in ds_cfg.get('calc_bindings', []) if entry}
    overrides = dict(ds_cfg.get('column_overrides', {}) or {})
    return calc_cols, overrides


# ── platform column discovery ─────────────────────────────────────────────────
def athena_sample(athena_sql, limit=5):
    """Run the extracted Athena SQL wrapped in a LIMIT so we get its output columns
    and a small data sample cheaply. Returns a pandas DataFrame. Raises on error
    (a deprecated/broken source surfaces here -> BLOCKED comparison)."""
    wrapped = f"SELECT * FROM (\n{athena_sql}\n) _lt_sample LIMIT {int(limit)}"
    return execute_athena_query(wrapped)


def snowflake_columns(fqn):
    """Return an ordered list of (column_name, data_type) for a Snowflake table/view
    from INFORMATION_SCHEMA (authoritative types, no data scan)."""
    db, schema, table = fqn.split('.')
    df = execute_snowflake_query(f"""
        SELECT column_name, data_type, ordinal_position
        FROM {db}.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema = '{schema}' AND table_name = '{table}'
        ORDER BY ordinal_position
    """)
    # pandas may lowercase headers depending on driver; access positionally.
    return [(row[0], row[1]) for row in df.itertuples(index=False)]


def snowflake_sample(sf_from, limit=5):
    return execute_snowflake_query(f"SELECT * FROM {sf_from} LIMIT {int(limit)}")


# ── aggregate battery (one query per platform) ────────────────────────────────
def build_aggregate_sql(from_clause, columns, numeric_cols, key_cols):
    """Build ONE aggregate query over `from_clause` (a table name or `(<sql>) t`).

    Batching every metric into a single SELECT keeps round trips (and Snowflake
    OAuth latency) to one per platform. Returns (sql, alias_map) where alias_map is
    {alias -> (column, metric)} for reading the single result row back.

    Metrics: COUNT(*) as `cnt`; per column a null count `n{i}`; per numeric column
    SUM/MIN/MAX `s{i}`/`mn{i}`/`mx{i}`; per key column COUNT(DISTINCT) `d{i}`.
    """
    numeric = {norm(c) for c in numeric_cols}
    keys = {norm(c) for c in key_cols}
    parts = ['COUNT(*) AS cnt']
    alias_map = {'cnt': (None, 'row_count')}
    for i, c in enumerate(columns):
        parts.append(f'SUM(CASE WHEN {q(c)} IS NULL THEN 1 ELSE 0 END) AS n{i}')
        alias_map[f'n{i}'] = (c, 'nulls')
        if norm(c) in numeric:
            parts.append(f'SUM({q(c)}) AS s{i}')
            parts.append(f'MIN({q(c)}) AS mn{i}')
            parts.append(f'MAX({q(c)}) AS mx{i}')
            alias_map[f's{i}'] = (c, 'sum')
            alias_map[f'mn{i}'] = (c, 'min')
            alias_map[f'mx{i}'] = (c, 'max')
        if norm(c) in keys:
            parts.append(f'COUNT(DISTINCT {q(c)}) AS d{i}')
            alias_map[f'd{i}'] = (c, 'distinct')
    sql = 'SELECT\n\t' + '\n\t, '.join(parts) + f'\nFROM {from_clause}'
    return sql, alias_map


def _row_to_dict(df):
    """First result row as {lowercased_alias -> value} (drivers vary on header case)."""
    if df is None or len(df) == 0:
        return {}
    row = df.iloc[0]
    return {str(k).lower(): v for k, v in row.items()}


def run_aggregates(runner, from_clause, columns, numeric_cols, key_cols):
    """Run the aggregate battery via `runner` (execute_*_query). Returns a nested
    dict: {'row_count': N, 'columns': {col: {'nulls':.., 'sum':.., ...}}}."""
    sql, alias_map = build_aggregate_sql(from_clause, columns, numeric_cols, key_cols)
    df = runner(sql)
    row = _row_to_dict(df)
    out = {'row_count': None, 'columns': {}}
    for alias, (col, metric) in alias_map.items():
        val = row.get(alias.lower())
        if metric == 'row_count':
            out['row_count'] = None if val is None else int(val)
        else:
            out['columns'].setdefault(col, {})[metric] = _num(val)
    return out


def _num(v):
    """Coerce a pandas/py scalar to a JSON-friendly number/str/None."""
    if v is None:
        return None
    try:
        import math
        f = float(v)
        if math.isnan(f):
            return None
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return str(v)


# ── column reconciliation ─────────────────────────────────────────────────────
def reconcile_columns(athena_cols, sf_cols, calc_sf_cols, overrides):
    """Match Athena output columns to Snowflake view columns by normalized name.

    `overrides` ({athena_remote -> sf_physical}) are applied as aliases first, so an
    explicitly renamed column matches. `calc_sf_cols` are SF columns known to be
    materialized calc fields (expected to be SF-only, not real "missing on Athena").

    Returns (matched, athena_only, sf_only, sf_calc_only) where matched is a list of
    (athena_col, sf_col) pairs."""
    # alias athena names via overrides so they normalize onto the sf physical name
    alias = {norm(k): norm(v) for k, v in overrides.items()}
    sf_by_norm = {norm(c): c for c in sf_cols}
    matched, athena_only = [], []
    used_sf = set()
    for ac in athena_cols:
        target = alias.get(norm(ac), norm(ac))
        sf = sf_by_norm.get(target)
        if sf is not None:
            matched.append((ac, sf))
            used_sf.add(sf)
        else:
            athena_only.append(ac)
    calc_norm = {norm(c) for c in calc_sf_cols}
    sf_only, sf_calc_only = [], []
    for sc in sf_cols:
        if sc in used_sf:
            continue
        (sf_calc_only if norm(sc) in calc_norm else sf_only).append(sc)
    return matched, athena_only, sf_only, sf_calc_only


# ── source-table tracing ──────────────────────────────────────────────────────
def load_table_mappings(path=None):
    """Read table_mappings.csv into a list of dict rows."""
    import csv
    path = path or os.path.join(_REPO_ROOT, 'table_mappings.csv')
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))


# a FROM/JOIN target: capture a dotted, optionally-quoted identifier chain. Bare
# single-word targets are almost always CTE names, so we require >=2 parts (a
# schema/db-qualified table) to count as a real source table.
_FROM_JOIN = re.compile(
    r'\b(?:FROM|JOIN)\s+("?[\w$]+"?(?:\.\s*"?[\w$]+"?)+)', re.IGNORECASE)
_CTE_NAME = re.compile(r'(?:\bWITH\b|,)\s*"?([\w$]+)"?\s+AS\s*\(', re.IGNORECASE)


def parse_source_tables(sql):
    """Best-effort: pull schema/db-qualified source tables out of an Athena query,
    excluding CTE names. Returns a sorted list of (db, table) where db may itself be
    dotted (e.g. `edwcatalog.dataprep`). Heuristic -- report, don't trust blindly."""
    ctes = {m.lower() for m in _CTE_NAME.findall(sql)}
    tables = set()
    for raw in _FROM_JOIN.findall(sql):
        parts = [p.strip().strip('"') for p in raw.split('.')]
        if not parts or parts[0].lower() in ctes:
            continue
        db, table = '.'.join(parts[:-1]), parts[-1]
        tables.add((db, table))
    return sorted(tables)


def map_source_table(db, table, mappings, sql=None):
    """Resolve one Athena (db, table) to a Snowflake FQN via table_mappings.csv.
    Returns (sf_fqn or None, note). Handles the omniture 1:many split by matching a
    `reporting_suite = '<suite>'` predicate in `sql` to the mapped row's notes."""
    rows = [r for r in mappings
            if r['athena_database'].strip().lower() == db.lower()
            and r['athena_table'].strip().lower() == table.lower()]
    if not rows:
        # fuzzy: table name alone
        rows = [r for r in mappings if r['athena_table'].strip().lower() == table.lower()]
        if not rows:
            return None, 'no mapping in table_mappings.csv'
    if len(rows) > 1:
        # omniture-style 1:many keyed on reporting_suite
        suite = None
        if sql:
            m = re.search(r"reporting_suite\s*=\s*'([^']+)'", sql, re.IGNORECASE)
            suite = m.group(1).lower() if m else None
        if suite:
            for r in rows:
                if suite in (r.get('notes') or '').lower():
                    fqn = f"{r['snowflake_database']}.{r['snowflake_schema']}.{r['snowflake_table']}"
                    return fqn, f"1:many split; matched reporting_suite='{suite}'"
        cands = ', '.join(f"{r['snowflake_database']}.{r['snowflake_schema']}.{r['snowflake_table']}"
                          for r in rows)
        return None, f'{len(rows)} candidate mappings (ambiguous): {cands}'
    r = rows[0]
    return (f"{r['snowflake_database']}.{r['snowflake_schema']}.{r['snowflake_table']}",
            (r.get('notes') or '').strip())


def trace_sources(athena_sql, mappings):
    """For each source table in the Athena SQL, probe row counts on both platforms
    so a divergence can be localized. Returns a list of per-table dicts. Runs only
    when a check has already flagged a discrepancy (caller decides)."""
    traced = []
    for db, table in parse_source_tables(athena_sql):
        entry = {'athena': f'{db}.{table}'}
        sf_fqn, note = map_source_table(db, table, mappings, sql=athena_sql)
        entry['snowflake'] = sf_fqn
        entry['note'] = note
        try:
            df = execute_athena_query(f'SELECT COUNT(*) AS c FROM {db}.{table}')
            entry['athena_rows'] = int(_row_to_dict(df).get('c'))
        except Exception as e:  # deprecated/broken source table
            entry['athena_rows'] = None
            entry['athena_error'] = str(e).splitlines()[0][:200]
        if sf_fqn:
            try:
                df = execute_snowflake_query(f'SELECT COUNT(*) AS c FROM {sf_fqn}')
                entry['snowflake_rows'] = int(_row_to_dict(df).get('c'))
            except Exception as e:
                entry['snowflake_rows'] = None
                entry['snowflake_error'] = str(e).splitlines()[0][:200]
        else:
            entry['snowflake_rows'] = None
        ar, sr = entry.get('athena_rows'), entry.get('snowflake_rows')
        if ar and sr is not None:
            entry['delta_pct'] = round(abs(sr - ar) / ar * 100, 2) if ar else None
        traced.append(entry)
    return traced


# ── deep row-level reconciliation ─────────────────────────────────────────────
def deep_reconcile(athena_sql, sf_from, matched, key_cols, max_rows):
    """Full pull of both result sets and a key-based row diff in pandas.

    Cross-engine hashing isn't comparable, so we pull both sides, align columns to
    the matched (athena_col -> sf_col) pairs, and diff on `key_cols`. Guarded by
    `max_rows`: if either side exceeds it, we skip with a clear note rather than pull
    a huge result. Returns a dict of counts + small samples, or a skip reason."""
    if not key_cols:
        return {'skipped': 'no --key given; row-level reconciliation needs a key'}
    import pandas as pd

    a_df = execute_athena_query(f"SELECT * FROM (\n{athena_sql}\n) _lt_full")
    s_df = execute_snowflake_query(f"SELECT * FROM {sf_from}")
    if len(a_df) > max_rows or len(s_df) > max_rows:
        return {'skipped': f'result exceeds --deep-max-rows ({max_rows}): '
                           f'athena={len(a_df)}, snowflake={len(s_df)}'}

    # align both frames to a common, normalized schema using matched pairs
    a_map = {ac: norm(ac) for ac, _sc in matched}
    s_map = {sc: norm(ac) for ac, sc in matched}
    a_df = a_df.rename(columns={c: a_map.get(c, norm(c)) for c in a_df.columns})
    s_df = s_df.rename(columns={c: s_map.get(c, norm(c)) for c in s_df.columns})
    common = [norm(ac) for ac, _ in matched]
    keys = [norm(k) for k in key_cols]
    a_df, s_df = a_df[common].copy(), s_df[common].copy()

    a_keyed = a_df.set_index(keys)
    s_keyed = s_df.set_index(keys)
    a_idx, s_idx = set(a_keyed.index), set(s_keyed.index)
    only_a, only_s, both = a_idx - s_idx, s_idx - a_idx, a_idx & s_idx

    changed = 0
    for k in list(both)[:max_rows]:
        ar = a_keyed.loc[[k]].astype(str).iloc[0].to_dict()
        srow = s_keyed.loc[[k]].astype(str).iloc[0].to_dict()
        if ar != srow:
            changed += 1
    return {
        'athena_rows': len(a_df), 'snowflake_rows': len(s_df),
        'keys': keys,
        'only_in_athena': len(only_a), 'only_in_snowflake': len(only_s),
        'changed_rows': changed, 'matched_keys': len(both),
        'sample_only_in_athena': [list(k) if isinstance(k, tuple) else [k]
                                  for k in list(only_a)[:5]],
        'sample_only_in_snowflake': [list(k) if isinstance(k, tuple) else [k]
                                     for k in list(only_s)[:5]],
    }


# ── orchestration ─────────────────────────────────────────────────────────────
def rate_delta(delta_pct, threshold):
    if delta_pct is None:
        return 'UNKNOWN'
    if delta_pct < threshold:
        return 'PASS'
    if delta_pct <= 5.0:
        return 'CONDITIONAL'
    return 'INVESTIGATE'


def run(athena_sql, sf_from, sf_fqn=None, config=None, match=None, keys=None,
        deep=False, deep_max_rows=500000, delta_threshold=1.0, trace=True,
        label=None, mappings_path=None):
    """Run the full comparison. Returns a result dict (also the JSON payload)."""
    keys = keys or []
    # undo the .twb doubled-operator artifact before anything runs the Athena SQL
    athena_sql = normalize_athena_sql(athena_sql)
    result = {
        'label': label, 'athena_sql_len': len(athena_sql), 'sf_from': sf_from,
        'keys': keys, 'deep': deep, 'delta_threshold': delta_threshold,
        'status': None, 'issues': [], 'athena_error': None,
    }

    # 1) discover Athena output columns + sample (also the BLOCKED gate)
    try:
        a_sample = athena_sample(athena_sql)
    except Exception as e:
        result['status'] = 'BLOCKED'
        result['athena_error'] = str(e).splitlines()[0][:400]
        result['issues'].append({
            'severity': 'HIGH', 'category': 'comparison-impossible',
            'detail': f'Athena source could not be queried (deprecated/broken?): '
                      f'{result["athena_error"]}'})
        # still profile the SF side so the run isn't a total loss
        try:
            sf_cols = snowflake_columns(sf_fqn) if sf_fqn else []
            result['snowflake_only_profile'] = {
                'columns': [c for c, _ in sf_cols],
                'row_count': int(_row_to_dict(execute_snowflake_query(
                    f'SELECT COUNT(*) AS c FROM {sf_from}')).get('c')),
            }
        except Exception as e2:
            result['snowflake_error'] = str(e2).splitlines()[0][:400]
        return result

    athena_cols = list(a_sample.columns)
    import pandas.api.types as pdt
    athena_numeric = [c for c in athena_cols if pdt.is_numeric_dtype(a_sample[c])]
    result['athena_sample'] = a_sample.head(5).astype(str).to_dict(orient='records')

    # 2) Snowflake columns + types + sample
    sf_typed = snowflake_columns(sf_fqn) if sf_fqn else []
    sf_cols = [c for c, _ in sf_typed]
    sf_numeric = [c for c, t in sf_typed if _is_numeric_sf_type(t)]
    if not sf_cols:  # e.g. --sf-sql (not deployed): fall back to a sample for names
        s_sample0 = snowflake_sample(sf_from)
        sf_cols = list(s_sample0.columns)
        sf_numeric = [c for c in sf_cols if pdt.is_numeric_dtype(s_sample0[c])]
        result['snowflake_sample'] = s_sample0.head(5).astype(str).to_dict(orient='records')
    else:
        s_sample = snowflake_sample(sf_from)
        result['snowflake_sample'] = s_sample.head(5).astype(str).to_dict(orient='records')

    # 3) column reconciliation
    calc_sf_cols, overrides = load_expected_calc_and_overrides(config, match)
    matched, athena_only, sf_only, sf_calc_only = reconcile_columns(
        athena_cols, sf_cols, calc_sf_cols, overrides)
    result['schema'] = {
        'matched': [{'athena': a, 'snowflake': s} for a, s in matched],
        'athena_only': athena_only, 'snowflake_only': sf_only,
        'snowflake_calc_only': sf_calc_only,
    }
    if athena_only:
        result['issues'].append({
            'severity': 'MEDIUM', 'category': 'schema',
            'detail': f'{len(athena_only)} column(s) in Athena output missing from the '
                      f'Snowflake view: {athena_only}'})
    if sf_only:
        result['issues'].append({
            'severity': 'LOW', 'category': 'schema',
            'detail': f'{len(sf_only)} non-calc Snowflake column(s) with no Athena match '
                      f'(extra columns): {sf_only}'})

    # 4) aggregate battery on each platform
    a_from = f"(\n{athena_sql}\n) _lt"
    a_agg = run_aggregates(execute_athena_query, a_from, athena_cols, athena_numeric, keys)
    s_agg = run_aggregates(execute_snowflake_query, sf_from, sf_cols, sf_numeric, keys)
    result['athena_aggregates'] = a_agg
    result['snowflake_aggregates'] = s_agg

    # row count
    ar, sr = a_agg['row_count'], s_agg['row_count']
    delta_pct = round(abs(sr - ar) / ar * 100, 2) if ar else (0.0 if sr == 0 else None)
    row_rating = rate_delta(delta_pct, delta_threshold)
    result['row_count'] = {'athena': ar, 'snowflake': sr,
                           'delta': (sr - ar) if (ar is not None and sr is not None) else None,
                           'delta_pct': delta_pct, 'rating': row_rating}
    if row_rating in ('CONDITIONAL', 'INVESTIGATE', 'UNKNOWN'):
        result['issues'].append({
            'severity': 'HIGH' if row_rating == 'INVESTIGATE' else 'MEDIUM',
            'category': 'row-count',
            'detail': f'Row count {row_rating}: Athena {ar} vs Snowflake {sr} '
                      f'(delta {delta_pct}%)'})

    # 5) per-column null / numeric comparison on matched pairs
    col_findings = []
    for ac, sc in matched:
        a_c = a_agg['columns'].get(ac, {})
        s_c = s_agg['columns'].get(sc, {})
        entry = {'athena': ac, 'snowflake': sc}
        a_null, s_null = a_c.get('nulls'), s_c.get('nulls')
        entry['athena_nulls'], entry['snowflake_nulls'] = a_null, s_null
        # flag a null-rate divergence larger than the row-count delta explains
        if a_null is not None and s_null is not None and ar and sr:
            a_rate, s_rate = a_null / ar, s_null / sr
            entry['null_rate_delta_pct'] = round(abs(a_rate - s_rate) * 100, 2)
            if entry['null_rate_delta_pct'] > 1.0:
                result['issues'].append({
                    'severity': 'MEDIUM', 'category': 'nulls',
                    'detail': f'Null rate differs on {ac}->{sc}: '
                              f'Athena {a_rate:.2%} vs Snowflake {s_rate:.2%}'})
        for m in ('sum', 'min', 'max', 'distinct'):
            if m in a_c or m in s_c:
                entry[f'athena_{m}'], entry[f'snowflake_{m}'] = a_c.get(m), s_c.get(m)
        # numeric sum divergence
        if entry.get('athena_sum') is not None and entry.get('snowflake_sum') is not None:
            base = abs(entry['athena_sum']) or 1
            sum_delta = abs(entry['snowflake_sum'] - entry['athena_sum']) / base * 100
            entry['sum_delta_pct'] = round(sum_delta, 4)
            if sum_delta > max(delta_threshold, 0.5):
                result['issues'].append({
                    'severity': 'HIGH', 'category': 'aggregate',
                    'detail': f'SUM differs on {ac}->{sc}: Athena {entry["athena_sum"]} '
                              f'vs Snowflake {entry["snowflake_sum"]} ({sum_delta:.2f}%)'})
        col_findings.append(entry)
    result['column_comparison'] = col_findings

    # 6) optional deep row-level reconciliation
    if deep:
        result['deep'] = deep_reconcile(athena_sql, sf_from, matched, keys, deep_max_rows)
        d = result['deep']
        if not d.get('skipped') and (d.get('only_in_athena') or d.get('only_in_snowflake')
                                     or d.get('changed_rows')):
            result['issues'].append({
                'severity': 'HIGH', 'category': 'row-level',
                'detail': f"Row-level diff on {d.get('keys')}: "
                          f"{d.get('only_in_athena')} only-in-Athena, "
                          f"{d.get('only_in_snowflake')} only-in-Snowflake, "
                          f"{d.get('changed_rows')} changed"})

    # 7) overall status
    severities = {i['severity'] for i in result['issues']}
    if 'HIGH' in severities:
        result['status'] = 'INVESTIGATE'
    elif 'MEDIUM' in severities:
        result['status'] = 'CONDITIONAL'
    else:
        result['status'] = 'PASS'

    # 8) trace sources only when something is off
    if trace and result['status'] in ('INVESTIGATE', 'CONDITIONAL'):
        result['source_trace'] = trace_sources(athena_sql, load_table_mappings(mappings_path))

    return result


# ── markdown report ────────────────────────────────────────────────────────────
def _fmt(v):
    return '' if v is None else v


def write_report(result, out_md):
    label = result.get('label') or result.get('sf_from') or 'logic test'
    L = [f'# {label} — Logic Test (Athena vs Snowflake)\n']
    L.append(f'**Snowflake target:** `{result.get("sf_from")}`  ')
    L.append(f'**Overall assessment:** **{result.get("status")}**  ')
    if result.get('deep'):
        L.append('**Mode:** deep (row-level reconciliation)  ')
    L.append('')

    if result.get('status') == 'BLOCKED':
        L.append('## Comparison not possible\n')
        L.append('The Athena source could not be queried — likely deprecated or broken. '
                 'The back-end logic test cannot run; validate this datasource via the '
                 'front-end Tableau open test instead.\n')
        L.append(f'- Athena error: `{result.get("athena_error")}`')
        prof = result.get('snowflake_only_profile')
        if prof:
            L.append(f'- Snowflake view still profiled: {prof.get("row_count")} rows, '
                     f'{len(prof.get("columns", []))} columns.')
        _write(out_md, L)
        return out_md

    # row count
    rc = result.get('row_count', {})
    L.append('## 1. Row Count\n')
    L.append('| | Athena | Snowflake | Delta | Delta % | Result |')
    L.append('|---|---|---|---|---|---|')
    L.append(f'| rows | {_fmt(rc.get("athena"))} | {_fmt(rc.get("snowflake"))} | '
             f'{_fmt(rc.get("delta"))} | {_fmt(rc.get("delta_pct"))}% | {rc.get("rating")} |')
    L.append('')

    # schema
    sch = result.get('schema', {})
    L.append('## 2. Schema / Column Reconciliation\n')
    L.append(f'- Matched columns: {len(sch.get("matched", []))}')
    L.append(f'- Athena-only (missing on Snowflake): {sch.get("athena_only") or "none"}')
    L.append(f'- Snowflake-only non-calc (extra): {sch.get("snowflake_only") or "none"}')
    L.append(f'- Snowflake materialized calc columns (expected, no Athena source): '
             f'{sch.get("snowflake_calc_only") or "none"}')
    L.append('')

    # column comparison
    cols = result.get('column_comparison', [])
    if cols:
        L.append('## 3. Column Comparison (nulls / aggregates)\n')
        L.append('| Athena→Snowflake | Ath nulls | SF nulls | Null Δ% | Ath sum | SF sum | Sum Δ% | Flag |')
        L.append('|---|---|---|---|---|---|---|---|')
        for c in cols:
            flag = ''
            if c.get('null_rate_delta_pct', 0) and c['null_rate_delta_pct'] > 1.0:
                flag = 'NULLS'
            if c.get('sum_delta_pct', 0) and c['sum_delta_pct'] > 0.5:
                flag = (flag + ' SUM').strip()
            L.append(f'| {c["athena"]}→{c["snowflake"]} | {_fmt(c.get("athena_nulls"))} | '
                     f'{_fmt(c.get("snowflake_nulls"))} | {_fmt(c.get("null_rate_delta_pct"))} | '
                     f'{_fmt(c.get("athena_sum"))} | {_fmt(c.get("snowflake_sum"))} | '
                     f'{_fmt(c.get("sum_delta_pct"))} | {flag or "OK"} |')
        L.append('')

    # deep
    if result.get('deep'):
        d = result['deep']
        L.append('## 4. Row-Level Reconciliation (deep)\n')
        if d.get('skipped'):
            L.append(f'- Skipped: {d["skipped"]}')
        else:
            L.append(f'- Keys: `{d.get("keys")}`')
            L.append(f'- Only in Athena: {d.get("only_in_athena")}  '
                     f'(sample keys: {d.get("sample_only_in_athena")})')
            L.append(f'- Only in Snowflake: {d.get("only_in_snowflake")}  '
                     f'(sample keys: {d.get("sample_only_in_snowflake")})')
            L.append(f'- Changed rows (same key, differing values): {d.get("changed_rows")} '
                     f'of {d.get("matched_keys")} matched keys')
        L.append('')

    # source trace
    if result.get('source_trace'):
        L.append('## 5. Source-Table Trace (discrepancy localization)\n')
        L.append('Per source table parsed from the Athena SQL, row counts on both '
                 'platforms so the divergence can be localized to the table/join it '
                 'enters at.\n')
        L.append('| Athena table | Snowflake table | Athena rows | SF rows | Δ% | Note |')
        L.append('|---|---|---|---|---|---|')
        for t in result['source_trace']:
            L.append(f'| {t.get("athena")} | {_fmt(t.get("snowflake"))} | '
                     f'{_fmt(t.get("athena_rows"))} | {_fmt(t.get("snowflake_rows"))} | '
                     f'{_fmt(t.get("delta_pct"))} | {t.get("note", "")} |')
        L.append('')

    # issues
    L.append('## 6. Issues\n')
    if result.get('issues'):
        L.append('| # | Severity | Category | Detail |')
        L.append('|---|---|---|---|')
        for i, iss in enumerate(result['issues'], 1):
            L.append(f'| {i} | {iss["severity"]} | {iss["category"]} | {iss["detail"]} |')
    else:
        L.append('No issues — Athena and Snowflake logic agree within thresholds.')
    L.append('')
    L.append(f'### Overall Assessment: {result.get("status")}\n')
    _write(out_md, L)
    return out_md


def _write(out_md, lines):
    os.makedirs(os.path.dirname(os.path.abspath(out_md)), exist_ok=True)
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ── CLI ─────────────────────────────────────────────────────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(
        description='Back-end logic equivalence test (Athena original vs Snowflake view).')
    p.add_argument('--athena-sql', required=True,
                   help='path to the extracted Athena/Presto SQL (single relation or flattened join)')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--sf-fqn', help='deployed Snowflake view DB.SCHEMA.VIEW to compare against')
    g.add_argument('--sf-sql', help='path to a .sql SELECT for a not-yet-deployed view '
                                    '(wrapped as a subquery; no INFORMATION_SCHEMA types)')
    p.add_argument('--config', help='reconstruct config JSON (to tag materialized calc '
                                    'columns + honor column_overrides)')
    p.add_argument('--match', help='datasource caption/name to select from --config')
    p.add_argument('--key', action='append', dest='keys', default=[],
                   help='key column for distinct-count + deep reconciliation (repeatable)')
    p.add_argument('--deep', action='store_true', help='row-level reconciliation on --key')
    p.add_argument('--deep-max-rows', type=int, default=500000,
                   help='skip deep pull if either side exceeds this (default 500000)')
    p.add_argument('--delta-threshold', type=float, default=1.0,
                   help='row-count delta%% under which the run PASSes (default 1.0)')
    p.add_argument('--no-trace', action='store_true', help='disable source-table tracing')
    p.add_argument('--label', help='label for the report (datasource name)')
    p.add_argument('--out-json', help='write the structured result JSON here')
    p.add_argument('--out-md', help='write the markdown report here')
    p.add_argument('--mappings', help='path to table_mappings.csv (default: repo root)')
    args = p.parse_args(argv)

    with open(args.athena_sql, 'r', encoding='utf-8') as f:
        athena_sql = _strip_sql_comments_header(f.read())

    if args.sf_fqn:
        sf_from, sf_fqn = args.sf_fqn, args.sf_fqn
    else:
        with open(args.sf_sql, 'r', encoding='utf-8') as f:
            sf_from = '(\n' + _strip_sql_comments_header(f.read()) + '\n) _lt_sf'
        sf_fqn = None

    result = run(athena_sql, sf_from, sf_fqn=sf_fqn, config=args.config, match=args.match,
                 keys=args.keys, deep=args.deep, deep_max_rows=args.deep_max_rows,
                 delta_threshold=args.delta_threshold, trace=not args.no_trace,
                 label=args.label or (args.sf_fqn or args.athena_sql),
                 mappings_path=args.mappings)

    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, default=str)
        print(f'Wrote JSON: {args.out_json}')
    if args.out_md:
        write_report(result, args.out_md)
        print(f'Wrote report: {args.out_md}')

    print(f'\n=== LOGIC TEST: {result.get("status")} ===')
    for iss in result.get('issues', []):
        print(f'  [{iss["severity"]}] {iss["category"]}: {iss["detail"]}')
    return 0 if result.get('status') in ('PASS', 'CONDITIONAL') else 1


def _strip_sql_comments_header(text):
    """Drop the leading `-- ...` comment header the extractors emit, keeping the SQL
    body (leading comments before the first non-comment line)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].lstrip().startswith('--') or not lines[i].strip()):
        i += 1
    return '\n'.join(lines[i:]).strip() or text.strip()


if __name__ == '__main__':
    sys.exit(main())
