"""
reconstruct.py -- workbook-agnostic Path A reconstructor.

Path A = convert a Tableau .twbx from Athena custom-SQL (extract-backed) to a
Snowflake table/view by editing the bundled .twb XML *in place*, preserving ALL
scaffolding (captions, drill-down hierarchies, calculated fields, worksheet
bindings). Contrast Path B (Tableau's Replace Data Source), which rebinds by
internal field name and silently drops scaffolding when names mismatch.

Why it works: Tableau binds worksheet fields by internal field name (local-name),
decoupled from the physical column via <metadata-record> (remote-name ->
local-name). We keep the <datasource> element intact and only rewrite:
  (1) the connection block (athena -> snowflake named-connection),
  (2) the relation (text custom-SQL -> table pointing at the gold view),
  (3) the connection-level metadata-records so each gold column maps to the
      ORIGINAL internal field name,
plus: neutralize materialized calc fields (strip <calculation> so they resolve to
the physical column, at BOTH datasource and worksheet-dependency levels) and strip
the extract (<extract> element + dangling object-graph extract-context relation).

This module is workbook-agnostic: it hardcodes no datasource ids, view names, or
calc bindings. Those come from a per-run JSON/dict config (see CONFIG SCHEMA
below). Base column bindings are AUTO-DERIVED from the workbook's own
connection-level metadata-records, so a config that only names the target view
(and optionally the casing + materialized calcs) is sufficient.

------------------------------------------------------------------------------
CONFIG SCHEMA (dict, or JSON file passed via --config):
{
  "connection": {                      # optional; defaults below can be overridden
    "server":    "<acct>.snowflakecomputing.com",
    "dbname":    "SANDBOX",
    "schema":    "B2B",
    "warehouse": "B2B_S_WH",
    "role":      "B2B_ANALYST_PRIVILEGED",
    "authentication": "oauth"
  },
  "datasources": [
    {
      "match":   "Custom SQL Query (dimension_external_db)",  # datasource caption
                                                              # (or "name": federated id)
      "view":    "HVC_VIEW_PROFILER_FULL_GOLD",   # target table/view in dbname.schema
      "casing":  "upper",                          # how the view names BASE columns
                                                   #   vs the workbook's lowercase
                                                   #   internal names: upper|lower|exact
      "calc_bindings": [                           # materialized row-level calcs
        ["GUC", "[Calculation_1080301001502830592]", "string"],
        ["XSELL_SUGGESTIONS", "[Calculation_226305922411905025]", "integer"]
        # [gold_physical_col, original_tableau_calc_internal_id, local_type]
      ]
    }
  ]
}

Datasources NOT listed in config are left untouched (Glossary, Parameters, etc.).
------------------------------------------------------------------------------

Usage:
    python reconstruct.py INPUT.twbx --config config.json [-o OUTPUT.twbx]
    python reconstruct.py INPUT.twbx --config config.json --template TEMPLATE.twbx
    python reconstruct.py INPUT.twbx --emit-config            # print a config skeleton

The optional --template is a Snowflake-connected .twbx whose metadata-records are
cloned for authentic per-local-type record shape. If omitted, the input workbook's
own records are used as templates (Tableau reconciles remote-type on refresh).
"""

import argparse
import copy
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile


# ---- default Snowflake connection (override via config["connection"]) --------
DEFAULT_CONNECTION = {
    'server': 'dj-cdl_prod_us_east_1.snowflakecomputing.com',
    'dbname': 'SANDBOX',
    'schema': 'B2B',
    'warehouse': 'B2B_S_WH',
    'role': 'B2B_ANALYST_PRIVILEGED',
    'authentication': 'oauth',
}


# ---- .twb IO -----------------------------------------------------------------
def read_twb(twbx_path):
    """Return (twb_entry_name, raw_text) for the .twb inside a .twbx."""
    with zipfile.ZipFile(twbx_path) as z:
        names = [n for n in z.namelist() if n.endswith('.twb')]
        if not names:
            raise RuntimeError(f'No .twb found inside {twbx_path}')
        return names[0], z.read(names[0]).decode('utf-8')


