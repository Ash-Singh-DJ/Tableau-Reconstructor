"""
tableau.py -- Tableau Cloud REST API connector (Personal Access Token).

Signs in to Tableau Cloud with a PAT from `.env` and exposes the small set of
read-only operations the migration workflow needs: list content, and (later) pull a
workbook plus the side-car .tdsx for each published data source it references into
Inputs/. Never publishes. Uses `tableauserverclient` (TSC).

    from connectors.tableau import tableau_session
    with tableau_session() as server:
        for wb in TSC.Pager(server.workbooks):
            ...

Required environment variables
------------------------------
TABLEAU_SERVER_URL   e.g. https://us-east-1.online.tableau.com
TABLEAU_SITE         the site's content URL (the `/t/<site>/` segment), e.g. dj-cdl-prod
TABLEAU_PAT_NAME     Personal Access Token name
TABLEAU_PAT_SECRET   Personal Access Token secret

Tableau Cloud allows ONE active session per PAT: a second sign-in with the same token
ends the first. Keep this token separate from any tableau-mcp server's.

Usage:
    python -m connectors.tableau test                       # sign in, list content
    python -m connectors.tableau pull "<workbook name>"     # -> Inputs/<name>/
    python -m connectors.tableau pull <contentUrl> --project "<project>"
`pull` downloads the workbook without extracts, then the side-car .tdsx of every
published data source it references, wraps any bare .twb/.tds into an archive, and
ends with the check_staging.py report. Items the PAT user cannot download (missing
the Download permission) are reported, not fatal.
"""

import argparse
import os
import re
import sys
from contextlib import contextmanager

import tableauserverclient as TSC
from dotenv import load_dotenv


def _env(name):
    v = os.getenv(name)
    if not v:
        raise ValueError(f'{name} not set. Please check your .env file.')
    return v


@contextmanager
def tableau_session():
    """Signed-in TSC `Server`; signs out on exit (so the PAT session is released)."""
    load_dotenv(override=True)
    # Tableau Cloud redirects http -> https, which turns POSTs (sign-out!) into GETs
    # and fails with 405. Force https regardless of how the URL was typed.
    url = _env('TABLEAU_SERVER_URL').rstrip('/')
    url = 'https://' + url.split('://', 1)[-1]
    server = TSC.Server(url, use_server_version=True)
    auth = TSC.PersonalAccessTokenAuth(_env('TABLEAU_PAT_NAME'), _env('TABLEAU_PAT_SECRET'),
                                       site_id=_env('TABLEAU_SITE'))
    with server.auth.sign_in(auth):
        yield server


def test_connection(limit=10):
    """Sign in and print who/where we are plus a sample of visible content."""
    with tableau_session() as server:
        # sites.get_by_id needs a site admin; the sign-in response already gives us the id
        user = server.users.get_by_id(server.user_id)
        print(f'Signed in to {server.server_address}  (REST API {server.version})')
        print(f"  site : content URL {os.getenv('TABLEAU_SITE')!r}  id {server.site_id}")
        print(f'  user : {user.name}  site role {user.site_role}')

        req = TSC.RequestOptions(pagesize=limit)
        wbs, pag = server.workbooks.get(req)
        print(f'\nWorkbooks visible: {pag.total_available}  (first {len(wbs)})')
        for wb in wbs:
            print(f'  {wb.name}  [{wb.project_name}]  content URL {wb.content_url}')

        dss, pag = server.datasources.get(req)
        print(f'\nPublished data sources visible: {pag.total_available}  (first {len(dss)})')
        for ds in dss:
            print(f'  {ds.name}  [{ds.project_name}]  content URL {ds.content_url}  '
                  f'type {ds.datasource_type}')
    print('\nSigned out.')


# ---- pull a workbook + its published side-cars into Inputs/ ------------------
def _find_workbook(server, name, project=None):
    """Resolve a workbook by exact name OR content URL (the `/workbooks/<X>` URL
    segment, also the workbook's <repository-location id>). Ambiguous names must be
    narrowed with `project`."""
    hits = []
    for field in (TSC.RequestOptions.Field.Name, TSC.RequestOptions.Field.ContentUrl):
        req = TSC.RequestOptions()
        req.filter.add(TSC.Filter(field, TSC.RequestOptions.Operator.Equals, name))
        hits = list(TSC.Pager(server.workbooks, req))
        if hits:
            break
    if project:
        hits = [w for w in hits if w.project_name == project]
    if not hits:
        raise LookupError(f'no workbook named / with content URL {name!r}'
                          + (f' in project {project!r}' if project else ''))
    if len(hits) > 1:
        projs = ', '.join(sorted(repr(w.project_name) for w in hits))
        raise LookupError(f'{len(hits)} workbooks named {name!r} (projects: {projs}); '
                          'pass --project to pick one')
    return hits[0]


