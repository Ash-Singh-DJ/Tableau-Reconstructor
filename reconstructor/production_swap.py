"""
production_swap.py -- Snowflake -> Snowflake table repoint for Tableau .twbx.

Unlike reconstruct.py (which migrates an Athena custom-SQL workbook to Snowflake
and rebuilds scaffolding), this is a *narrow* operation: a workbook is ALREADY
Snowflake-backed (e.g. pointing at a SANDBOX view), and we repoint one or more of
its datasources at a final production table (B2B_GOLD or another prod schema),
leaving everything else byte-for-byte intact.

Because the source and target are both Snowflake and the column shapes are meant to
match, NO metadata-records are rebuilt, NO extract is stripped, NO calc is touched,
and drill-paths are preserved untouched. The ONLY edits are:
  (1) each target datasource's inner <connection> dbname/schema -> the prod DB/schema,
  (2) each <relation type="table"> table="[DB].[SCHEMA].[TABLE]" -> the prod table
      (both the connection-level relation AND its object-graph duplicate),
  (3) OPTIONALLY, if a column was renamed in prod, rewrite the affected
      <metadata-record> remote-name/remote-alias via a per-datasource column_map.

The seamless-swap CONTRACT (enforced when target columns are supplied via
--target-columns): the production table must be a SUPERSET of the columns the
workbook currently binds (after applying any column_map). Extra prod columns are
fine; a missing bound column is a hard refusal -- the swap would silently break a
field binding. This module does NOT query Snowflake itself (no credentials here);
the driving skill queries INFORMATION_SCHEMA and passes the column sets in.

------------------------------------------------------------------------------
CONFIG SCHEMA (dict, or JSON file passed via --config):
{
  "connection": {                 # optional overrides; omitted attrs are KEPT as-is
    "warehouse": "...",           #   from the workbook (same Snowflake account assumed)
    "role": "...",
    "server": "..."
  },
  "datasources": [
    {
      "match":  "Custom SQL Query (dimension_external_db)",   # caption or federated id
      "target": "B2B_GOLD.HIGH_VALUE_CUSTOMER.PROFILER_DASHBOARD_FULL_GOLD",
      "column_map": {              # optional; ONLY for columns renamed in prod
        "OLD_REMOTE_NAME": "NEW_REMOTE_NAME"
      }
    }
  ]
}

TARGET-COLUMNS SCHEMA (dict, or JSON via --target-columns) -- the prod table's
actual columns, queried by the skill, keyed by the same "match" value:
{
  "Custom SQL Query (dimension_external_db)": {
    "PRIMARY_NAME": "TEXT", "DUNS_NUMBER": "NUMBER", ...
  }
}
Datasources NOT listed in config are left untouched.
------------------------------------------------------------------------------

Usage:
    python production_swap.py INPUT.twbx --list                      # show SF datasources + bound cols
    python production_swap.py INPUT.twbx --emit-config               # print a config skeleton
    python production_swap.py INPUT.twbx --config c.json [-o OUT.twbx]
    python production_swap.py INPUT.twbx --config c.json --target-columns cols.json
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile


# ---- .twb IO -----------------------------------------------------------------
def read_twb(twbx_path):
    """Return (twb_entry_name, raw_text) for the .twb inside a .twbx."""
    with zipfile.ZipFile(twbx_path) as z:
        names = [n for n in z.namelist() if n.endswith('.twb')]
        if not names:
            raise RuntimeError(f'No .twb found inside {twbx_path}')
        return names[0], z.read(names[0]).decode('utf-8')


# ---- introspection -----------------------------------------------------------
def _snowflake_inner(ds):
    """Return the inner snowflake <connection> element for a datasource, or None."""
    conn = ds.find('connection')
    if conn is None:
        return None
    for nc in conn.iter('named-connection'):
        inner = nc.find('connection')
        if inner is not None and inner.get('class') == 'snowflake':
            return inner
    return None


def _current_table(ds):
    """Return the live connection's table attr (e.g. [DB].[SCHEMA].[TABLE]).

    Prefer the relation that carries a `connection` attribute -- that is the one
    bound to the Snowflake named-connection. Extract-backed workbooks also carry a
    `[Extract].[Extract]` relation (no connection attr) for the local extract
    namespace; that is NOT the source table and must not be returned here."""
    fallback = None
    for rel in ds.iter('relation'):
        if rel.get('type') == 'table' and rel.get('table'):
            if rel.get('connection'):
                return rel.get('table')
            if fallback is None:
                fallback = rel.get('table')
    return fallback


def bound_columns(ds):
    """The remote-name + local-type each datasource binds, from connection-level
    metadata-records. These are the columns the workbook expects to exist."""
    conn = ds.find('connection')
    cols = []
    if conn is not None:
        mrs = conn.find('metadata-records')
        if mrs is not None:
            for r in mrs.findall('metadata-record'):
                if r.get('class') == 'column':
                    rn = r.findtext('remote-name')
                    if rn:
                        cols.append((rn, r.findtext('local-type', 'string')))
    return cols


def describe_workbook(twbx_path):
    """List Snowflake-backed datasources with current target + bound columns."""
    _, raw = read_twb(twbx_path)
    root = ET.fromstring(raw)
    out = []
    dss = root.find('.//datasources')
    if dss is None:
        return out
    for ds in dss.findall('datasource'):
        if _snowflake_inner(ds) is None:
            continue
        out.append({
            'name': ds.get('name'),
            'caption': ds.get('caption'),
            'current_table': _current_table(ds),
            'bound_columns': bound_columns(ds),
        })
    return out


def emit_config_skeleton(twbx_path):
    desc = describe_workbook(twbx_path)
    datasources = []
    for d in desc:
        datasources.append({
            'match': d['caption'] or d['name'],
            'target': 'TODO_DB.TODO_SCHEMA.TODO_TABLE',
            '_current_table': d['current_table'],
            '_bound_columns': [c[0] for c in d['bound_columns']],
            'column_map': {},
        })
    print(json.dumps({'connection': {}, 'datasources': datasources}, indent=2))


# ---- helpers -----------------------------------------------------------------
def parse_target(target):
    """'DB.SCHEMA.TABLE' -> (DB, SCHEMA, TABLE, '[DB].[SCHEMA].[TABLE]')."""
    parts = target.split('.')
    if len(parts) != 3:
        raise RuntimeError(f"target must be DB.SCHEMA.TABLE, got: {target!r}")
    db, schema, table = parts
    return db, schema, table, f'[{db}].[{schema}].[{table}]'


def _match_datasource(ds, match):
    """A config 'match' resolves against caption first, then federated name."""
    return ds.get('caption') == match or ds.get('name') == match


# ---- validation --------------------------------------------------------------
def validate_columns(ds, ds_cfg, target_cols, match):
    """Enforce the seamless-swap contract: every column the workbook binds (after
    column_map) must exist in the production table. Returns a report dict; raises
    on a missing bound column."""
    column_map = ds_cfg.get('column_map', {})
    target_names = {c.upper() for c in target_cols}
    bound = bound_columns(ds)

    mapped, missing = [], []
    for remote, _ltype in bound:
        wanted = column_map.get(remote, remote)
        mapped.append((remote, wanted))
        if wanted.upper() not in target_names:
            missing.append((remote, wanted))

    if missing:
        lines = [f"  bound '{o}' -> expects '{n}' (NOT in target)" for o, n in missing]
        raise RuntimeError(
            f"{match}: production table is missing {len(missing)} bound column(s); "
            f"swap would break field bindings. Supply a column_map or fix the table:\n"
            + "\n".join(lines))

    # type drift is reported, not blocked (Tableau reconciles remote-type on refresh)
    type_notes = []
    for remote, ltype in bound:
        wanted = column_map.get(remote, remote)
        tt = target_cols.get(wanted) or target_cols.get(wanted.upper())
        if tt:
            type_notes.append((wanted, ltype, tt))
    extra = sorted(target_names - {n.upper() for _o, n in mapped})
    return {
        'bound_count': len(bound),
        'remapped': [(o, n) for o, n in mapped if o != n],
        'extra_in_target': extra,
        'type_notes': type_notes,
    }


# ---- core transform ----------------------------------------------------------
def swap_datasource(ds, ds_cfg, conn_overrides, report):
    fed_name = ds.get('name')
    match = ds_cfg.get('match') or ds_cfg.get('name')
    db, schema, table, sf_table = parse_target(ds_cfg['target'])
    column_map = ds_cfg.get('column_map', {})

    inner = _snowflake_inner(ds)
    if inner is None:
        raise RuntimeError(f'{fed_name}: no snowflake named-connection found '
                           '(is this workbook already Snowflake-backed?)')

    old_table = _current_table(ds)

    # (1) repoint the inner connection's database/schema context (+ optional overrides)
    inner.set('dbname', db)
    inner.set('schema', schema)
    for attr in ('warehouse', 'role', 'server'):
        if attr in conn_overrides:
            inner.set(attr, conn_overrides[attr])
    if 'server' in conn_overrides:
        inner.set('instanceurl', f"https://{conn_overrides['server']}")

    # (2) repoint the source-table relations only (connection-level AND object-graph
    #     duplicate). An extract-backed workbook ALSO carries `[Extract].[Extract]`
    #     relations for the local extract namespace -- those are not the source table
    #     and must stay `[Extract].[Extract]`, else the extract's logical model breaks
    #     ("missing field" / "unable to create extract"). Gate on the old table value.
    relations_repointed = 0
    relations_skipped = []
    for rel in ds.iter('relation'):
        if rel.get('type') != 'table' or not rel.get('table'):
            continue
        if rel.get('table') == old_table:
            rel.set('table', sf_table)
            relations_repointed += 1
        else:
            relations_skipped.append(rel.get('table'))

    # (3) optional remote-name remap for prod-renamed columns
    cols_remapped = []
    if column_map:
        conn = ds.find('connection')
        mrs = conn.find('metadata-records') if conn is not None else None
        if mrs is not None:
            for r in mrs.findall('metadata-record'):
                if r.get('class') != 'column':
                    continue
                rn_el = r.find('remote-name')
                if rn_el is not None and rn_el.text in column_map:
                    new = column_map[rn_el.text]
                    cols_remapped.append((rn_el.text, new))
                    rn_el.text = new
                    ra_el = r.find('remote-alias')
                    if ra_el is not None:
                        ra_el.text = new

    report.append({
        'datasource': fed_name,
        'caption': ds.get('caption'),
        'match': match,
        'old_table': old_table,
        'new_table': sf_table,
        'new_dbname': db,
        'new_schema': schema,
        'relations_repointed': relations_repointed,
        'relations_skipped': relations_skipped,
        'columns_remapped': cols_remapped,
        'conn_overrides': {k: v for k, v in conn_overrides.items()},
    })


def count_drill_paths(ds):
    dp = ds.find('drill-paths')
    return len(dp.findall('drill-path')) if dp is not None else 0


# ---- orchestration -----------------------------------------------------------
def production_swap(input_twbx, config, output_twbx, target_columns=None, verbose=True):
    conn_overrides = config.get('connection', {}) or {}
    ds_configs = config['datasources']

    twb_name, raw = read_twb(input_twbx)
    prefix = raw[:raw.index('<workbook')]   # preserve XML decl + build comment
    root = ET.fromstring(raw)
    datasources = root.find('.//datasources')

    # resolve config matches to datasource elements
    matched = []
    for ds_cfg in ds_configs:
        match = ds_cfg.get('match') or ds_cfg.get('name')
        hits = [ds for ds in datasources.findall('datasource')
                if _match_datasource(ds, match)]
        if not hits:
            raise RuntimeError(f"config datasource not found in workbook: {match!r}")
        if len(hits) > 1:
            raise RuntimeError(f"config datasource ambiguous (matched {len(hits)}): {match!r}")
        matched.append((hits[0], ds_cfg, match))

    # validation gate (only if target columns supplied)
    validations = {}
    if target_columns is not None:
        for ds, ds_cfg, match in matched:
            if match not in target_columns:
                raise RuntimeError(
                    f"{match}: no target columns supplied for validation. Query the "
                    f"production table's INFORMATION_SCHEMA and pass them in, or omit "
                    f"--target-columns to skip the check.")
            validations[match] = validate_columns(ds, ds_cfg, target_columns[match], match)

    report = []
    drill_before = {ds.get('name'): count_drill_paths(ds) for ds, _c, _m in matched}
    for ds, ds_cfg, _match in matched:
        swap_datasource(ds, ds_cfg, conn_overrides, report)

    # invariant: drill-paths untouched
    for ds, _c, _m in matched:
        after = count_drill_paths(ds)
        assert after == drill_before[ds.get('name')], \
            f"{ds.get('name')}: drill-paths changed {drill_before[ds.get('name')]}->{after}"

    new_twb = prefix + ET.tostring(root, encoding='unicode')

    os.makedirs(os.path.dirname(os.path.abspath(output_twbx)), exist_ok=True)
    with zipfile.ZipFile(input_twbx) as zin, \
            zipfile.ZipFile(output_twbx, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            # production swap PRESERVES extract mode: keep .hyper entries as-is
            zout.writestr(item, new_twb if item == twb_name else zin.read(item))

    result = {'report': report, 'output': output_twbx, 'validations': validations}
    if verbose:
        _print_report(result)
    return result


def _print_report(result):
    print(f"\nWrote: {result['output']}")
    print('\n=== PRODUCTION SWAP REPORT ===')
    for r in result['report']:
        print(f"\n[{r['caption']}]  ({r['datasource']})")
        print(f"  table   : {r['old_table']} -> {r['new_table']}")
        print(f"  context : dbname/schema -> {r['new_dbname']}.{r['new_schema']}")
        print(f"  relations repointed: {r['relations_repointed']}")
        if r.get('relations_skipped'):
            print(f"  relations preserved (extract namespace): {r['relations_skipped']}")
        if r['conn_overrides']:
            print(f"  connection overrides: {r['conn_overrides']}")
        if r['columns_remapped']:
            print(f"  columns remapped ({len(r['columns_remapped'])}):")
            for o, n in r['columns_remapped']:
                print(f"    - {o} -> {n}")
        v = result['validations'].get(r['match'])
        if v:
            print(f"  validation: {v['bound_count']} bound cols all present in target "
                  f"(+{len(v['extra_in_target'])} extra in target)")
            if v['remapped']:
                print(f"    remapped via column_map: {v['remapped']}")


def write_notes(result, notes_path):
    lines = ['# Production swap notes (Snowflake -> Snowflake)\n']
    lines.append('Generated by `Reconstructor/production_swap.py`. In-place repoint of '
                 'Snowflake datasource(s) from a prior (e.g. SANDBOX) table to a final '
                 'production table. Extract mode, calculated fields, drill-down '
                 'hierarchies, and all worksheet bindings are preserved untouched.\n')
    lines.append(f'Output: `{os.path.basename(result["output"])}`\n')
    lines.append('## Per-datasource changes\n')
    for r in result['report']:
        lines.append(f'### {r["caption"]}\n')
        lines.append(f'- Datasource (unchanged identity): `{r["datasource"]}`')
        lines.append(f'- Table: `{r["old_table"]}` -> `{r["new_table"]}`')
        lines.append(f'- Connection context: dbname/schema -> `{r["new_dbname"]}.{r["new_schema"]}`')
        lines.append(f'- Relations repointed: {r["relations_repointed"]}')
        if r['columns_remapped']:
            lines.append(f'- Columns remapped (prod renames): {len(r["columns_remapped"])}')
            for o, n in r['columns_remapped']:
                lines.append(f'    - `{o}` -> `{n}`')
        v = result['validations'].get(r['match'])
        if v:
            lines.append(f'- Validation: all {v["bound_count"]} bound columns present in '
                         f'target (+{len(v["extra_in_target"])} extra columns in prod table)')
        lines.append('')
    lines.append('## Verification checklist (Tableau open test)\n')
    lines.append('- [ ] Workbook opens and signs in to Snowflake.')
    lines.append('- [ ] All worksheets render with no "field is missing" warnings.')
    lines.append('- [ ] Drill-down hierarchies and calculated fields intact.')
    lines.append('- [ ] If the source was extract-backed, refresh the extract against prod.')
    lines.append('- [ ] Visuals match the pre-swap workbook.')
    with open(notes_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return notes_path


# ---- CLI ---------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description='Snowflake -> Snowflake table repoint for Tableau .twbx.')
    p.add_argument('input', help='input .twbx (already Snowflake-backed)')
    p.add_argument('--config', help='config JSON file')
    p.add_argument('--target-columns', help='JSON of prod table columns per datasource (enables validation)')
    p.add_argument('-o', '--output', help='output .twbx path')
    p.add_argument('--list', action='store_true', help='list Snowflake datasources + bound columns and exit')
    p.add_argument('--emit-config', action='store_true', help='print a config skeleton and exit')
    p.add_argument('--notes', help='also write a notes .md to this path')
    args = p.parse_args(argv)

    if args.list:
        for d in describe_workbook(args.input):
            print(f"\n[{d['caption']}]  ({d['name']})")
            print(f"  current table: {d['current_table']}")
            print(f"  bound columns ({len(d['bound_columns'])}): "
                  f"{', '.join(c[0] for c in d['bound_columns'])}")
        return 0

    if args.emit_config:
        emit_config_skeleton(args.input)
        return 0

    if not args.config:
        p.error('--config is required (or use --list / --emit-config)')

    with open(args.config, 'r', encoding='utf-8') as f:
        config = json.load(f)
    for ds in config.get('datasources', []):
        ds.pop('_current_table', None)
        ds.pop('_bound_columns', None)

    target_columns = None
    if args.target_columns:
        with open(args.target_columns, 'r', encoding='utf-8') as f:
            target_columns = json.load(f)

    output = args.output
    if not output:
        base = os.path.splitext(os.path.basename(args.input))[0]
        output = os.path.join(os.path.dirname(os.path.abspath(args.input)),
                              f'{base} - Production Swap.twbx')

    result = production_swap(args.input, config, output, target_columns=target_columns)
    if args.notes:
        write_notes(result, args.notes)
        print(f'\nWrote notes: {args.notes}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
