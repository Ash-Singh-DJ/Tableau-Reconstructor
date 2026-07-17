"""
embed_collapse.py -- collapse a published-datasource .twbx + its side-car .tdsx
files into ONE workbook whose data sources are EMBEDDED and Snowflake-backed.

THE TOPOLOGY THIS HANDLES (distinct from reconstruct.py's two cases). Some .twbx
workbooks don't embed their Athena connection at all -- their data datasources are
`class="sqlproxy"` references to *published* data sources on Tableau Server
(<repository-location> derived-from a server URL). The proxy <connection> is a stub
(<relation table="[sqlproxy]">) carrying only a pruned mirror of the fields the
workbook uses; the real Athena Custom SQL + full schema live in a separate .tdsx
file per published source. reconstruct.py alone refuses such a .twbx -- there is no
source SQL connection inside it to swap.

WHAT THIS DOES. Given the .twbx and the side-car .tdsx for each sqlproxy datasource:
  1. Source-swap each .tdsx to Snowflake with the EXISTING engine (reconstruct.py) --
     no dialect logic is duplicated here; this module never touches a database.
  2. Match each workbook sqlproxy datasource to its swapped .tds (by the caller's
     explicit mapping) and VERIFY the pairing with a field fingerprint: the calc-id
     sets must match and every base column the workbook references must exist in the
     .tds. A mismatch is a hard stop (a wrong pairing cannot silently pass).
  3. GRAFT the swapped .tds's physical layer onto the proxy datasource in place:
     strip <repository-location>, replace <connection> and <object-graph> with the
     Snowflake ones, and KEEP EVERYTHING ELSE -- crucially the federated `name`
     (sqlproxy.xxxxx), so every worksheet binding and calc alias (which key on that
     opaque name and on calc ids) stays valid with ZERO worksheet rewrites. The proxy
     already holds the workbook-local logical layer (columns, calcs, aliases, groups,
     layout, column-instances, dependencies); the .tds schema is a SUPERSET of what
     the proxy exposed, so all referenced fields resolve against the gold view.
  4. Drop the orphaned local extracts (the collapsed workbook connects live) and
     write a single .twbx with the data sources embedded.

WHY THE GRAFT IS SAFE. Tableau binds worksheet fields by internal field name under
the datasource's federated `name`; that name and the logical layer never move. Only
the physical layer (how those names reach a database) is replaced. This is the same
principle as reconstruct.py Path A, applied across the workbook/side-car seam.

------------------------------------------------------------------------------
CONFIG SCHEMA (dict, or JSON file via --config):
{
  "connection": { ...Snowflake conn, same shape as reconstruct.py... },
  "datasources": [
    {
      "match":   "B2B Factiva Daily Counter Report Usage",  # proxy caption OR name
      "tdsx":    "Inputs/... - Usage.tdsx",                  # the side-car for it
      "view":    "RECONSTRUCTOR_FACTIVA_USAGE",              # gold view in dbname.schema
      "casing":  "upper",                                    # how the view names cols
      "calc_bindings": []          # usually EMPTY in collapse mode: calcs stay as
                                   #   Tableau calcs in the workbook (the logical
                                   #   layer is preserved), so the view need only
                                   #   expose base columns. Provide bindings only if
                                   #   you intend to materialize a calc into the view.
      # "column_overrides": {...}  # optional, forwarded to reconstruct.py
    }
  ]
}
Datasources NOT listed (Parameters, live sources) are left untouched.
------------------------------------------------------------------------------

Usage:
    python embed_collapse.py INPUT.twbx --config config.json [-o OUTPUT.twbx]
    python embed_collapse.py INPUT.twbx --inventory     # list sqlproxy datasources

Matching of proxy<->tdsx is the CALLER's job (an Opus model in the source-swap
skill resolves it by repository-location id / fuzzy caption and gets user sign-off);
this engine only verifies the mapping it is given.
"""

import argparse
import copy
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tableau_doc import (read_doc, split_prefix, datasource_elements, ds_label,
                         matches as ds_matches, default_output_path)
