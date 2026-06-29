"""reconstructor -- workbook-agnostic Tableau .twbx swap engines (pure stdlib).

Two operations, two engines:
  - reconstruct.py     Athena (Presto custom SQL) -> Snowflake view  (source swap)
  - production_swap.py Snowflake -> Snowflake table repoint          (production swap)

Plus extraction helpers used to inspect a workbook before swapping:
  - extract_custom_sql.py          base Custom SQL per datasource
  - extract_custom_sql_advanced.py base + translatable calc fields as CTE columns
  - extract_field_metadata.py      captions / calc formulas / SQL-column maps -> CSVs
  - verify_output.py               static, config-driven verification of a swap output

None of these modules depend on the `connectors` package or any third-party library
-- they are pure standard-library (zipfile, xml.etree, json, re, csv).
"""
