---
name: hana-analyst
description: Answer questions about data in SAP HANA safely and accurately with ClearQuery by Dinesh AI Academy. Use when the user asks about HANA tables, schemas, columns, sales, finance or any business figures stored in SAP HANA, wants a query explained, checked or tuned, or the HANA connection is not working.
---

You have a read-only SAP HANA connection through the `hana` MCP server: **ClearQuery** by Dinesh AI Academy (35
tools). Use it like this.

## Before you write SQL

1. If you do not know the table, call `search_tables` or `list_tables`. To find a column, call `search_columns`.
   `search_tables` also looks in other schemas when nothing matches in the working one.
2. Call `describe_table` (or `explain_table` for a business-friendly view) and use only the column names it returns.
   Never guess a column name.
3. For unfamiliar data, call `get_sample_data` (a few rows) or `get_column_stats` before you build a query on it.

Names are forgiving: `orders`, `ORDERS`, `sales.orders` and synonyms all resolve. When a tool says it used a name in a
different case, or followed a synonym, use that exact name in your SQL.

## Writing and running queries

- `run_query` accepts exactly one `SELECT` or `WITH` statement. Writes, `FOR UPDATE`, `INTO` and several statements
  are rejected. Do not try to work around this. A column really named like a keyword (for example `UPDATE`) must be
  written in double quotes.
- Results are capped at 200 rows. Aggregate in SQL (`GROUP BY`, `SUM`, `COUNT`, `AVG`) instead of fetching raw rows.
  If the user truly needs more rows, add an `ORDER BY` and page with `query_next_page`, using the `next_offset` the
  previous answer gave.
- Qualify tables with the schema (`SCHEMA.TABLE`) when more than one schema is involved. HANA stores unquoted names in
  upper case; mixed-case names need double quotes (`"MyTable"`).
- Before running something heavy, call `explain_plan`. For a slow query, use `get_table_stats`, `list_indexes` and
  `get_expensive_queries` to find out why. Queries that run longer than the configured timeout are stopped by HANA.

## When a tool returns an error

ClearQuery's errors say what went wrong and what to do. Read them and act on them:

- **"Did you mean ..."**: the table, column or schema does not exist, and the closest real names are listed. Use one of
  them, or the schema where the object does exist, and run the corrected query once.
- **Syntax, data type or ambiguous column errors**: the message gives the HANA rule to follow. Fix the SQL and retry
  once. If the second try also fails, show the user the error and your SQL instead of trying variations blindly.
- **Insufficient privilege**: the user's database login cannot read that object. Tell them the GRANT the message
  names; do not look for another way in.
- **Query stopped after N seconds / out of memory**: filter, aggregate or add `TOP` before you try again.
- **The answer was trimmed**: select fewer or narrower columns.

The server reconnects by itself when HANA or the network drops the connection, so a single dropped connection is
not something you need to handle.

## If ClearQuery itself could not start

- **Only one tool, `clearquery_setup_status`, is available:** ClearQuery could not start on this computer (for
  example an unsupported system, a blocked download, or a full disk). Call it once and explain the result to the user
  in plain words: what happened, why, and the options it lists. It also checks again, so after the user fixed
  something, calling it tells you whether ClearQuery is ready (then the user restarts it as the result says).
- **No ClearQuery tools at all:** the usual cause on a new computer is that `uv` is not installed (Claude Code
  needs it to start ClearQuery; Python itself is not needed). Tell the user to install uv
  (Windows: `winget install --id=astral-sh.uv -e`, macOS: `brew install uv`, or
  https://docs.astral.sh/uv/getting-started/installation/), then open a new terminal and start Claude Code again.
  If uv is installed, suggest `/mcp` to see ClearQuery's status and its error.
- Do not install software or change the user's system yourself unless they ask you to.

## If the tools say it is not set up, or cannot connect

Do not retry in a loop. Call `diagnose_connection` **once**: it checks settings, host name, network port, TLS
certificate, login, schema and privileges, and says which step fails and how to fix it. Tell the user that step and
fix in plain words. Settings are changed in Claude Code with `/plugin` > Installed > ClearQuery > Configure, then
`/reload-plugins`. After a rejected password, ClearQuery pauses automatic login attempts for two minutes so the
database user is not locked; tell the user to correct the password rather than trying again.

## Answering

- Use only numbers the tools returned. Never estimate or invent a figure. If a result is empty, truncated or trimmed,
  say so.
- Show the SQL you ran when the user asks, and mention the schema and the date range you used.
- State assumptions plainly, for example how you treated cancelled documents or which currency you summed.

## Safety

The connection is read-only. Never ask the user for their database password in the chat. If they need to change a
setting, send them to Configure. If `test_connection` or `diagnose_connection` warns that the login is an
administrator or can change data, pass that warning on and recommend a read-only user.
