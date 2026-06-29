"""connectors -- standalone Athena + Snowflake query helpers.

Independent of the reconstructor engine; import the query functions directly:

    from connectors import execute_athena_query, execute_snowflake_query
"""

from .db import (
    execute_athena_query,
    execute_snowflake_query,
    close_snowflake_connection,
)

__all__ = [
    'execute_athena_query',
    'execute_snowflake_query',
    'close_snowflake_connection',
]