import reconstruct


EXTRACT_PREFIX = 'Data/Extracts/'   # local extract shadow files (may lack .hyper ext)

# Physical-only <column> attributes that a *published* (sqlproxy) source stamps onto
# EVERY field it mirrors -- calcs included -- because the server flattened the whole
# result set into resolved physical/pivot columns. `pivot="key"` + `user-datatype` are
# the signature Tableau reads as "this is a database column"; left on a calc column
# after we sever the server link, Tableau renders the calc as a plain Database column
# (its formula becomes inert) instead of a Calculated Field -- regardless of whether
# the calc is materializable. A federated-native calc (and the canonical .tdsx calc)
# carries only the clean logical set {caption, datatype, name, role, type}. Stripping
# these from calc columns restores calc rendering; base columns are left untouched.
PHYSICAL_ONLY_CALC_ATTRS = ('pivot', 'user-datatype', 'default-type', 'layered',
                            'visual-totals', 'aggregation')


# ---- introspection -----------------------------------------------------------
def _conn_local_names(ds):
    """local-names exposed by a datasource's connection-level metadata-records."""
    out = set()
    conn = ds.find('connection')
    mrs = conn.find('metadata-records') if conn is not None else None
    if mrs is None:
        return out
    for r in mrs.findall('metadata-record'):
        if r.get('class') == 'column':
            ln = r.findtext('local-name')
            if ln:
                out.add(ln)
    return out


def _calc_col_names(ds):
    """[Calculation_*] (and any calc) column names defined on the datasource."""
    return {c.get('name') for c in ds.findall('column')
            if c.find('calculation') is not None}


def _referenced_locals(root, fed_name):
    """Every [field] any worksheet references for this datasource."""
    refs = set()
    for ws in root.findall('.//worksheet'):
        for dd in ws.findall('.//datasource-dependencies'):
            if dd.get('datasource') != fed_name:
                continue
            for col in dd.findall('column'):
                if col.get('name'):
                    refs.add(col.get('name'))
            for ci in dd.findall('column-instance'):
                if ci.get('column'):
                    refs.add(ci.get('column'))
    return refs


def _repository_id(ds):
    rl = ds.find('repository-location')
    return rl.get('id') if rl is not None else None


def _is_sqlproxy(ds):
    conn = ds.find('connection')
    return conn is not None and conn.get('class') == 'sqlproxy'


def inventory(twbx_path):
    """List the workbook's sqlproxy (published-datasource) datasources, with the
    fingerprint fields a caller needs to match each to its side-car .tdsx."""
    _, raw = read_doc(twbx_path)
    root = ET.fromstring(raw)
    out = []
    for ds in datasource_elements(root):
        if not _is_sqlproxy(ds):
            continue
        out.append({
            'name': ds.get('name'),
            'caption': ds.get('caption'),
            'repository_id': _repository_id(ds),
            'calc_ids': sorted(_calc_col_names(ds)),
            'referenced_base_locals': sorted(
                _referenced_locals(root, ds.get('name')) - _calc_col_names(ds)),
        })
    return out


# ---- side-car swap (delegates to reconstruct.py) -----------------------------
def _swap_tdsx(tdsx_path, conn_cfg, ds_cfg, workdir):
    """Source-swap one side-car .tdsx to Snowflake via reconstruct.py, returning the
    parsed swapped <datasource> root. reconstruct matches its lone datasource by
    formatted-name; we pass that through from ds_cfg (default: the tds's own)."""
    _, raw = read_doc(tdsx_path)
    tds_root = ET.fromstring(raw)
    formatted = tds_root.get('formatted-name')

    sub = {
        'match': ds_cfg.get('tdsx_match', formatted),
        'view': ds_cfg['view'],
        'casing': ds_cfg.get('casing', 'upper'),
        'calc_bindings': ds_cfg.get('calc_bindings', []),
    }
    if 'column_overrides' in ds_cfg:
        sub['column_overrides'] = ds_cfg['column_overrides']
    sub_config = {'connection': conn_cfg, 'datasources': [sub]}

    out_tdsx = os.path.join(workdir, os.path.basename(tdsx_path))
    reconstruct.reconstruct(tdsx_path, sub_config, out_tdsx, verbose=False)
    _, swapped_raw = read_doc(out_tdsx)
    return ET.fromstring(swapped_raw)


