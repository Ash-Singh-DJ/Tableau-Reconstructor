"""
verify_output.py -- static verification of a reconstructed (.twbx) source swap.

Workbook-agnostic: drive it with the SAME config JSON used for the source swap
(reconstruct.py --config), and it derives the expected per-datasource targets from
that config. For each target datasource it checks:

  - inner connection class is `snowflake` (no athena/presto left)
  - no leftover `type="text"` (custom-SQL) relations in the target datasource
  - relations point at the configured gold view  [DB].[SCHEMA].[VIEW]
  - connection-level metadata-records == expected count (base auto-derived + calc)
  - drill-paths count unchanged (pass --drill EXPECTED per datasource to assert)
  - no worksheet-level <calculation> collisions remain for materialized calc ids

Plus global checks: zero `.hyper` entries and zero `athena` substring anywhere.

Usage:
    python verify_output.py OUTPUT.twbx --config config.json
    python verify_output.py OUTPUT.twbx --config config.json --input ORIGINAL.twbx
      # --input lets it auto-derive the expected base metadata-record count per
      #   datasource from the source workbook (base cols + len(calc_bindings)).

Exit code 0 if all checks pass, 1 otherwise.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tableau_doc import datasource_elements, ds_label, matches as _match


def _read_doc(path):
    """Return (member_name, raw_text, all_zip_names) for the .twb/.tds bundle."""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.endswith(('.twb', '.tds'))]
        if not names:
            raise RuntimeError(f'No .twb or .tds found inside {path}')
        return names[0], z.read(names[0]).decode('utf-8'), z.namelist()


def _base_col_count(ds):
    conn = ds.find('connection')
    if conn is None:
        return 0
    mrs = conn.find('metadata-records')
    if mrs is None:
        return 0
    return sum(1 for r in mrs.findall('metadata-record') if r.get('class') == 'column')


def verify(output_twbx, config, input_twbx=None):
    conn_cfg = config.get('connection', {})
    db = conn_cfg.get('dbname', 'SANDBOX')
    schema = conn_cfg.get('schema', 'B2B')

    # expected base metadata-record count per datasource (from the ORIGINAL workbook)
    base_counts = {}
    if input_twbx:
        _, raw_in, _ = _read_doc(input_twbx)
        root_in = ET.fromstring(raw_in)
        for ds in datasource_elements(root_in):
            base_counts[ds.get('name')] = _base_col_count(ds)
            label = ds_label(ds)
            if label:
                base_counts[label] = _base_col_count(ds)

    twb, raw, names = _read_doc(output_twbx)
    hypers = [n for n in names if n.endswith('.hyper')]
    print(f"ZIP entries: {len(names)} | .hyper files: {len(hypers)}  "
          f"-> {'OK (none)' if not hypers else 'FAIL'}")
    n_athena = raw.count('athena')
    print(f"'athena' substring anywhere in twb: {n_athena}  "
          f"-> {'OK' if n_athena == 0 else 'CHECK'}")

    root = ET.fromstring(raw)
    all_datasources = datasource_elements(root)
    ok = (not hypers) and (n_athena == 0)

    for ds_cfg in config['datasources']:
        match = ds_cfg.get('match') or ds_cfg.get('name')
        hits = [ds for ds in all_datasources if _match(ds, match)]
        if not hits:
            print(f"\n[{match}]  -> FAIL (not found in output)")
            ok = False
            continue
        ds = hits[0]
        view = ds_cfg['view']
        sf_table = f'[{db}].[{schema}].[{view}]'

        conn = ds.find('connection')
        classes = [nc.find('connection').get('class')
                   for nc in conn.iter('named-connection')
                   if nc.find('connection') is not None]
        rels = [(r.get('type'), r.get('table')) for r in ds.iter('relation')]
        text_rels = [r for r in rels if r[0] == 'text']
        view_rels = [r for r in rels if r[1] == sf_table]
        mrs = conn.find('metadata-records')
        mr_cols = ([r for r in mrs.findall('metadata-record') if r.get('class') == 'column']
                   if mrs is not None else [])
        calcs = sum(1 for c in ds.findall('column')
                    if c.find('calculation') is not None
                    and c.find('calculation').get('class') == 'tableau')
        dp = ds.find('drill-paths')
        ndp = len(dp.findall('drill-path')) if dp is not None else 0

        n_calc = len(ds_cfg.get('calc_bindings', []))
        exp_mr = None
        if match in base_counts:
            exp_mr = base_counts[match] + n_calc

        print(f"\n[{ds_label(ds)}]  ({ds.get('name')})")
        cls_ok = bool(classes) and all(c == 'snowflake' for c in classes)
        print(f"  conn classes: {classes}  -> {'OK' if cls_ok else 'FAIL'}")
        print(f"  text relations remaining: {len(text_rels)}  "
              f"-> {'OK' if not text_rels else 'FAIL'}")
        print(f"  relations pointing at {view}: {len(view_rels)}  "
              f"-> {'OK' if view_rels else 'FAIL'}")
        if exp_mr is not None:
            print(f"  metadata-records: {len(mr_cols)} (expected {exp_mr})  "
                  f"-> {'OK' if len(mr_cols) == exp_mr else 'FAIL'}")
        else:
            print(f"  metadata-records: {len(mr_cols)} ({len(mr_cols) - n_calc} base "
                  f"+ {n_calc} calc)  -> (no --input; count not asserted)")
        exp_dp = ds_cfg.get('_expected_drill_paths')
        if exp_dp is not None:
            print(f"  drill-paths: {ndp} (expected {exp_dp})  "
                  f"-> {'OK' if ndp == exp_dp else 'FAIL'}")
        else:
            print(f"  drill-paths: {ndp}")
        print(f"  remaining datasource-level tableau calcs: {calcs}")

        # worksheet-level calc collisions for the materialized ids
        calc_ids = {b[1] for b in ds_cfg.get('calc_bindings', [])}
        leftover = 0
        for ws in root.findall('.//worksheet'):
            for dd in ws.findall('.//datasource-dependencies'):
                if dd.get('datasource') != ds.get('name'):
                    continue
                for col in dd.findall('column'):
                    if col.get('name') in calc_ids and col.find('calculation') is not None:
                        leftover += 1
        print(f"  worksheet calc collisions for materialized ids: {leftover}  "
              f"-> {'OK' if leftover == 0 else 'FAIL'}")

        if not cls_ok or text_rels or not view_rels or leftover:
            ok = False
        if exp_mr is not None and len(mr_cols) != exp_mr:
            ok = False
        if exp_dp is not None and ndp != exp_dp:
            ok = False

    print(f"\n=== {'ALL STATIC CHECKS PASS' if ok else 'SOME CHECKS FAILED'} ===")
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(description='Static verification of a source-swap .twbx.')
    p.add_argument('output', help='reconstructed output .twbx')
    p.add_argument('--config', required=True, help='the reconstruct config JSON used for the swap')
    p.add_argument('--input', help='original .twbx (lets it assert metadata-record counts)')
    args = p.parse_args(argv)

    with open(args.config, 'r', encoding='utf-8') as f:
        config = json.load(f)
    ok = verify(args.output, config, input_twbx=args.input)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
