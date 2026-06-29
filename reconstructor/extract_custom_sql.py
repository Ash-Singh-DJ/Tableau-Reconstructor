"""
Extract Custom SQL queries from Tableau .twbx workbook files.

Usage:
    python extract_custom_sql.py                     # Process all .twbx files in current directory
    python extract_custom_sql.py path/to/file.twbx   # Process a specific file
    python extract_custom_sql.py path/to/dir/         # Process all .twbx files in a directory
"""

import zipfile
import xml.etree.ElementTree as ET
import os
import sys
import re


def sanitize_filename(name):
    """Convert a datasource caption into a safe filename."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip().replace(' ', '_')
    return name


def extract_custom_sql(twbx_path):
    """
    Extract Custom SQL queries from a .twbx file.

    Returns a list of dicts with keys:
        - datasource: caption of the Tableau datasource
        - connection_class: type of connection (e.g. 'athena')
        - schema: schema from the connection, if present
        - sql: the SQL query text
    """
    results = []

    with zipfile.ZipFile(twbx_path) as z:
        twb_names = [n for n in z.namelist() if n.endswith('.twb')]
        if not twb_names:
            print(f"  No .twb found inside {twbx_path}")
            return results
        twb_xml = z.read(twb_names[0]).decode('utf-8')

    root = ET.fromstring(twb_xml)
    datasources_el = root.find('.//datasources')
    if datasources_el is None:
        return results

    for ds in datasources_el.findall('datasource'):
        caption = ds.get('caption', ds.get('name', 'unknown'))

        # Identify connection class and schema from the named-connection
        named_conn = ds.find('.//named-connection/connection')
        conn_class = named_conn.get('class', '') if named_conn is not None else ''
        schema = named_conn.get('schema', '') if named_conn is not None else ''

        # Get the first relation type="text" — the canonical one lives under
        # <connection>/<relation>, not the duplicate in <object-graph>.
        conn_el = ds.find('connection')
        if conn_el is None:
            continue
        rel = conn_el.find('relation[@type="text"]')
        if rel is None:
            continue

        sql = (rel.text or '').strip()
        if not sql:
            continue

        results.append({
            'datasource': caption,
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

    saved = []
    for q in queries:
        filename = sanitize_filename(q['datasource']) + '.sql'
        filepath = os.path.join(output_dir, filename)

        header = (
            f"-- Source workbook: {workbook_name}\n"
            f"-- Datasource: {q['datasource']}\n"
            f"-- Connection: {q['connection_class']}"
        )
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

    if os.path.isfile(target) and target.endswith('.twbx'):
        process_twbx(target)
    elif os.path.isdir(target):
        twbx_files = [
            os.path.join(target, f)
            for f in os.listdir(target)
            if f.endswith('.twbx')
        ]
        if not twbx_files:
            print(f"No .twbx files found in {target}")
            return
        for path in sorted(twbx_files):
            process_twbx(path)
    else:
        print(f"Not a .twbx file or directory: {target}")
        sys.exit(1)


if __name__ == '__main__':
    main()