_ARCHIVE_EXT = {'.twb': '.twbx', '.tds': '.tdsx', '.twbx': '.twbx', '.tdsx': '.tdsx'}


def _ensure_archive(path, name):
    """Rename a download to `<published name><ext>` (Tableau serves it under its
    content URL, e.g. B2BLicensingIPLevelData.tdsx) and, if it came down as a bare
    .twb/.tds (extract-less content), wrap it in the archive the engines read."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from reconstructor.check_staging import zip_bare, _is_bare
    ext = os.path.splitext(path)[1].lower()
    safe = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    dst = os.path.join(os.path.dirname(path), safe + _ARCHIVE_EXT[ext])
    if _is_bare(path):
        bare = os.path.join(os.path.dirname(path), safe + ext)
        os.replace(path, bare)
        if os.path.exists(dst):
            os.remove(dst)
        zip_bare(bare)
        os.remove(bare)
    else:
        os.replace(path, dst)
    return dst


def pull_workbook(name, project=None, out_root='Inputs', verbose=True):
    """Download a workbook (no extracts) plus the side-car .tdsx of every published
    data source it references into `<out_root>/<workbook name>/`. Returns the
    check_staging report for the staged folder."""
    from reconstructor.check_staging import (check_workbook, print_report, published_refs,
                                             load_root)

    with tableau_session() as server:
        wb = _find_workbook(server, name, project)
        out_dir = os.path.join(out_root, wb.name)
        os.makedirs(out_dir, exist_ok=True)
        if verbose:
            print(f'Workbook {wb.name!r}  [{wb.project_name}]  content URL {wb.content_url}')

        wb_path = _ensure_archive(
            server.workbooks.download(wb.id, filepath=out_dir, include_extract=False), wb.name)
        if verbose:
            print(f'  -> {wb_path}')

        # The workbook holds no SQL for its published (sqlproxy) sources; each must come
        # down as a side-car. Resolve them by CONTENT URL read from the downloaded .twb's
        # <repository-location id> -- the REST connection list's `datasource_id` is the
        # workbook-internal datasource id, not the published source's LUID (404s).
        refs = published_refs(load_root(wb_path))
        if verbose:
            print(f'  published data sources referenced: {len(refs)}')

        failures = []
        for ref in refs:
            label = ref['published_as'] or ref['caption']
            req = TSC.RequestOptions()
            req.filter.add(TSC.Filter(TSC.RequestOptions.Field.ContentUrl,
                                      TSC.RequestOptions.Operator.Equals, ref['content_url']))
            hits = list(TSC.Pager(server.datasources, req))
            if not hits:
                failures.append((label, '404', f'no visible data source with content URL '
                                               f'{ref["content_url"]!r}'))
                if verbose:
                    print(f'  !! {label}: not found / not visible ({ref["content_url"]})')
                continue
            ds = hits[0]
            try:
                path = _ensure_archive(
                    server.datasources.download(ds.id, filepath=out_dir, include_extract=False), ds.name)
                if verbose:
                    print(f'  -> {path}  ({ds.name!r} [{ds.project_name}], {ds.datasource_type})')
            except TSC.ServerResponseError as e:
                failures.append((ds.name, e.code, e.summary))
                if verbose:
                    print(f'  !! {ds.name}: {e.code} {e.summary}')

    report = check_workbook(wb_path, [out_dir])
    report['download_failures'] = failures
    if verbose:
        print_report(report)
        for ds_name, code, summary in failures:
            print(f'  DOWNLOAD FAILED {ds_name!r}: {code} {summary}'
                  + ('  (needs the Download Data Source permission)' if code.startswith('403')
                     else ''))
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description='Tableau Cloud REST connector (read-only).')
    sub = p.add_subparsers(dest='cmd')
    t = sub.add_parser('test', help='sign in and list visible content')
    t.add_argument('--limit', type=int, default=10, help='items to list per type')
    pl = sub.add_parser('pull', help='download a workbook + its published side-car .tdsx')
    pl.add_argument('workbook', help='workbook name or content URL')
    pl.add_argument('--project', help='disambiguate same-named workbooks')
    pl.add_argument('--out', default='Inputs', help='root folder (default Inputs/)')
    args = p.parse_args(argv)

    if args.cmd == 'test':
        test_connection(args.limit)
        return 0
    if args.cmd == 'pull':
        report = pull_workbook(args.workbook, args.project, args.out)
        return 0 if report['summary']['ready'] and not report['download_failures'] else 1
    p.error('nothing to do; use `test` or `pull`')


if __name__ == '__main__':
    sys.exit(main())