# ---- fingerprint verification ------------------------------------------------
def _verify_pairing(proxy, swapped_tds, root, match_label):
    """Guardrail against a wrong proxy<->tds pairing. Returns a report dict; raises
    on a hard mismatch. Checks:
      - calc-id sets are equal (worksheets bind calcs by these ids), and
      - every base column the workbook references exists in the swapped tds's
        connection metadata-records (the tds must be a superset of what's used)."""
    proxy_calcs = _calc_col_names(proxy)
    tds_calcs = _calc_col_names(swapped_tds)
    if proxy_calcs != tds_calcs:
        only_p = sorted(proxy_calcs - tds_calcs)
        only_t = sorted(tds_calcs - proxy_calcs)
        raise RuntimeError(
            f"{match_label!r}: calc-field fingerprint mismatch -- this .tdsx is not "
            f"the published source for this datasource.\n"
            f"  only in workbook proxy: {only_p}\n"
            f"  only in .tdsx        : {only_t}")

    referenced = _referenced_locals(root, proxy.get('name'))
    referenced_base = referenced - proxy_calcs
    tds_locals = _conn_local_names(swapped_tds)
    missing = referenced_base - tds_locals
    if missing:
        raise RuntimeError(
            f"{match_label!r}: {len(missing)} worksheet-referenced base column(s) are "
            f"absent from the .tdsx / gold view -- the view would not satisfy the "
            f"workbook: {sorted(missing)}")

    return {
        'calc_ids_matched': len(proxy_calcs),
        'referenced_base': len(referenced_base),
        'tds_base_columns': len(tds_locals),
        'unreferenced_base_in_view': len(tds_locals - referenced_base),
    }


# ---- calc-column shape normalization -----------------------------------------
def _normalize_calc_columns(scope):
    """Strip physical-only attributes from every calc <column> under `scope` (a
    datasource element, or a worksheet's <datasource-dependencies>) so the calc
    renders as a Calculated Field rather than a resolved Database column. A calc
    column is one carrying a <calculation> child. Returns the count normalized."""
    n = 0
    for col in scope.findall('column'):
        if col.find('calculation') is None:
            continue
        touched = False
        for attr in PHYSICAL_ONLY_CALC_ATTRS:
            if attr in col.attrib:
                del col.attrib[attr]
                touched = True
        if touched:
            n += 1
    return n


# ---- the graft ---------------------------------------------------------------
def _graft(proxy, swapped_tds):
    """Replace the proxy's physical layer with the swapped tds's, in place, keeping
    the federated name and the whole logical layer. Returns a small report dict."""
    # (1) strip repository-location (no longer a published reference)
    rl = proxy.find('repository-location')
    if rl is not None:
        proxy.remove(rl)

    # (2) swap <connection>: sqlproxy stub -> federated/snowflake (named-connection,
    #     gold-view table relation, full metadata-records)
    old_conn = proxy.find('connection')
    idx = list(proxy).index(old_conn)
    proxy.remove(old_conn)
    proxy.insert(idx, copy.deepcopy(swapped_tds.find('connection')))

    # (3) swap <object-graph> (its live model mirrors the connection relation)
    old_og = proxy.find('object-graph')
    new_og = swapped_tds.find('object-graph')
    if old_og is not None:
        oidx = list(proxy).index(old_og)
        proxy.remove(old_og)
        if new_og is not None:
            proxy.insert(oidx, copy.deepcopy(new_og))
    elif new_og is not None:
        proxy.append(copy.deepcopy(new_og))

    # (4) normalize calc columns: a published (sqlproxy) source stamps physical/pivot
    #     attributes onto its mirrored calc columns; left in place after the swap they
    #     make Tableau render the calcs as Database columns. Strip them so the calcs
    #     render as Calculated Fields (their formulas + logical layer are untouched).
    calcs_normalized = _normalize_calc_columns(proxy)

    return {
        'repository_location_stripped': rl is not None,
        'new_conn_class': proxy.find('connection').get('class'),
        'metadata_records': len(_conn_local_names(proxy)),
        'calcs_kept': len(_calc_col_names(proxy)),
        'calcs_normalized': calcs_normalized,
    }


