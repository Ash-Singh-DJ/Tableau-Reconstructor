"""
Extract Custom SQL queries from Tableau .twbx/.tdsx files.

Usage:
    python extract_custom_sql.py                     # Process all .twbx/.tdsx files in current directory
    python extract_custom_sql.py path/to/file.tdsx   # Process a specific file
    python extract_custom_sql.py path/to/dir/         # Process all .twbx/.tdsx files in a directory
"""

import zipfile
import xml.etree.ElementTree as ET
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tableau_doc import read_doc, datasource_elements, ds_label, ARCHIVE_EXTS


def sanitize_filename(name):
    """Convert a datasource caption into a safe filename."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip().replace(' ', '_')
    return name


def extract_custom_sql(twbx_path):
    """
    Extract Custom SQL queries from a .twbx/.tdsx file.

    Returns a list of dicts with keys:
        - datasource: caption of the Tableau datasource
        - relation_name: name of the Custom SQL relation (distinguishes the parts
          of a joined/unioned datasource that carries several Custom SQL queries)
        - connection_class: type of connection (e.g. 'athena')
        - schema: schema from the connection, if present
        - sql: the SQL query text
    """
    results = []

    try:
        _, twb_xml = read_doc(twbx_path)
    except RuntimeError as e:
        print(f"  {e}")
        return results

    root = ET.fromstring(twb_xml)

    for ds in datasource_elements(root):
        caption = ds_label(ds) or 'unknown'

        # Identify connection class and schema from the named-connection
        named_conn = ds.find('.//named-connection/connection')
        conn_class = named_conn.get('class', '') if named_conn is not None else ''
        schema = named_conn.get('schema', '') if named_conn is not None else ''

        # Collect EVERY text relation in the <connection> subtree, not just a direct
        # child: a joined/unioned datasource nests its Custom SQL relations inside a
        # join <relation> container. We iterate <connection> only (the <object-graph>
        # holds duplicate copies of the same relations).
        conn_el = ds.find('connection')
        if conn_el is None:
            continue
        for rel in conn_el.iter('relation'):
            if rel.get('type') != 'text':
                continue
            sql = (rel.text or '').strip()
            if not sql:
                continue
            results.append({
                'datasource': caption,
                'relation_name': rel.get('name', ''),
                'connection_class': conn_class,
                'schema': schema,
                'sql': sql,
            })

    return results


def process_twbx(twbx_path, parent_dir=None):
    """Extract and save Custom SQL queries from a single .twbx file."""
    if parent_dir is None:
        parent_dir = os.path.dirname(os.path.abspath(twbx_path))

    workbook_name = os.path.splitext(os.path.basename(twbx_path))[0]
    output_dir = os.path.join(parent_dir, workbook_name)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\nProcessing: {os.path.basename(twbx_path)}")
    print(f"  Output folder: {workbook_name}/")

    queries = extract_custom_sql(twbx_path)

    if not queries:
        print("  No Custom SQL datasources found.")
        return []

    # A joined/unioned datasource yields several relations; disambiguate their
    # filenames by relation name so they don't overwrite each other. A datasource
    # with a single relation keeps the simple caption-only filename.
    per_ds_count = {}
    for q in queries:
        per_ds_count[q['datasource']] = per_ds_count.get(q['datasource'], 0) + 1

    saved = []
    for q in queries:
        stem = sanitize_filename(q['datasource'])
        if per_ds_count[q['datasource']] > 1 and q['relation_name']:
            stem += '__' + sanitize_filename(q['relation_name'])
        filename = stem + '.sql'
        filepath = os.path.join(output_dir, filename)

        header = (
            f"-- Source workbook: {workbook_name}\n"
            f"-- Datasource: {q['datasource']}\n"
        )
        if q['relation_name']:
            header += f"-- Relation: {q['relation_name']}\n"
        header += f"-- Connection: {q['connection_class']}"
        if q['schema']:
            header += f" (schema: {q['schema']})"
        header += "\n--\n\n"

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(header + q['sql'] + '\n')

        print(f"  Saved: {filename}")
        saved.append(filepath)

    return saved


def main():
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = os.getcwd()

    if os.path.isfile(target) and target.endswith(ARCHIVE_EXTS):
        process_twbx(target)
    elif os.path.isdir(target):
        twbx_files = [
            os.path.join(target, f)
            for f in os.listdir(target)
            if f.endswith(ARCHIVE_EXTS)
        ]
        if not twbx_files:
            print(f"No .twbx/.tdsx files found in {target}")
            return
        for path in sorted(twbx_files):
            process_twbx(path)
    else:
        print(f"Not a .twbx/.tdsx file or directory: {target}")
        sys.exit(1)


if __name__ == '__main__':
    main()
