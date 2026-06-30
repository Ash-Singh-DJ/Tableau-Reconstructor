"""
Extract Custom SQL queries from Tableau .twbx/.tdsx files, with advanced
variants that append translatable Tableau calculated fields as CTE columns.

Outputs are organized into folders named after each .twbx/.tdsx file:
  - Base .sql files: the raw Custom SQL from each datasource
  - advanced_*.sql files: base query wrapped with a CTE adding translatable
    Tableau calculated fields as additional columns

A datasource that joins/unions several Custom SQL relations is FLATTENED into one
query first (a CTE per text relation + reconstructed JOINs with best-effort ON-clause
translation), so the calc fields bolt onto a single base SELECT. Non-SQL join leaves
(e.g. a Google Drive sheet) can't be inlined; they're surfaced as a TODO + warning.

Usage:
    python extract_custom_sql_advanced.py                     # Process all .twbx/.tdsx in current dir
    python extract_custom_sql_advanced.py path/to/file.tdsx   # Process a specific file
    python extract_custom_sql_advanced.py path/to/dir/         # Process all .twbx/.tdsx in a directory
"""

import zipfile
import xml.etree.ElementTree as ET
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


# ---- join-tree flattening ----------------------------------------------------
# A Tableau datasource can join/union several Custom SQL relations. Tableau stores
# this as a binary tree of <relation type="join"> nodes under <connection>: each
# join node holds a <clause> plus exactly two child <relation>s (left, right); a
# child may itself be a join. Leaf <relation type="text"> nodes carry the actual
# Custom SQL; <relation type="table"> leaves are external (e.g. a Google Drive
# sheet) and have NO SQL. To feed the calc-field materializer one query, we wrap
# each text relation as a CTE and reconstruct the JOINs, translating the ON-clause
# expressions from Tableau syntax to SQL. Non-SQL leaves can't be inlined, so they
# are surfaced as a TODO + warning rather than dropped.

_JOIN_OPS = {'=', '<>', '!=', '>', '<', '>=', '<=', 'AND', 'OR'}


def _join_expr_to_sql(text):
    """Translate a single Tableau expression string (a join-clause operand, which
    may be a bare field ref or a full IF/ISNULL/concat formula) to SQL.

    Field refs are qualified by relation name in join clauses, so
    [Rel Name].[field] -> "Rel Name"."field" (quoted to survive spaces). This is a
    best-effort draft translation for human review, not a guaranteed-correct rewrite.
    """
    s = (text or '').strip()
    # qualified [rel].[field] -> "rel"."field"; then any bare [field] -> "field"
    s = re.sub(r'\[([^\]]+)\]\.\[([^\]]+)\]', r'"\1"."\2"', s)
    s = re.sub(r'\[([^\]]+)\]', r'"\1"', s)
    # IF/ELSEIF -> CASE WHEN/WHEN (THEN/ELSE/END are identical in SQL)
    s = re.sub(r'\bIF\b', 'CASE WHEN', s, count=1, flags=re.IGNORECASE)
    s = re.sub(r'\bELSEIF\b', 'WHEN', s, flags=re.IGNORECASE)
    # ISNULL(x) -> x IS NULL
    s = re.sub(r'\bISNULL\s*\(\s*([^)]+?)\s*\)', r'\1 IS NULL', s, flags=re.IGNORECASE)
    # Tableau string concat (+) -> SQL (||); equality (==) -> (=)
    s = re.sub(r'\s*\+\s*', ' || ', s)
    s = s.replace('==', '=')
    return s


def _render_expression(expr):
    """Recursively render a <clause> <expression> tree into a SQL boolean string.

    An expression with child <expression>s is an operator joining them (op is '=',
    'AND', ...); a childless expression is a leaf whose `op` attribute holds the
    Tableau operand text."""
    children = expr.findall('expression')
    op = expr.get('op', '') or ''
    if not children:
        return _join_expr_to_sql(op)
    rendered = [_render_expression(c) for c in children]
    if len(rendered) == 1:
        # unary (e.g. NOT) — keep the operator prefix
        return f"{op} {rendered[0]}"
    sql_op = op.upper() if op.upper() in _JOIN_OPS else op
    return '(' + f' {sql_op} '.join(rendered) + ')'


def _render_clause(clause):
    expr = clause.find('expression') if clause is not None else None
    if expr is None:
        return 'TODO_JOIN_CONDITION /* clause not found */'
    return _render_expression(expr)


def _indent(text, prefix='\t'):
    return '\n'.join(prefix + line for line in text.split('\n'))