# ---- introspection -----------------------------------------------------------
SQL_CONNECTION_CLASSES = {'athena', 'presto', 'snowflake', 'redshift',
                          'sqlserver', 'postgres', 'bigquery'}


def _inner_connection_class(ds):
    """Return the first named-connection inner class for a datasource, or None."""
    conn = ds.find('connection')
    if conn is None:
        return None
    for nc in conn.iter('named-connection'):
        inner = nc.find('connection')
        if inner is not None and inner.get('class'):
            return inner.get('class')
    return None


def describe_workbook(twbx_path):
    """Return a list of per-datasource descriptors for inspection / config skeleton."""
    _, raw = read_twb(twbx_path)
    root = ET.fromstring(raw)
    out = []
    dss = root.find('.//datasources')
    if dss is None:
        return out
    for ds in dss.findall('datasource'):
        conn = ds.find('connection')
        cls = _inner_connection_class(ds)
        base_cols, calc_ids = [], []
        if conn is not None:
            mrs = conn.find('metadata-records')
            if mrs is not None:
                for r in mrs.findall('metadata-record'):
                    if r.get('class') == 'column':
                        base_cols.append((r.findtext('remote-name'),
                                          r.findtext('local-name'),
                                          r.findtext('local-type', 'string')))
        for col in ds.findall('column'):
            c = col.find('calculation')
            if c is not None and c.get('class') == 'tableau':
                calc_ids.append((col.get('name'), col.get('caption', '')))
        dp = ds.find('drill-paths')
        out.append({
            'name': ds.get('name'),
            'caption': ds.get('caption'),
            'connection_class': cls,
            'is_custom_sql': any(r.get('type') == 'text' for r in ds.iter('relation')),
            'base_columns': base_cols,
            'calc_fields': calc_ids,
            'drill_paths': [d.get('name') for d in dp.findall('drill-path')] if dp is not None else [],
        })
    return out


def emit_config_skeleton(twbx_path):
    """Print a starter config JSON listing SQL-backed custom-SQL datasources."""
    desc = describe_workbook(twbx_path)
    datasources = []
    for d in desc:
        if not d['is_custom_sql']:
            continue
        if d['connection_class'] not in SQL_CONNECTION_CLASSES:
            continue
        datasources.append({
            'match': d['caption'],
            'view': 'TODO_VIEW_NAME',
            'casing': 'upper',
            '_base_columns_detected': [c[0] for c in d['base_columns']],
            '_calc_fields_available': [
                {'id': cid, 'caption': cap} for cid, cap in d['calc_fields']],
            'calc_bindings': [],
        })
    cfg = {'connection': DEFAULT_CONNECTION, 'datasources': datasources}
    print(json.dumps(cfg, indent=2))


# ---- binding derivation ------------------------------------------------------
def apply_casing(name, casing):
    if casing == 'upper':
        return name.upper()
    if casing == 'lower':
        return name.lower()
    return name  # 'exact'


def derive_bindings(ds, ds_cfg):
    """Ordered binding list. Base bindings auto-derived from the existing
    connection-level metadata-records (local-name + local-type preserved;
    remote-name = casing(original local-name) to match the gold physical column).
    Calc bindings appended from config."""
    casing = ds_cfg.get('casing', 'upper')
    conn = ds.find('connection')
    mrs = conn.find('metadata-records') if conn is not None else None
    bindings = []
    if mrs is not None:
        for rec in mrs.findall('metadata-record'):
            if rec.get('class') != 'column':
                continue
            remote = rec.findtext('remote-name')
            local = rec.findtext('local-name')
            ltype = rec.findtext('local-type', 'string')
            if not remote or not local:
                continue
            bindings.append({
                'remote_name': apply_casing(remote, casing),
                'local_name': local,
                'local_type': ltype,
                'is_calc': False,
            })
    for entry in ds_cfg.get('calc_bindings', []):
        gold_col, calc_id = entry[0], entry[1]
        ltype = entry[2] if len(entry) > 2 else 'string'
        bindings.append({
            'remote_name': gold_col,
            'local_name': calc_id,
            'local_type': ltype,
            'is_calc': True,
        })
    return bindings


