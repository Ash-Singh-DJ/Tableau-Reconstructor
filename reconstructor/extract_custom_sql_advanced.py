"""
Extract Custom SQL queries from Tableau .twbx workbook files, with advanced
variants that append translatable Tableau calculated fields as CTE columns.

Outputs are organized into folders named after each .twbx file:
  - Base .sql files: the raw Custom SQL from each datasource
  - advanced_*.sql files: base query wrapped with a CTE adding translatable
    Tableau calculated fields as additional columns

Usage:
    python extract_custom_sql_advanced.py                     # Process all .twbx in current dir
    python extract_custom_sql_advanced.py path/to/file.twbx   # Process a specific file
    python extract_custom_sql_advanced.py path/to/dir/         # Process all .twbx in a directory
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


def sanitize_alias(name):
    """Convert a field caption into a valid SQL column alias."""
    alias = re.sub(r'[<>:"/\\|?*]', '', name)
    alias = alias.strip().replace(' ', '_').replace('-', '_')
    alias = re.sub(r'[^a-zA-Z0-9_]', '', alias)
    alias = alias.lower()
    # Ensure it doesn't start with a digit
    if alias and alias[0].isdigit():
        alias = '_' + alias
    return alias


def parse_twb(twbx_path):
    """Read the .twb XML from inside a .twbx archive and return the parsed root."""
    with zipfile.ZipFile(twbx_path) as z:
        twb_names = [n for n in z.namelist() if n.endswith('.twb')]
        if not twb_names:
            return None
        return ET.fromstring(z.read(twb_names[0]).decode('utf-8'))


def get_base_columns(ds_element):
    """Return a set of base column names (remote_name) from metadata-records."""
    cols = set()
    for mr in ds_element.iter('metadata-record'):
        if mr.get('class') == 'column':
            remote = mr.findtext('remote-name', '')
            if remote:
                cols.add(remote.lower())
    return cols


def get_calculated_fields(ds_element):
    """
    Return all non-trivial calculated fields from a datasource element.

    Each entry is a dict with: caption, name, datatype, formula
    """
    fields = []
    for col in ds_element.findall('column'):
        calc = col.find('calculation')
        if calc is None or calc.get('class') != 'tableau':
            continue
        formula = calc.get('formula', '')
        if not formula:
            continue
        stripped = formula.strip()
        if stripped in ('true', 'false'):
            continue
        if stripped.startswith('"') and stripped.endswith('"') and '\n' not in stripped:
            continue
        try:
            float(stripped)
            continue
        except ValueError:
            pass

        fields.append({
            'caption': col.get('caption', ''),
            'name': col.get('name', ''),
            'datatype': col.get('datatype', ''),
            'formula': formula,
        })
    return fields


def is_translatable(formula, base_columns):
    """
    Determine whether a Tableau formula can be translated to a row-level SQL
    expression (no aggregation context, no parameters, no LOD expressions,
    no RANK, no COUNTD, no references to other calculated fields).
    """
    f = formula.strip()

    # Reject LOD expressions
    if '{' in f:
        return False

    # Reject parameter references
    if '[Parameters].' in f:
        return False

    # Reject aggregation functions that depend on viz context
    agg_pattern = r'\b(RANK|COUNTD|SUM|AVG|MIN|MAX|COUNT|MEDIAN|ATTR)\s*\('
    if re.search(agg_pattern, f, re.IGNORECASE):
        return False

    # Extract all field references [field_name]
    refs = re.findall(r'\[([^\]]+)\]', f)
    for ref in refs:
        if ref.lower() in base_columns:
            continue
        # Reject references to other calculated fields
        return False

    return True


def tableau_to_sql(formula):
    """
    Translate a translatable Tableau formula to a SQL expression.
    """
    sql = formula.strip()

    # Replace Tableau [field] references with bare column names
    sql = re.sub(r'\[([^\]]+)\]', r'\1', sql)

    # Convert Tableau double-quoted strings to SQL single-quoted strings
    # (handles "string" -> 'string' but avoids breaking escaped quotes)
    sql = re.sub(r'"([^"]*)"', r"'\1'", sql)

    # Convert Tableau string concatenation (+) to SQL (||)
    # Match + that sits between string-like expressions
    sql = re.sub(r'\s*\+\s*', ' || ', sql)

    # Convert Tableau IF/ELSEIF/ELSE/END to SQL CASE WHEN
    if re.match(r'\s*IF\b', sql, re.IGNORECASE):
        sql = re.sub(r'\bIF\b', 'CASE WHEN', sql, count=1, flags=re.IGNORECASE)
        sql = re.sub(r'\bELSEIF\b', 'WHEN', flags=re.IGNORECASE, string=sql)

    # Convert Tableau AND within CASE WHEN (Tableau uses AND, SQL does too — no change needed)

    # Convert Tableau ISNULL() to SQL IS NULL
    sql = re.sub(r'\bISNULL\s*\(\s*(\w+)\s*\)', r'\1 IS NULL', sql, flags=re.IGNORECASE)

    # Tableau uses == for equality; SQL uses =
    sql = sql.replace('==', '=')

    return sql


def build_advanced_sql(base_sql, calculated_fields, base_columns):
    """
    Wrap the base Custom SQL in a CTE and add translatable calculated fields
    as additional columns in the final SELECT.

    Returns (advanced_sql, included_fields, skipped_fields).
    """
    translatable = []
    skipped = []

    for field in calculated_fields:
        if is_translatable(field['formula'], base_columns):
            translatable.append(field)
        else:
            skipped.append(field)

    if not translatable:
        return None, [], skipped

    lines = []
    lines.append("-- Calculated fields appended as additional columns via CTE")
    lines.append("-- Fields that depend on Tableau viz context (aggregations, parameters,")
    lines.append("-- LOD expressions, RANK) are excluded — see skipped list below.")
    lines.append("--")

    if skipped:
        lines.append("-- SKIPPED (not translatable to row-level SQL):")
        for s in skipped:
            label = s['caption'] or s['name']
            lines.append(f"--   {label}")
        lines.append("--")

    lines.append("")

    # Check if the base SQL already starts with WITH (has its own CTEs).
    # If so, we need to incorporate its CTEs rather than nesting WITH inside WITH.
    stripped_base = base_sql.strip()
    base_uses_with = re.match(r'\bWITH\b', stripped_base, re.IGNORECASE)

    if base_uses_with:
        # Extract everything after the initial WITH keyword, then prepend
        # the base CTEs before adding our own base_query CTE.
        # Strategy: strip the leading WITH, wrap the final SELECT in base_query AS (...)
        # Find the last top-level SELECT by finding where the CTEs end.
        # Simpler approach: wrap the entire original query as a subquery.
        lines.append("WITH base_query AS (")
        lines.append("    SELECT * FROM (")
        for line in base_sql.split('\n'):
            lines.append("        " + line)
        lines.append("    )")
        lines.append(")")
    else:
        lines.append("WITH base_query AS (")
        for line in base_sql.split('\n'):
            lines.append("    " + line)
        lines.append(")")

    lines.append("")
    lines.append("SELECT")
    lines.append("    base_query.*,")

    calc_expressions = []
    for field in translatable:
        sql_expr = tableau_to_sql(field['formula'])
        alias = sanitize_alias(field['caption'] or field['name'])
        # Indent each line of the expression and append alias to the last line
        expr_lines = sql_expr.strip().split('\n')
        indented = '\n'.join('    ' + el for el in expr_lines)
        indented += f" AS {alias}"
        calc_expressions.append(indented)

    lines.append(',\n'.join(calc_expressions))
    lines.append("FROM base_query")

    return '\n'.join(lines), translatable, skipped


def extract_datasources(twbx_path):
    """
    Extract Custom SQL datasources with their calculated fields.

    Returns a list of dicts with keys:
        datasource, connection_class, schema, sql, calculated_fields, base_columns
    """
    root = parse_twb(twbx_path)
    if root is None:
        print(f"  No .twb found inside {twbx_path}")
        return []

    results = []
    datasources_el = root.find('.//datasources')
    if datasources_el is None:
        return results

    for ds in datasources_el.findall('datasource'):
        caption = ds.get('caption', ds.get('name', 'unknown'))

        named_conn = ds.find('.//named-connection/connection')
        conn_class = named_conn.get('class', '') if named_conn is not None else ''
        schema = named_conn.get('schema', '') if named_conn is not None else ''

        conn_el = ds.find('connection')
        if conn_el is None:
            continue
        rel = conn_el.find('relation[@type="text"]')
        if rel is None:
            continue

        sql = (rel.text or '').strip()
        if not sql:
            continue

        base_cols = get_base_columns(ds)
        calc_fields = get_calculated_fields(ds)

        results.append({
            'datasource': caption,
            'connection_class': conn_class,
            'schema': schema,
            'sql': sql,
            'calculated_fields': calc_fields,
            'base_columns': base_cols,
        })

    return results


def make_header(workbook_name, datasource, conn_class, schema, variant=None):
    """Build the SQL file header comment."""
    header = f"-- Source workbook: {workbook_name}\n"
    header += f"-- Datasource: {datasource}\n"
    header += f"-- Connection: {conn_class}"
    if schema:
        header += f" (schema: {schema})"
    if variant:
        header += f"\n-- Variant: {variant}"
    header += "\n--\n\n"
    return header


def process_twbx(twbx_path, parent_dir=None):
    """Extract and save Custom SQL queries from a single .twbx file."""
    if parent_dir is None:
        parent_dir = os.path.dirname(os.path.abspath(twbx_path))

    workbook_name = os.path.splitext(os.path.basename(twbx_path))[0]
    output_dir = os.path.join(parent_dir, workbook_name)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\nProcessing: {os.path.basename(twbx_path)}")
    print(f"  Output folder: {workbook_name}/")

    datasources = extract_datasources(twbx_path)

    if not datasources:
        print("  No Custom SQL datasources found.")
        return []

    saved = []
    for ds in datasources:
        base_filename = sanitize_filename(ds['datasource']) + '.sql'
        base_filepath = os.path.join(output_dir, base_filename)

        # Write base SQL
        header = make_header(workbook_name, ds['datasource'],
                             ds['connection_class'], ds['schema'])
        with open(base_filepath, 'w', encoding='utf-8') as f:
            f.write(header + ds['sql'] + '\n')
        print(f"  Saved: {base_filename}")
        saved.append(base_filepath)

        # Build and write advanced variant
        if ds['calculated_fields']:
            advanced_sql, included, skipped = build_advanced_sql(
                ds['sql'], ds['calculated_fields'], ds['base_columns']
            )
            if advanced_sql:
                adv_filename = 'advanced_' + base_filename
                adv_filepath = os.path.join(output_dir, adv_filename)
                adv_header = make_header(
                    workbook_name, ds['datasource'],
                    ds['connection_class'], ds['schema'],
                    variant=f"advanced ({len(included)} calculated fields added)"
                )
                with open(adv_filepath, 'w', encoding='utf-8') as f:
                    f.write(adv_header + advanced_sql + '\n')
                print(f"  Saved: {adv_filename}  ({len(included)} fields added, {len(skipped)} skipped)")
                saved.append(adv_filepath)
            else:
                print(f"  No translatable calculated fields for {ds['datasource']}")

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
