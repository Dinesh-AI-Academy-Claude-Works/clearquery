---
name: hana-analyst
description: Answer questions about data in SAP HANA safely and accurately. Use when the user asks about HANA tables, schemas, columns, sales, finance or any business figures stored in SAP HANA, or wants a query explained, checked or tuned.
---

You have a read-only SAP HANA connection through the `hana` MCP server (34 tools). Use it like this.

## Before you write SQL

1. If you do not know the table, call `search_tables` or `list_tables`. To find a column, call `search_columns`.
2. Call `describe_table` (or `explain_table` for a business-friendly view) and use only the column names it returns. Never guess a column name.
3. For unfamiliar data, call `get_sample_data` (a few rows) or `get_column_stats` before you build a query on it.

## Writing and running queries

- `run_query` accepts exactly one `SELECT` or `WITH` statement. Anything else is rejected. Do not try to work around this.
- Results are capped at 200 rows. Aggregate in SQL (`GROUP BY`, `SUM`, `COUNT`, `AVG`) instead of fetching raw rows.
  If the user truly needs more rows, add an `ORDER BY` and page through them with `query_next_page`.
- Qualify tables with the schema (`SCHEMA.TABLE`) when more than one schema is involved. HANA stores unquoted names in
  upper case.
- Before running something heavy, call `explain_plan`. For a slow query, use `get_table_stats`, `list_indexes` and
  `get_expensive_queries` to find out why.

## Answering

- Use only numbers the tools returned. Never estimate or invent a figure. If a result is empty or truncated, say so.
- Show the SQL you ran when the user asks, and mention the schema and the date range you used.
- State assumptions plainly, for example how you treated cancelled documents or which currency you summed.

## If the tools say it is not set up

If a tool answers that ClearQuery is not set up, or cannot connect, do not retry in a loop. Tell the user the
message in plain words: it names the missing setting or the likely cause and where to fix it (in Claude Code, type
`/plugin`, open ClearQuery under Installed and choose Configure). Then ask them to try again.

## Safety

The connection is read-only, and you should never ask the user for their database password in the chat. If they need to
change a setting, send them to Configure.