# ---- metadata-record templates ----------------------------------------------
def harvest_templates(input_twbx, template_twbx=None):
    """Grab one full <metadata-record> per local-type. Prefer Snowflake-shaped
    records from template_twbx; backfill any missing types from the input."""
    templates = {}
    if template_twbx and os.path.exists(template_twbx):
        _, raw = read_twb(template_twbx)
        root = ET.fromstring(raw)
        for ds in root.find('.//datasources').findall('datasource'):
            conn = ds.find('connection')
            if conn is None:
                continue
            inner = conn.find('.//connection[@class]')
            if inner is None or inner.get('class') != 'snowflake':
                continue
            mrs = conn.find('metadata-records')
            if mrs is None:
                continue
            for rec in mrs.findall('metadata-record'):
                if rec.get('class') == 'column':
                    lt = rec.findtext('local-type', 'string')
                    templates.setdefault(lt, copy.deepcopy(rec))
    # backfill from input workbook
    _, raw_in = read_twb(input_twbx)
    root_in = ET.fromstring(raw_in)
    for rec in root_in.iter('metadata-record'):
        if rec.get('class') == 'column':
            lt = rec.findtext('local-type', 'string')
            templates.setdefault(lt, copy.deepcopy(rec))
    if 'string' not in templates:
        raise RuntimeError('No string metadata-record template available')
    return templates


def _set_child(rec, tag, text):
    el = rec.find(tag)
    if el is not None:
        el.text = text


def make_record(templates, ordinal, remote_name, local_name, local_type, parent):
    tmpl = templates[local_type] if local_type in templates else templates['string']
    rec = copy.deepcopy(tmpl)
    _set_child(rec, 'remote-name', remote_name)
    _set_child(rec, 'remote-alias', remote_name)
    _set_child(rec, 'local-name', local_name)
    _set_child(rec, 'parent-name', parent)
    _set_child(rec, 'ordinal', str(ordinal))
    _set_child(rec, 'local-type', local_type)
    return rec


# ---- connection swap ---------------------------------------------------------
def make_snowflake_named_connection(named_conn, new_conn_name, conn_cfg):
    """Replace the inner SQL <connection> with a snowflake one; rename the
    named-connection. Mutates named_conn in place."""
    named_conn.set('caption', conn_cfg['server'])
    named_conn.set('name', new_conn_name)
    for child in list(named_conn):
        named_conn.remove(child)
    sf = ET.SubElement(named_conn, 'connection')
    sf.set('authentication', conn_cfg.get('authentication', 'oauth'))
    sf.set('class', 'snowflake')
    sf.set('dbname', conn_cfg['dbname'])
    sf.set('instanceurl', f"https://{conn_cfg['server']}")
    sf.set('oauth-config-id', 'default')
    sf.set('odbc-connect-string-extras', '')
    sf.set('one-time-sql', '')
    sf.set('role', conn_cfg['role'])
    sf.set('schema', conn_cfg['schema'])
    sf.set('server', conn_cfg['server'])
    sf.set('service', conn_cfg['role'])
    sf.set('warehouse', conn_cfg['warehouse'])


