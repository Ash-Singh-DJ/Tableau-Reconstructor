"""
Extract field metadata from Tableau .twbx/.tdsx files as CSVs.

For each datasource in each workbook, produces:
  - fields_<Datasource>.csv: all column elements (name, caption, datatype, role,
    whether calculated, formula if applicable)
  - sql_columns_<Datasource>.csv: metadata-record entries mapping the actual SQL
    column names from Custom SQL queries to Tableau's internal local names

These are useful for building Snowflake gold tables that can serve as direct
replace-data-source candidates in Tableau.

Usage:
    python extract_field_metadata.py                     # Process all .twbx/.tdsx in Inputs/
    python extract_field_metadata.py path/to/file.tdsx   # Process a specific file
"""

import zipfile
import xml.etree.ElementTree as ET
import csv
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tableau_doc import parse_doc, datasource_elements, ds_label, ARCHIVE_EXTS


def sanitize_filename(name):
    """Convert a datasource caption into a safe filename."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip().replace(' ', '_')
    return name


def extract_metadata(twbx_path, output_dir):
    """Extract field metadata CSVs from a single workbook or data source."""
    root = parse_doc(twbx_path)
    if root is None:
        print(f"  No .twb or .tds found inside {twbx_path}")
        return

    datasources = datasource_elements(root)
    if not datasources:
        print("  No datasource element found")
        return

    for ds in datasources:
        ds_caption = ds_label(ds) or 'unknown'
        ds_name = ds.get('name', '')

        if ds_name == 'Parameters' or ds_caption == 'Parameters':
            continue

        # Collect metadata-records (SQL column names from the query)
        metadata_records = {}
        for mr in ds.iter('metadata-record'):
            if mr.get('class') == 'column':
                remote = mr.findtext('remote-name', '')
                local = mr.findtext('local-name', '')
                local_type = mr.findtext('local-type', '')
                parent = mr.findtext('parent-name', '')
                # Only keep records from Custom SQL Query parent (not Extract duplicates)
                if remote and 'Extract' not in (parent or ''):
                    metadata_records[remote.lower()] = {
                        'sql_column': remote,
                        'local_name': local,
                        'local_type': local_type,
                        'parent': parent,
                    }

        # Collect column elements (calculated fields + field captions)
        columns = []
        for col in ds.findall('column'):
            col_name = col.get('name', '')
            col_caption = col.get('caption', '')
            col_datatype = col.get('datatype', '')
            col_role = col.get('role', '')
            col_type = col.get('type', '')

            # Skip internal measure names
            if col_name == '[:Measure Names]':
                continue

            calc = col.find('calculation')
            formula = ''
            if calc is not None and calc.get('class') == 'tableau':
                formula = calc.get('formula', '')

            is_calculated = bool(formula)

            columns.append({
                'tableau_name': col_name,
                'caption': col_caption,
                'datatype': col_datatype,
                'role': col_role,
                'type': col_type,
                'is_calculated': is_calculated,
                'formula': formula,
            })

        if not columns and not metadata_records:
            continue

        safe_name = sanitize_filename(ds_caption)

        # Write columns CSV
        if columns:
            col_csv_path = os.path.join(output_dir, f'fields_{safe_name}.csv')
            with open(col_csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'tableau_name', 'caption', 'datatype', 'role', 'type',
                    'is_calculated', 'formula'
                ])
                for c in columns:
                    writer.writerow([
                        c['tableau_name'], c['caption'], c['datatype'],
                        c['role'], c['type'], c['is_calculated'], c['formula']
                    ])
            print(f"  {os.path.basename(col_csv_path)} ({len(columns)} fields)")

        # Write metadata-records CSV (SQL column mapping)
        if metadata_records:
            meta_csv_path = os.path.join(output_dir, f'sql_columns_{safe_name}.csv')
            with open(meta_csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['sql_column', 'local_name', 'local_type', 'parent'])
                for mr in metadata_records.values():
                    writer.writerow([
                        mr['sql_column'], mr['local_name'],
                        mr['local_type'], mr['parent']
                    ])
            print(f"  {os.path.basename(meta_csv_path)} ({len(metadata_records)} SQL columns)")


def process_workbook(twbx_path, output_dir=None):
    """Process a single workbook, writing CSVs to the appropriate Outputs folder."""
    workbook_name = os.path.splitext(os.path.basename(twbx_path))[0]

    if output_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(script_dir, 'Outputs', workbook_name)

    os.makedirs(output_dir, exist_ok=True)

    print(f"\nProcessing: {os.path.basename(twbx_path)}")
    print(f"  Output folder: {output_dir}")
    extract_metadata(twbx_path, output_dir)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    if len(sys.argv) > 1:
        target = sys.argv[1]
        if os.path.isfile(target) and target.endswith(ARCHIVE_EXTS):
            process_workbook(target)
        else:
            print(f"Not a .twbx/.tdsx file: {target}")
            sys.exit(1)
    else:
        inputs_dir = os.path.join(script_dir, 'Inputs')
        twbx_files = [
            os.path.join(inputs_dir, f)
            for f in os.listdir(inputs_dir)
            if f.endswith(ARCHIVE_EXTS)
        ]
        if not twbx_files:
            print(f"No .twbx/.tdsx files found in {inputs_dir}")
            return
        for path in sorted(twbx_files):
            process_workbook(path)

    print("\nDone.")


if __name__ == '__main__':
    main()