# ---- orchestration -----------------------------------------------------------
def collapse(twbx_path, config, output_path, verbose=True):
    conn_cfg = {**reconstruct.DEFAULT_CONNECTION, **config.get('connection', {})}
    ds_configs = config['datasources']

    twb_name, raw = read_doc(twbx_path)
    prefix = split_prefix(raw)
    root = ET.fromstring(raw)
    all_ds = datasource_elements(root)

    report = []
    with tempfile.TemporaryDirectory() as workdir:
        for ds_cfg in ds_configs:
            match = ds_cfg.get('match') or ds_cfg.get('name')
            hits = [d for d in all_ds if ds_matches(d, match)]
            if not hits:
                raise RuntimeError(f"config datasource not found in workbook: {match!r}")
            if len(hits) > 1:
                raise RuntimeError(f"config datasource ambiguous (matched {len(hits)}): {match!r}")
            proxy = hits[0]
            if not _is_sqlproxy(proxy):
                raise RuntimeError(
                    f"{match!r}: not a sqlproxy datasource (class="
                    f"{proxy.find('connection').get('class')!r}) -- use reconstruct.py "
                    f"for embedded-connection datasources.")

            tdsx = ds_cfg['tdsx']
            if not os.path.exists(tdsx):
                raise RuntimeError(f"{match!r}: side-car .tdsx not found: {tdsx!r}")

            swapped = _swap_tdsx(tdsx, conn_cfg, ds_cfg, workdir)
            fp = _verify_pairing(proxy, swapped, root, match)
            graft_rep = _graft(proxy, swapped)

            # worksheets carry their own copies of each calc column inside
            # <datasource-dependencies>; they bear the same physical/pivot stamp and
            # must be normalized too, or the calc renders as a Database column on the
            # sheet even though the datasource-level column is clean.
            fed_name = proxy.get('name')
            ws_norm = 0
            for ws in root.findall('.//worksheet'):
                for dd in ws.findall('.//datasource-dependencies'):
                    if dd.get('datasource') == fed_name:
                        ws_norm += _normalize_calc_columns(dd)
            graft_rep['worksheet_calc_copies_normalized'] = ws_norm

            report.append({
                'match': match,
                'name': proxy.get('name'),
                'caption': ds_label(proxy),
                'tdsx': os.path.basename(tdsx),
                'view': ds_cfg['view'],
                'fingerprint': fp,
                'graft': graft_rep,
            })

    new_twb = prefix + ET.tostring(root, encoding='unicode')

    # write output: embed the twb, drop orphaned local extracts + any .hyper
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    dropped = []
    with zipfile.ZipFile(twbx_path) as zin, \
            zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            if item.startswith(EXTRACT_PREFIX) or item.endswith('.hyper'):
                dropped.append(item)
                continue
            zout.writestr(item, new_twb if item == twb_name else zin.read(item))

    if verbose:
        _print_report(report, dropped, output_path)
    return {'report': report, 'dropped': dropped, 'output': output_path,
            'connection': conn_cfg}