# ---- core transform ----------------------------------------------------------
def transform_datasource(ds, ds_cfg, conn_cfg, templates, report):
    fed_name = ds.get('name')
    view = ds_cfg['view']
    sf_table = f"[{conn_cfg['dbname']}].[{conn_cfg['schema']}].[{view}]"
    conn = ds.find('connection')

    bindings = derive_bindings(ds, ds_cfg)

    # (1) connection swap: find the source SQL named-connection (non-snowflake)
    old_conn_name = new_conn_name = old_class = None
    for nc in conn.iter('named-connection'):
        inner = nc.find('connection')
        if inner is not None and inner.get('class') in SQL_CONNECTION_CLASSES \
                and inner.get('class') != 'snowflake':
            old_conn_name = nc.get('name')
            old_class = inner.get('class')
            new_conn_name = old_conn_name.replace(f'{old_class}.', 'snowflake.', 1) \
                if old_conn_name.startswith(f'{old_class}.') \
                else f'snowflake.{old_conn_name}'
            make_snowflake_named_connection(nc, new_conn_name, conn_cfg)
            break
    if old_conn_name is None:
        raise RuntimeError(f'{fed_name}: no source SQL named-connection found')

    # (2) repoint every connection ref + convert text relations to table relations
    relation_name = None
    relations_converted = 0
    for el in ds.iter():
        if el.get('connection') == old_conn_name:
            el.set('connection', new_conn_name)
        if el.tag == 'relation' and el.get('type') == 'text':
            if relation_name is None:
                relation_name = el.get('name')
            el.set('type', 'table')
            el.set('table', sf_table)
            el.text = None
            for sub in list(el):
                el.remove(sub)
            relations_converted += 1
    parent_name = f'[{relation_name}]'

    # (3) rebuild connection-level metadata-records
    mrs = conn.find('metadata-records')
    if mrs is None:
        mrs = ET.SubElement(conn, 'metadata-records')
    orig_count = len([r for r in mrs.findall('metadata-record')
                      if r.get('class') == 'column'])
    for r in list(mrs):
        mrs.remove(r)
    for i, b in enumerate(bindings, start=1):
        mrs.append(make_record(templates, i, b['remote_name'], b['local_name'],
                               b['local_type'], parent_name))

    # (4) neutralize materialized calcs at datasource level
    calc_ids = {b['local_name'] for b in bindings if b['is_calc']}
    calcs_neutralized = []
    for col in ds.findall('column'):
        if col.get('name') in calc_ids:
            calc = col.find('calculation')
            if calc is not None:
                col.remove(calc)
                calcs_neutralized.append(col.get('name'))

    # (5) strip extract: <extract> element + dangling object-graph extract relation
    extracts_removed = 0
    for ext in ds.findall('extract'):
        ds.remove(ext)
        extracts_removed += 1
    extract_props_removed = 0
    og = ds.find('object-graph')
    if og is not None:
        for obj in og.iter('object'):
            for props in obj.findall('properties'):
                if props.get('context') == 'extract':
                    obj.remove(props)
                    extract_props_removed += 1

    missing = calc_ids - set(calcs_neutralized)
    if missing:
        raise RuntimeError(f'{fed_name}: calc ids not found as columns: {missing}')

    report.append({
        'datasource': fed_name,
        'caption': ds.get('caption'),
        'view': view,
        'sf_table': sf_table,
        'casing': ds_cfg.get('casing', 'upper'),
        'old_class': old_class,
        'old_conn': old_conn_name,
        'new_conn': new_conn_name,
        'relation_name': relation_name,
        'relations_converted': relations_converted,
        'metadata_records_before': orig_count,
        'metadata_records_after': len(bindings),
        'base_records': len(bindings) - len(calc_ids),
        'calc_records': len(calc_ids),
        'calcs_neutralized': sorted(calcs_neutralized),
        'extracts_removed': extracts_removed,
        'extract_props_removed': extract_props_removed,
        'calc_ids': calc_ids,
    })


def count_drill_paths(ds):
    dp = ds.find('drill-paths')
    return len(dp.findall('drill-path')) if dp is not None else 0


def neutralize_worksheet_calc_copies(root, fed_name, calc_ids):
    """Strip <calculation> from worksheet <datasource-dependencies> copies of the
    materialized calcs, so they don't collide with the now-physical column on load
    ('field is already defined by data source')."""
    stripped = 0
    for ws in root.findall('.//worksheet'):
        for dd in ws.findall('.//datasource-dependencies'):
            if dd.get('datasource') != fed_name:
                continue
            for col in dd.findall('column'):
                if col.get('name') in calc_ids:
                    calc = col.find('calculation')
                    if calc is not None:
                        col.remove(calc)
                        stripped += 1
    return stripped