def _build_from(rel, ctes, warnings):
    """Walk a relation (sub)tree, registering text relations as CTEs in `ctes`
    (ordered dict name->sql), and return the SQL FROM-clause fragment for it."""
    rtype = rel.get('type')
    name = rel.get('name') or ''

    if rtype == 'text':
        sql = (rel.text or '').strip()
        if name not in ctes:
            ctes[name] = sql
        return f'"{name}"'

    if rtype == 'table':
        tbl = rel.get('table', '')
        warnings.append(
            f"relation '{name}' is a non-SQL source (table {tbl} on connection "
            f"'{rel.get('connection', '')}') and cannot be flattened into SQL — "
            f"left as a TODO in the JOIN for manual handling.")
        return f'/* TODO non-SQL source: {name} (table {tbl}) */ "{name}"'

    if rtype == 'join':
        clause = rel.find('clause')
        kids = [c for c in rel if c.tag == 'relation']
        if len(kids) != 2:
            warnings.append(f"join node has {len(kids)} child relations (expected 2); "
                            "flattening best-effort.")
        left = _build_from(kids[0], ctes, warnings) if kids else 'TODO_LEFT'
        right = _build_from(kids[1], ctes, warnings) if len(kids) > 1 else 'TODO_RIGHT'
        join_kw = (rel.get('join') or 'inner').upper() + ' JOIN'
        on = _render_clause(clause)
        return f"{left}\n{join_kw} {right}\n\tON {on}"

    if rtype == 'union':
        kids = [c for c in rel if c.tag == 'relation']
        refs = [_build_from(k, ctes, warnings) for k in kids]
        warnings.append("union relation flattened as UNION ALL of its members.")
        return '\nUNION ALL\n'.join(f'SELECT * FROM {r}' for r in refs)

    warnings.append(f"unhandled relation type '{rtype}' (name '{name}') — skipped.")
    return f'/* TODO unhandled relation type {rtype}: {name} */'


def flatten_joined_sql(conn_el):
    """Flatten a datasource <connection>'s relation tree into one SQL query.

    Returns (sql, warnings, relation_count). For a single Custom SQL relation this
    is just that relation's SQL (no CTE wrapping). For a join/union of several text
    relations it returns a `WITH <rel> AS (...), ... SELECT * FROM <joins>` query.
    Returns (None, warnings, 0) if there is no Custom SQL to extract.
    """
    top = conn_el.find('relation')
    if top is None:
        return None, [], 0

    # Single text relation, no join: pass the SQL through unchanged.
    if top.get('type') == 'text':
        sql = (top.text or '').strip()
        return (sql or None), [], (1 if sql else 0)

    ctes = {}
    warnings = []
    from_clause = _build_from(top, ctes, warnings)

    if not ctes:
        return None, warnings, 0

    # A single CTE that's the whole query needs no JOIN scaffolding.
    if len(ctes) == 1 and top.get('type') == 'text':
        return next(iter(ctes.values())) or None, warnings, 1

    lines = []
    for i, (cte_name, cte_sql) in enumerate(ctes.items()):
        lines.append(('WITH ' if i == 0 else ', ') + f'"{cte_name}" AS (')
        lines.append(_indent(cte_sql))
        lines.append(')')
    lines.append('SELECT *')
    lines.append('FROM ' + from_clause)
    return '\n'.join(lines), warnings, len(ctes)


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
    root = parse_doc(twbx_path)
    if root is None:
        print(f"  No .twb or .tds found inside {twbx_path}")
        return []

    results = []
    for ds in datasource_elements(root):
        caption = ds_label(ds) or 'unknown'

        named_conn = ds.find('.//named-connection/connection')
        conn_class = named_conn.get('class', '') if named_conn is not None else ''
        schema = named_conn.get('schema', '') if named_conn is not None else ''

        conn_el = ds.find('connection')
        if conn_el is None:
            continue

        # Flatten the (possibly joined/unioned) relation tree into one query so the
        # calc-field materializer has a single base SELECT to bolt onto.
        sql, warnings, rel_count = flatten_joined_sql(conn_el)
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
            'flatten_warnings': warnings,
            'relation_count': rel_count,
        })

    return results


def make_header(workbook_name, datasource, conn_class, schema, variant=None,
                relation_count=None, flatten_warnings=None):
    """Build the SQL file header comment."""
    header = f"-- Source workbook: {workbook_name}\n"
    header += f"-- Datasource: {datasource}\n"
    header += f"-- Connection: {conn_class}"
    if schema:
        header += f" (schema: {schema})"
    if variant:
        header += f"\n-- Variant: {variant}"
    if relation_count and relation_count > 1:
        header += (f"\n-- Flattened from {relation_count} joined Custom SQL relations "
                   "into one query (CTE per relation). Review the reconstructed JOINs "
                   "and translated ON-clauses below.")
    if flatten_warnings:
        for w in flatten_warnings:
            header += f"\n-- WARNING: {w}"
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
                             ds['connection_class'], ds['schema'],
                             relation_count=ds.get('relation_count'),
                             flatten_warnings=ds.get('flatten_warnings'))
        with open(base_filepath, 'w', encoding='utf-8') as f:
            f.write(header + ds['sql'] + '\n')
        rc = ds.get('relation_count') or 1
        print(f"  Saved: {base_filename}" + (f"  (flattened {rc} joined relations)" if rc > 1 else ""))
        for w in ds.get('flatten_warnings', []):
            print(f"    WARNING: {w}")
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
                    variant=f"advanced ({len(included)} calculated fields added)",
                    relation_count=ds.get('relation_count'),
                    flatten_warnings=ds.get('flatten_warnings')
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