def _print_report(report, dropped, out_path):
    print(f'\nWrote: {out_path}')
    if dropped:
        print(f'Dropped {len(dropped)} orphaned extract file(s):')
        for d in dropped:
            print('  -', d)
    print('\n=== EMBED-COLLAPSE REPORT ===')
    for r in report:
        fp, g = r['fingerprint'], r['graft']
        print(f"\n[{r['caption']}]  ({r['name']})")
        print(f"  side-car   : {r['tdsx']}  ->  view {r['view']}")
        print(f"  fingerprint: {fp['calc_ids_matched']} calc ids matched; "
              f"{fp['referenced_base']} referenced base cols all present "
              f"({fp['tds_base_columns']} in view, "
              f"{fp['unreferenced_base_in_view']} extra)")
        print(f"  graft      : connection sqlproxy -> {g['new_conn_class']}/snowflake; "
              f"repository-location stripped: {g['repository_location_stripped']}")
        print(f"               {g['metadata_records']} metadata-records, "
              f"{g['calcs_kept']} Tableau calcs kept")
        print(f"               {g['calcs_normalized']} calc columns shape-normalized "
              f"(+ {g['worksheet_calc_copies_normalized']} worksheet copies) -> "
              f"render as Calculated Fields")


# ---- static verification -----------------------------------------------------
def verify_collapse(output_twbx, config, verbose=True):
    """Static checks on a collapsed .twbx, driven by the SAME config used to build it.

    Global: 0 `.hyper` entries, 0 orphaned `Data/Extracts/` files, 0 `athena`
    substring anywhere in the twb.

    Per configured (formerly-sqlproxy) datasource:
      - the datasource still exists and its connection class is now `federated`
      - its inner named-connection class is `snowflake` (no sqlproxy/athena left)
      - NO datasource-level <repository-location> remains (published ref severed) --
        workbook/dashboard-level <repository-location> is unrelated and left alone
      - no `[sqlproxy]` stub relation survives
      - relations point at the configured gold view; 0 `type="text"` relations remain
      - RESOLUTION INVARIANT: every worksheet-referenced field for this datasource
        resolves to a connection metadata-record local-name or a kept calc column

    Returns True iff all checks pass."""
    with zipfile.ZipFile(output_twbx) as z:
        names = z.namelist()
        twb = next((n for n in names if n.endswith('.twb')), None)
        if twb is None:
            print('FAIL: no .twb inside the output (a collapsed workbook must embed one)')
            return False
        raw = z.read(twb).decode('utf-8')
    root = ET.fromstring(raw)

    conn_cfg = {**reconstruct.DEFAULT_CONNECTION, **config.get('connection', {})}
    db, schema = conn_cfg['dbname'], conn_cfg['schema']

    hypers = [n for n in names if n.endswith('.hyper')]
    extracts = [n for n in names if n.startswith(EXTRACT_PREFIX)]
    n_athena = raw.count('athena')
    ok = not hypers and not extracts and n_athena == 0
    if verbose:
        print(f".hyper entries          : {len(hypers)}  -> {'OK' if not hypers else 'FAIL'}")
        print(f"Data/Extracts orphans   : {len(extracts)}  -> {'OK' if not extracts else 'FAIL'}")
        print(f"'athena' substring      : {n_athena}  -> {'OK' if n_athena == 0 else 'FAIL'}")

    all_ds = datasource_elements(root)
    for ds_cfg in config['datasources']:
        match = ds_cfg.get('match') or ds_cfg.get('name')
        hits = [d for d in all_ds if ds_matches(d, match)]
        if not hits:
            print(f"\n[{match}]  -> FAIL (not found in output)")
            ok = False
            continue
        ds = hits[0]
        view = ds_cfg['view']
        sf_table = f'[{db}].[{schema}].[{view}]'
        conn = ds.find('connection')

        cls = conn.get('class') if conn is not None else None
        inner = [nc.find('connection').get('class') for nc in conn.iter('named-connection')
                 if nc.find('connection') is not None] if conn is not None else []
        inner_ok = bool(inner) and all(c == 'snowflake' for c in inner)
        has_repo = ds.find('repository-location') is not None
        rels = [(r.get('type'), r.get('table')) for r in ds.iter('relation')]
        text_rels = [r for r in rels if r[0] == 'text']
        view_rels = [r for r in rels if r[1] == sf_table]
        stub = any(r.get('table') == '[sqlproxy]' for r in ds.iter('relation'))

        universe = _conn_local_names(ds) | _calc_col_names(ds)
        referenced = _referenced_locals(root, ds.get('name'))
        unresolved = referenced - universe

        # calc columns must carry the clean (federated-native) shape -- any residual
        # physical/pivot attribute would make Tableau render the calc as a Database
        # column, silently inerting its formula. Check both datasource-level calc
        # columns and worksheet <datasource-dependencies> copies.
        dirty_calcs = [c.get('name') for c in ds.findall('column')
                       if c.find('calculation') is not None
                       and any(a in c.attrib for a in PHYSICAL_ONLY_CALC_ATTRS)]
        for ws in root.findall('.//worksheet'):
            for dd in ws.findall('.//datasource-dependencies'):
                if dd.get('datasource') != ds.get('name'):
                    continue
                for c in dd.findall('column'):
                    if (c.find('calculation') is not None
                            and any(a in c.attrib for a in PHYSICAL_ONLY_CALC_ATTRS)):
                        dirty_calcs.append(c.get('name'))
        calc_shape_ok = not dirty_calcs

        ds_ok = (cls == 'federated' and inner_ok and not has_repo and not text_rels
                 and bool(view_rels) and not stub and not unresolved and calc_shape_ok)
        ok = ok and ds_ok
        if verbose:
            print(f"\n[{ds_label(ds)}]  ({ds.get('name')})")
            print(f"  connection class      : {cls}  -> {'OK' if cls == 'federated' else 'FAIL'}")
            print(f"  inner conn classes    : {inner}  -> {'OK' if inner_ok else 'FAIL'}")
            print(f"  datasource repo-loc   : {'present' if has_repo else 'none'}  "
                  f"-> {'FAIL' if has_repo else 'OK'}")
            print(f"  [sqlproxy] stub relation: {'present' if stub else 'none'}  "
                  f"-> {'FAIL' if stub else 'OK'}")
            print(f"  text relations left   : {len(text_rels)}  -> {'OK' if not text_rels else 'FAIL'}")
            print(f"  relations -> {view}: {len(view_rels)}  -> {'OK' if view_rels else 'FAIL'}")
            print(f"  field resolution      : {len(referenced)} referenced, "
                  f"{len(unresolved)} unresolved  -> {'OK' if not unresolved else 'FAIL'}")
            for u in sorted(unresolved):
                print(f"      !! {u}")
            print(f"  calc column shape     : {len(dirty_calcs)} with physical attrs  "
                  f"-> {'OK' if calc_shape_ok else 'FAIL'}")
            for d in sorted(set(dirty_calcs)):
                print(f"      !! {d} still carries physical/pivot attributes")

    if verbose:
        print(f"\n=== {'ALL STATIC CHECKS PASS' if ok else 'SOME CHECKS FAILED'} ===")
    return ok


# ---- CLI ---------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description='Collapse a sqlproxy .twbx + side-car .tdsx into one embedded '
                    'Snowflake-backed workbook.')
    p.add_argument('input', help='input .twbx with sqlproxy (published) datasources')
    p.add_argument('--config', help='config JSON (proxy<->tdsx mapping + swap config)')
    p.add_argument('-o', '--output', help='output .twbx path')
    p.add_argument('--inventory', action='store_true',
                   help='list the workbook sqlproxy datasources + fingerprints and exit')
    p.add_argument('--verify', action='store_true',
                   help='statically verify INPUT as a collapsed output (needs --config) '
                        'instead of building one')
    args = p.parse_args(argv)

    if args.inventory:
        print(json.dumps(inventory(args.input), indent=2))
        return 0
    if not args.config:
        p.error('--config is required (or use --inventory)')

    with open(args.config, 'r', encoding='utf-8') as f:
        config = json.load(f)

    if args.verify:
        ok = verify_collapse(args.input, config)
        return 0 if ok else 1

    output = args.output or default_output_path(args.input, 'Collapsed')
    collapse(args.input, config, output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