def _match_datasource(ds, match):
    """A config 'match' resolves against caption first, then federated name."""
    return ds.get('caption') == match or ds.get('name') == match


# ---- orchestration -----------------------------------------------------------
def reconstruct(input_twbx, config, output_twbx, template_twbx=None, verbose=True):
    conn_cfg = {**DEFAULT_CONNECTION, **config.get('connection', {})}
    ds_configs = config['datasources']

    templates = harvest_templates(input_twbx, template_twbx)
    if verbose:
        print(f'Harvested metadata-record templates: {sorted(templates)}')

    twb_name, raw = read_twb(input_twbx)
    prefix = raw[:raw.index('<workbook')]   # preserve XML decl + build comment
    root = ET.fromstring(raw)
    datasources = root.find('.//datasources')

    # resolve config matches to datasource elements
    matched = []
    for ds_cfg in ds_configs:
        match = ds_cfg.get('match') or ds_cfg.get('name')
        hits = [ds for ds in datasources.findall('datasource') if _match_datasource(ds, match)]
        if not hits:
            raise RuntimeError(f"config datasource not found in workbook: {match!r}")
        if len(hits) > 1:
            raise RuntimeError(f"config datasource ambiguous (matched {len(hits)}): {match!r}")
        matched.append((hits[0], ds_cfg))

    report = []
    drill_before = {}
    for ds, ds_cfg in matched:
        drill_before[ds.get('name')] = count_drill_paths(ds)
        transform_datasource(ds, ds_cfg, conn_cfg, templates, report)

    for r in report:
        r['ws_calc_copies_stripped'] = (
            neutralize_worksheet_calc_copies(root, r['datasource'], r['calc_ids'])
            if r['calc_ids'] else 0)

    # invariant: drill-paths preserved
    for ds, _ in matched:
        fed = ds.get('name')
        after = count_drill_paths(ds)
        assert after == drill_before[fed], \
            f'{fed}: drill-paths changed {drill_before[fed]}->{after}'

    new_twb = prefix + ET.tostring(root, encoding='unicode')

    os.makedirs(os.path.dirname(os.path.abspath(output_twbx)), exist_ok=True)
    dropped = []
    with zipfile.ZipFile(input_twbx) as zin, \
            zipfile.ZipFile(output_twbx, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            if item.endswith('.hyper'):
                dropped.append(item)
                continue
            zout.writestr(item, new_twb if item == twb_name else zin.read(item))

    if verbose:
        _print_report(report, dropped, output_twbx)
    return {'report': report, 'dropped': dropped, 'output': output_twbx,
            'connection': conn_cfg}


def _print_report(report, dropped, out_twbx):
    print(f'\nWrote: {out_twbx}')
    if dropped:
        print('Dropped extract files:')
        for d in dropped:
            print('  -', d)
    print('\n=== RECONSTRUCTION REPORT ===')
    for r in report:
        print(f"\n[{r['caption']}]  ({r['datasource']})")
        print(f"  connection : {r['old_class']} -> snowflake  ({r['old_conn']} -> {r['new_conn']})")
        print(f"  relation   : text SQL -> table {r['sf_table']}  (name kept: {r['relation_name']}, {r['relations_converted']} relation(s))")
        print(f"  casing     : {r['casing']}")
        print(f"  metadata   : {r['metadata_records_before']} -> {r['metadata_records_after']} ({r['base_records']} base + {r['calc_records']} calc)")
        print(f"  calcs neutralized ({len(r['calcs_neutralized'])}): {r['calcs_neutralized']}")
        print(f"  worksheet calc-copies neutralized: {r.get('ws_calc_copies_stripped', 0)}")
        print(f"  extract removed: {r['extracts_removed']} element(s), {r['extract_props_removed']} object-graph relation(s)")


def write_notes(result, notes_path, drill_label=None):
    report, dropped = result['report'], result['dropped']
    lines = ['# Source Swap reconstruction notes (Path A)\n']
    lines.append('Generated by `Reconstructor/reconstruct.py`. In-place datasource swap '
                 '(Athena custom SQL -> Snowflake view), preserving all captions, '
                 'drill-down hierarchies, calculated fields, and worksheet bindings.\n')
    lines.append(f'Output: `{os.path.basename(result["output"])}`\n')
    lines.append('## Per-datasource changes\n')
    for r in report:
        lines.append(f'### {r["caption"]}\n')
        lines.append(f'- Datasource (unchanged identity): `{r["datasource"]}`')
        lines.append(f'- Connection: `{r["old_class"]}` -> `snowflake`')
        lines.append(f'- Relation: text custom-SQL -> table `{r["sf_table"]}` (relation name kept as `{r["relation_name"]}`)')
        lines.append(f'- Base casing: `{r["casing"]}`')
        lines.append(f'- Metadata-records: {r["metadata_records_before"]} -> {r["metadata_records_after"]} ({r["base_records"]} base + {r["calc_records"]} calc)')
        lines.append(f'- Calc fields neutralized: {len(r["calcs_neutralized"])} datasource-level + {r.get("ws_calc_copies_stripped", 0)} worksheet copies')
        for c in r['calcs_neutralized']:
            lines.append(f'    - `{c}`')
        lines.append('')
    lines.append('## Extract files dropped (now a live Snowflake connection)\n')
    for d in dropped:
        lines.append(f'- `{d}`')
    lines.append('\n## Manual step: re-materialize the extract (optional)\n')
    lines.append('The workbook now connects **live** to Snowflake. To restore extract '
                 'performance, in Tableau Desktop: open (sign in via OAuth) -> for each '
                 'Snowflake datasource, Data menu -> [datasource] -> Extract Data... -> '
                 'Extract -> Save.\n')
    lines.append('## Verification checklist (Tableau open test)\n')
    lines.append('- [ ] All worksheets render with no "field is missing" warnings.')
    if drill_label:
        lines.append(f'- [ ] All drill-down hierarchies present: {drill_label}.')
    lines.append('- [ ] All calculated fields present (non-materialized calcs stay as Tableau calcs).')
    lines.append('- [ ] Visuals match the original Athena-backed workbook.')
    with open(notes_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return notes_path


# ---- CLI ---------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description='Workbook-agnostic Path A reconstructor.')
    p.add_argument('input', help='input .twbx (Athena, extract-backed)')
    p.add_argument('--config', help='config JSON file')
    p.add_argument('--template', help='Snowflake-connected .twbx for metadata-record shapes')
    p.add_argument('-o', '--output', help='output .twbx path')
    p.add_argument('--emit-config', action='store_true',
                   help='print a config skeleton for the input workbook and exit')
    p.add_argument('--notes', help='also write a RECONSTRUCTION_NOTES.md to this path')
    args = p.parse_args(argv)

    if args.emit_config:
        emit_config_skeleton(args.input)
        return 0

    if not args.config:
        p.error('--config is required (or use --emit-config)')

    with open(args.config, 'r', encoding='utf-8') as f:
        config = json.load(f)
    # strip helper keys the skeleton emits
    for ds in config.get('datasources', []):
        ds.pop('_base_columns_detected', None)
        ds.pop('_calc_fields_available', None)

    output = args.output
    if not output:
        base = os.path.splitext(os.path.basename(args.input))[0]
        output = os.path.join(os.path.dirname(os.path.abspath(args.input)),
                              f'{base} - Source Swap.twbx')

    result = reconstruct(args.input, config, output, template_twbx=args.template)
    if args.notes:
        write_notes(result, args.notes)
        print(f'\nWrote notes: {args.notes}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
