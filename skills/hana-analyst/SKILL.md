---
name: hana-analyst
description: Answer questions about data in SAP HANA safely and accurately with ClearQuery by Dinesh AI Academy. Use when the user asks about HANA tables, schemas, columns, sales, finance or any business figures stored in SAP HANA, wants a query explained, checked or tuned, wants to set up or configure ClearQuery or its SAP HANA credentials, or the HANA connection is not working.
---

**ClearQuery** by Dinesh AI Academy gives you a read-only SAP HANA connection through the `hana` MCP server (35
tools), when it runs on the user's computer. If its tools are not available, read "If the ClearQuery tools are not
available" below before anything else. Otherwise use them like this.

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

## If the ClearQuery tools are not available

First work out where you are running, because the fix depends on it:

- **claude.ai in a web browser (or any chat without local tools):** ClearQuery's HANA tools cannot run here. They
  run as a small program on the user's own computer, and only that computer can reach their database (often
  `localhost` or a company network). This is expected, not an error. Tell the user plainly, then offer the two ways
  to use ClearQuery with their data (see **Setup guide** below): the **Claude Desktop app** with the ClearQuery
  extension, or **Claude Code**. You can still help without the tools: explain HANA concepts, review or write SQL
  for them to run, and read results they paste in. Never invent figures from their database.
- **Only one tool, `clearquery_setup_status`, is available:** ClearQuery could not start on this computer (for
  example an unsupported system, a blocked download, or a full disk). Call it once and explain the result in plain
  words: what happened, why, and the options it lists. It also checks again, so after the user fixed something,
  calling it tells you whether ClearQuery is ready (then the user restarts it as the result says).
- **Claude Code or the Desktop Code tab, and no ClearQuery tools at all:** the usual cause on a new computer is that
  `uv` is not installed (Python itself is not needed). Tell the user to install uv (Windows:
  `winget install --id=astral-sh.uv -e`, macOS: `brew install uv`, or
  https://docs.astral.sh/uv/getting-started/installation/), then open a new terminal and start Claude Code again.
  If uv is installed, suggest `/mcp` to see ClearQuery's status and its error.
- **Claude Desktop chat, and no ClearQuery tools:** the extension is not installed or is turned off. See the setup
  guide.
- Do not install software or change the user's system yourself unless they ask you to.

## If the tools say it is not set up, or cannot connect

Do not retry in a loop. Call `diagnose_connection` **once**: it checks settings, host name, network port, TLS
certificate, login, schema and privileges, and says which step fails and how to fix it. Tell the user that step and
fix in plain words; the message names the right settings screen for their app. After a rejected password,
ClearQuery pauses automatic login attempts for two minutes so the database user is not locked; tell the user to
correct the password rather than trying again.

## Setup guide (walk the user through it when asked, or when ClearQuery is not set up)

Pick the user's app. Ask if it is unclear. Give one step at a time if they seem new to this.

**Claude Desktop app (chat):**
1. Download `clearquery-<version>.mcpb` from
   https://github.com/Dinesh-AI-Academy-Claude-Works/clearquery/releases/latest
2. Double-click it (or drag it into Settings > Extensions) and choose Install.
3. Fill in the form, then make sure ClearQuery is turned on. The first start takes about half a minute.

**Claude Code (terminal, VS Code, Desktop Code tab):**
1. Install uv once (see above).
2. `/plugin install clearquery --marketplace Dinesh-AI-Academy-Claude-Works/clearquery`
3. `/plugin` > Installed > ClearQuery > Configure, fill in the form, then `/reload-plugins`. `/mcp` should show
   ClearQuery connected.

**The form, field by field:**

| Field | HANA Cloud | HANA Express (Docker) or on-premise |
|---|---|---|
| Host | SQL endpoint from HANA Cloud Central, without `https://` | `localhost`, or the server name |
| Port | `443` | `39041` for HANA Express tenant HXE; ask the admin otherwise |
| User / password | a **read-only** user, e.g. `MCP_READER` (never an admin) | same |
| Default schema | optional, e.g. `SALES` | optional |
| Use TLS encryption | on | on (off only if the admin says TLS is disabled) |
| Validate TLS certificate | on | **off** for HANA Express or a self-signed certificate |
| Query timeout | 120 (default) | 120 (default) |

Then ask: "Diagnose the connection". Every step should say OK. Never ask for the password in the chat: it belongs
only in the form, which stores it securely. If the user has no read-only user yet, the administrator can create one:
`CREATE USER MCP_READER PASSWORD "<strong password>" NO FORCE_FIRST_PASSWORD_CHANGE;` then
`GRANT SELECT ON SCHEMA <schema> TO MCP_READER;`

## Answering

- Use only numbers the tools returned. Never estimate or invent a figure. If a result is empty, truncated or trimmed,
  say so.
- Show the SQL you ran when the user asks, and mention the schema and the date range you used.
- State assumptions plainly, for example how you treated cancelled documents or which currency you summed.

## Safety

The connection is read-only. Never ask the user for their database password in the chat. If they need to change a
setting, send them to Configure. If `test_connection` or `diagnose_connection` warns that the login is an
administrator or can change data, pass that warning on and recommend a read-only user.
