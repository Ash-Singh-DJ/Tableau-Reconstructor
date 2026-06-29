"""
db.py -- Athena (Presto) and Snowflake query connectors.

A self-contained pair of query helpers, independent of the reconstructor engine.
Both load credentials from a `.env` file (see `.env.example`) and return a pandas
DataFrame. The reconstruct/production-swap engines do NOT import this module --
they are pure stdlib. Only the deploy/discover helper scripts (which need to run a
Snowflake query) use it.

    from connectors import execute_athena_query, execute_snowflake_query

Required environment variables
------------------------------
Athena:     AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_STAGING_DIR (optional),
            AWS_REGION (optional, default us-east-1)
Snowflake:  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_WAREHOUSE,
            SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_ROLE (optional),
            SNOWFLAKE_AUTHENTICATOR (optional, default externalbrowser),
            SNOWFLAKE_PASSWORD (only if authenticator != externalbrowser)
"""

import os
import time

import pandas as pd
from dotenv import load_dotenv, find_dotenv


# ── Athena (Presto) ───────────────────────────────────────────────────────────
def execute_athena_query(sql):
    """Execute an Athena (Presto) query using credentials from the .env file.
    Returns a pandas DataFrame."""
    from pyathena import connect
    from pyathena.pandas.cursor import PandasCursor

    load_dotenv(override=True)

    aws_access_key = os.getenv('AWS_ACCESS_KEY_ID')
    athena_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    s3_staging_dir = os.getenv('S3_STAGING_DIR', 's3://djcdp-athena-queryresults-prod/')
    region_name = os.getenv('AWS_REGION', 'us-east-1')

    if not aws_access_key or not athena_secret_key:
        raise ValueError("AWS credentials not found. Please check your .env file.")

    cur = connect(aws_access_key_id=aws_access_key,
                  aws_secret_access_key=athena_secret_key,
                  s3_staging_dir=s3_staging_dir,
                  region_name=region_name,
                  cursor_class=PandasCursor).cursor()
    start_time = time.time()
    print("Executing Athena query...")
    df = cur.execute(sql).as_pandas()
    print(f"Query executed in {round((time.time() - start_time) / 60, 2)} minutes.")
    cur.close()
    return df


# ── Snowflake (singleton connection) ────────────────────────────────────────────
_snowflake_conn = None


def _get_snowflake_connection():
    """Return a reusable Snowflake connection (creates one on first call,
    reconnects if stale)."""
    global _snowflake_conn

    if _snowflake_conn is not None:
        try:
            _snowflake_conn.cursor().execute("SELECT 1")
            return _snowflake_conn
        except Exception:
            try:
                _snowflake_conn.close()
            except Exception:
                pass
            _snowflake_conn = None

    import snowflake.connector

    load_dotenv(find_dotenv())

    account = os.getenv('SNOWFLAKE_ACCOUNT')
    user = os.getenv('SNOWFLAKE_USER')
    warehouse = os.getenv('SNOWFLAKE_WAREHOUSE')
    database = os.getenv('SNOWFLAKE_DATABASE')
    schema = os.getenv('SNOWFLAKE_SCHEMA')
    role = os.getenv('SNOWFLAKE_ROLE')
    authenticator = os.getenv('SNOWFLAKE_AUTHENTICATOR', 'externalbrowser')

    if not all([account, user, warehouse, database, schema]):
        raise ValueError("Snowflake credentials not found. Please check your .env file.")

    connection_params = {
        'account': account,
        'user': user,
        'warehouse': warehouse,
        'database': database,
        'schema': schema,
        'authenticator': authenticator,
        'client_session_keep_alive': True,
    }

    if authenticator != 'externalbrowser':
        password = os.getenv('SNOWFLAKE_PASSWORD')
        if not password:
            raise ValueError("SNOWFLAKE_PASSWORD required when not using external browser authentication")
        connection_params['password'] = password

    if role:
        connection_params['role'] = role

    print("Authenticating with Snowflake... (browser will open once)")
    _snowflake_conn = snowflake.connector.connect(**connection_params)
    print("Connected.")
    return _snowflake_conn


def close_snowflake_connection():
    """Explicitly close the singleton connection (optional -- for clean shutdown)."""
    global _snowflake_conn
    if _snowflake_conn is not None:
        try:
            _snowflake_conn.close()
        except Exception:
            pass
        _snowflake_conn = None


def execute_snowflake_query(sql):
    """Execute a Snowflake query using the singleton connection (authenticates once
    per session). Returns a pandas DataFrame."""
    conn = _get_snowflake_connection()

    start_time = time.time()
    print("Executing Snowflake query...")
    df = pd.read_sql(sql, conn)
    print(f"Query executed in {round((time.time() - start_time) / 60, 2)} minutes.")
    return df
