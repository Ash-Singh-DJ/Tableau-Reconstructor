"""
deploy_view.py -- deploy a translated gold view to Snowflake and smoke-test it.

Generic replacement for a per-workbook deploy script: point it at a .sql file
containing a `CREATE OR REPLACE VIEW <db>.<schema>.<name> AS ...` statement (the
output of the translate step), run it, then COUNT(*) and dump INFORMATION_SCHEMA
columns so you can fill in the reconstruct config's `casing` + calc binding types.

This is the only engine-side helper that talks to Snowflake; it imports the
standalone `connectors` package. The swap engines themselves never touch a DB.

Usage:
    python deploy_view.py path/to/gold_view.sql --fqn DB.SCHEMA.VIEW_NAME
    python deploy_view.py path/to/gold_view.sql            # skip smoke test (no --fqn)
"""

import argparse
import os
import sys

# make the repo root importable so `connectors` resolves no matter the cwd
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from connectors import execute_snowflake_query


def deploy(sql_path, fqn=None):
    with open(sql_path, 'r', encoding='utf-8') as f:
        ddl = f.read()

    print(f"=== Deploying view from {os.path.basename(sql_path)} ===")
    result = execute_snowflake_query(ddl)
    print(result)

    if not fqn:
        print("\nNo --fqn supplied; skipping smoke test / column dump.")
        return

    db, schema, table = fqn.split('.')
    print(f"\n=== Smoke test: row count for {fqn} ===")
    df = execute_snowflake_query(f"SELECT COUNT(*) AS row_count FROM {fqn}")
    print(f"{fqn}: {df.iloc[0, 0]} rows")

    print(f"\n=== Columns (INFORMATION_SCHEMA) for {fqn} ===")
    cols = execute_snowflake_query(f"""
        SELECT column_name, data_type, ordinal_position
        FROM {db}.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema = '{schema}' AND table_name = '{table}'
        ORDER BY ordinal_position
    """)
    print(cols.to_string(index=False))


def main(argv=None):
    p = argparse.ArgumentParser(description='Deploy a gold view to Snowflake and smoke-test it.')
    p.add_argument('sql', help='path to a .sql file with a CREATE OR REPLACE VIEW statement')
    p.add_argument('--fqn', help='fully-qualified DB.SCHEMA.VIEW for the smoke test + column dump')
    args = p.parse_args(argv)
    deploy(args.sql, fqn=args.fqn)
    return 0


if __name__ == '__main__':
    sys.exit(main())
