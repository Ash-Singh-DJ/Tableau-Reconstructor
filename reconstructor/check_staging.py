"""
check_staging.py -- pre-flight check that a workbook's inputs are fully staged
(pure stdlib; read-only unless --zip).

WHY. A published-datasource workbook (`sqlproxy` references -- see embed_collapse.py)
holds no Athena SQL: every published source it references must be downloaded from
Tableau Cloud as a side-car .tdsx before the collapse can run. Without this check a
missing side-car surfaces partway through Phase 5. This tool reads the workbook up
front and reports, per published reference:
  - what to download: its published name, content URL, site and server
  - whether a staged .tdsx already covers it, and how the pairing was made
  - what that side-car connects to (athena / snowflake / a further sqlproxy)
plus staged side-cars the workbook does NOT reference, embedded (non-proxy) data
sources, and bare .twb/.tds files -- which the engines cannot read, since they only
open zip archives. --zip wraps each bare file into a sibling .twbx/.tdsx.

MATCHING. A .tdsx downloaded from Tableau carries a <repository-location id=...>
equal to the content URL the workbook's proxy references (the proxy's own
repository-location id, or failing that its connection dbname) -- regardless of how
either was captioned or what the downloaded file was renamed to. That is the
authoritative pairing. A side-car WITHOUT a repository-location falls back to a
normalized-name match, reported as a CANDIDATE that needs user sign-off. A side-car
whose repository id differs is a different published source and is never
name-matched. Same contract as embed_collapse.py: this tool proposes, the user
confirms, and the collapse engine's field fingerprint still verifies every pairing.

Usage:
    python check_staging.py "Inputs/<Workbook dir>"         # every workbook in a dir
    python check_staging.py INPUT.twbx [--search DIR ...]   # extra side-car dirs
    python check_staging.py INPUT.twbx --zip                # also wrap bare files
    python check_staging.py INPUT.twbx --json
Exit status: 0 if every input is staged and readable, else 1.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tableau_doc import read_doc, datasource_elements, ds_label
from embed_collapse import _is_sqlproxy, _repository_id

WORKBOOK_EXTS = ('.twbx', '.twb')
SIDECAR_EXTS = ('.tdsx', '.tds')
BARE_TO_ARCHIVE = {'.twb': '.twbx', '.tds': '.tdsx'}


# ---- IO ----------------------------------------------------------------------
def load_root(path):
    """Parsed XML root of a .twbx/.tdsx archive OR a bare .twb/.tds file (this tool
    must read bare files to report on them, unlike the engines)."""
    if zipfile.is_zipfile(path):
        _, raw = read_doc(path)
    else:
        with open(path, 'r', encoding='utf-8-sig') as f:
            raw = f.read()
    return ET.fromstring(raw)


def zip_bare(path):
    """Wrap a bare .twb/.tds into a sibling .twbx/.tdsx (member at the archive root).
    Returns the archive path, or None if a sibling archive already exists."""
    base, ext = os.path.splitext(path)
    dst = base + BARE_TO_ARCHIVE[ext.lower()]
    if os.path.exists(dst):
        return None
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(path, os.path.basename(path))
    return dst


def _list_docs(directory, exts):
    """Files in `directory` with one of `exts`, de-duplicated by stem: when both a
    bare file and its archive exist (X.twb + X.twbx), only the archive is kept."""
    by_stem = {}
    for fn in sorted(os.listdir(directory)):
        stem, ext = os.path.splitext(fn)
        if ext.lower() not in exts or not os.path.isfile(os.path.join(directory, fn)):
            continue
        prev = by_stem.get(stem)
        if prev is None or _is_bare(prev):
            by_stem[stem] = os.path.join(directory, fn)
    return list(by_stem.values())


def _is_bare(path):
    return os.path.splitext(path)[1].lower() in BARE_TO_ARCHIVE


# ---- introspection -----------------------------------------------------------
def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _conn_classes(ds):
    """Distinct source connection classes under a datasource -- the federated wrapper
    and local extract (hyper) connections excluded."""
    in_extract = {id(c) for ex in ds.iter('extract') for c in ex.iter('connection')}
    return sorted({c.get('class') for c in ds.iter('connection')
                   if c.get('class') and c.get('class') != 'federated'
                   and id(c) not in in_extract})


def _worksheets_using(root, fed_name):
    return sorted({ws.get('name') for ws in root.findall('.//worksheet')
                   if any(dd.get('datasource') == fed_name
                          for dd in ws.findall('.//datasource-dependencies'))})


def published_refs(root):
    """The workbook's sqlproxy datasources, with what's needed to find and download
    each published source on Tableau Cloud."""
    refs = []
    for ds in datasource_elements(root):
        if not _is_sqlproxy(ds):
            continue
        conn = ds.find('connection')
        rl = ds.find('repository-location')
        refs.append({
            'name': ds.get('name'),
            'caption': ds.get('caption'),
            # server-ds-friendly-name appears when the published name differs from
            # the workbook-local caption (e.g. a de-duplicated "Source Level Data (2)")
            'published_as': conn.get('server-ds-friendly-name') or ds.get('caption'),
            'content_url': _repository_id(ds) or conn.get('dbname'),
            'site': rl.get('site') if rl is not None else None,
            'server': conn.get('server'),
            'worksheets': _worksheets_using(root, ds.get('name')),
        })
    return refs


def embedded_sources(root):
    """Non-proxy datasources that carry a real connection (Parameters etc. skipped)."""
    return [{'label': ds_label(ds), 'connections': _conn_classes(ds)}
            for ds in datasource_elements(root)
            if not _is_sqlproxy(ds) and ds.get('hasconnection') != 'false']


def scan_sidecars(dirs):
    out = []
    for d in dirs:
        for path in _list_docs(d, SIDECAR_EXTS):
            rec = {'path': path, 'bare': _is_bare(path)}
            try:
                root = load_root(path)
            except (ET.ParseError, RuntimeError, zipfile.BadZipFile, OSError) as e:
                rec['error'] = str(e)
                out.append(rec)
                continue
            rec.update({
                'formatted_name': ds_label(root),
                'content_url': _repository_id(root),
                'connections': _conn_classes(root),
            })
            out.append(rec)
    return out


# ---- matching ----------------------------------------------------------------
def match(refs, sidecars):
    """Attach `staged` (list of side-cars) + `match` ('content-url' | 'name' | None)
    to each ref. Returns the side-cars no ref claimed."""
    usable = [s for s in sidecars if 'error' not in s]
    claimed = set()
    for ref in refs:
        hits = [s for s in usable if s['content_url'] and s['content_url'] == ref['content_url']]
        how = 'content-url'
        if not hits:
            keys = {_norm(ref['caption']), _norm(ref['published_as'])} - {''}
            hits = [s for s in usable if not s['content_url'] and any(
                k in _norm(s['formatted_name']) or
                k in _norm(os.path.splitext(os.path.basename(s['path']))[0])
                for k in keys)]
            how = 'name'
        ref['staged'] = hits
        ref['match'] = how if hits else None
        claimed.update(s['path'] for s in hits)
    return [s for s in usable if s['path'] not in claimed]


def check_workbook(wb_path, search_dirs):
    root = load_root(wb_path)
    refs = published_refs(root)
    sidecars = scan_sidecars(search_dirs)
    unreferenced = match(refs, sidecars)
    wb_rl = root.find('repository-location')
    report = {
        'workbook': wb_path,
        'workbook_bare': _is_bare(wb_path),
        'workbook_content_url': wb_rl.get('id') if wb_rl is not None else None,
        'published_refs': refs,
        'embedded_sources': embedded_sources(root),
        'unreferenced_sidecars': unreferenced,
        'unreadable_sidecars': [s for s in sidecars if 'error' in s],
    }
    missing = [r for r in refs if not r['staged']]
    unconfirmed = [r for r in refs if r['match'] == 'name' or len(r['staged']) > 1]
    bare = ([wb_path] if report['workbook_bare'] else []) + \
           [s['path'] for r in refs for s in r['staged'] if s['bare']]
    report['summary'] = {
        'missing': len(missing), 'unconfirmed': len(unconfirmed), 'bare': len(bare),
        'ready': not (missing or unconfirmed or bare),
    }
    return report


# ---- reporting ---------------------------------------------------------------
def _browse_url(ref):
    if ref['server'] and ref['site']:
        return f"https://{ref['server']}/#/site/{ref['site']}/datasources"
    return None


def print_report(rep):
    refs = rep['published_refs']
    s = rep['summary']
    print(f"\nWorkbook: {rep['workbook']}"
          + ('   [BARE .twb -- engines need a .twbx; rerun with --zip]'
             if rep['workbook_bare'] else ''))
    if rep['workbook_content_url']:
        print(f"  workbook content URL: {rep['workbook_content_url']}")

    print(f"\n  Published data sources referenced: {len(refs)}  "
          f"(staged {len(refs) - s['missing']}, missing {s['missing']})")
    for r in refs:
        tag = ('[MISSING]' if not r['staged'] else
               '[check]  ' if r['match'] == 'name' or len(r['staged']) > 1 else
               '[ok]     ')
        pub = (f"  (published as \"{r['published_as']}\")"
               if r['published_as'] != r['caption'] else '')
        print(f"    {tag} {r['caption']}{pub}")
        print(f"              content URL {r['content_url']} | site {r['site']} | "
              f"used by {len(r['worksheets'])} worksheet(s)"
              + ('  <- unused; may not need downloading' if not r['worksheets'] else ''))
        if not r['staged']:
            url = _browse_url(r)
            if url:
                print(f"              download from {url}")
        for sc in r['staged']:
            note = ('matched by content URL' if r['match'] == 'content-url' else
                    'NAME CANDIDATE -- no repository-location; confirm before use')
            print(f"              -> {os.path.basename(sc['path'])}  ({note}; "
                  f"connects: {', '.join(sc['connections']) or '?'})"
                  + ('  [BARE .tds]' if sc['bare'] else ''))
        if len(r['staged']) > 1:
            print('              multiple side-cars match -- keep one')

    if rep['embedded_sources']:
        print('\n  Embedded data sources (no download needed):')
        for e in rep['embedded_sources']:
            print(f"    {e['label']}  (connects: {', '.join(e['connections']) or '?'})")

    if rep['unreferenced_sidecars']:
        print('\n  Staged side-cars NOT referenced by this workbook:')
        for sc in rep['unreferenced_sidecars']:
            print(f"    {os.path.basename(sc['path'])}  (content URL {sc.get('content_url')})")
    for sc in rep['unreadable_sidecars']:
        print(f"\n  UNREADABLE side-car {sc['path']}: {sc['error']}")

    status = 'READY' if s['ready'] else (
        f"NOT READY ({s['missing']} missing, {s['unconfirmed']} to confirm, "
        f"{s['bare']} bare)")
    print(f'\n  Status: {status}')


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Check that a workbook's published side-car .tdsx files are staged.")
    p.add_argument('input', help='a .twbx/.twb, or a directory holding workbook(s)')
    p.add_argument('--search', action='append', default=[], metavar='DIR',
                   help="extra directory to look for side-cars in (the workbook's own "
                        'directory is always searched); repeatable')
    p.add_argument('--zip', action='store_true',
                   help='wrap bare .twb/.tds files into sibling .twbx/.tdsx first')
    p.add_argument('--json', action='store_true', help='emit JSON instead of text')
    args = p.parse_args(argv)

    if os.path.isdir(args.input):
        wb_dir = args.input
        workbooks = _list_docs(wb_dir, WORKBOOK_EXTS)
        if not workbooks:
            p.error(f'no .twbx/.twb found in {wb_dir}')
    elif os.path.splitext(args.input)[1].lower() in WORKBOOK_EXTS:
        wb_dir = os.path.dirname(os.path.abspath(args.input))
        workbooks = [args.input]
    else:
        p.error('input must be a .twbx/.twb or a directory (side-car .tdsx files are '
                'found automatically)')
    search_dirs = [wb_dir] + args.search

    if args.zip:
        bare = [w for w in workbooks if _is_bare(w)]
        for d in search_dirs:
            bare += [f for f in _list_docs(d, SIDECAR_EXTS) if _is_bare(f)]
        for path in bare:
            dst = zip_bare(path)
            if dst and not args.json:
                print(f'zipped {path} -> {dst}')
        workbooks = [os.path.splitext(w)[0] + BARE_TO_ARCHIVE[os.path.splitext(w)[1].lower()]
                     if _is_bare(w) else w for w in workbooks]

    reports = [check_workbook(w, search_dirs) for w in workbooks]
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for rep in reports:
            print_report(rep)
    return 0 if all(r['summary']['ready'] for r in reports) else 1


if __name__ == '__main__':
    sys.exit(main())
