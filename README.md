<p align="center">
  <img src="assets/icon.png" alt="ClearQuery by Dinesh AI Academy" width="120">
</p>

<h1 align="center">ClearQuery: Analyst for SAP HANA</h1>

<p align="center">
  <b>Ask Claude about your SAP HANA data, in plain words. Read-only, self-healing, and honest about every number.</b><br>
  Built by <a href="https://dineshaiacademy.com">Dinesh AI Academy</a> · MIT licensed · free for personal and commercial use
</p>

---

ClearQuery gives Claude **35 read-only tools** for SAP HANA and a skill (`hana-analyst`) that teaches Claude to work
like a careful analyst: look before it queries, use the real column names, aggregate in SQL, and show its work.

## What you can ask

- "Which schemas and tables do I have? Find the tables with a customer number."
- "Describe table VBRK and show five sample rows."
- "Total net sales by month for last year, highest first. Show the SQL."
- "Why is this query slow? Show its execution plan."
- "What depends on this view? What may my login read?"
- "The connection isn't working. What's wrong?"

## Install (Claude Code)

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once. It fetches the right Python and the
   packages for you.
2. In Claude Code, add the plugin:

   ```
   /plugin install clearquery --marketplace Dinesh-AI-Academy-Claude-Works/clearquery
   ```

   On older Claude Code versions, use the two-step form:

   ```
   /plugin marketplace add Dinesh-AI-Academy-Claude-Works/clearquery
   /plugin install clearquery@dinesh-ai-academy
   ```

3. Fill in the form: your HANA host, a **read-only user** and its password. The schema and the query timeout are
   optional. Change them later with `/plugin` > Installed > ClearQuery > Configure, then `/reload-plugins`.
4. The first start downloads the packages once (about half a minute). Then ask: *"Test the connection, then list the
   tables."*

To try a local copy before installing: `claude --plugin-dir <path to this folder>`.

### Create a read-only user (recommended)

Run once as an administrator, then use `MCP_READER` in the form:

```sql
CREATE USER MCP_READER PASSWORD "<a strong password>" NO FORCE_FIRST_PASSWORD_CHANGE;
GRANT SELECT ON SCHEMA <YOUR_SCHEMA> TO MCP_READER;
-- optional, for the performance tools:
GRANT CATALOG READ TO MCP_READER;
```

## Where it works

| App | Works? |
|---|---|
| **Claude Code** (terminal, VS Code, desktop Code tab) | Yes. Prompts you for the connection details when you install. |
| **Cowork** (session on your computer) | Loads the skill. The server needs its connection details, which Cowork does not prompt for. |
| **Claude Desktop chat** (Windows, macOS, Linux) | Yes, with the one-click extension `clearquery-<version>.mcpb` (see below). |
| **claude.ai in the browser** | No: the browser cannot start a local server that reaches your database. |

## Install (Claude Desktop chat)

1. Download `clearquery-<version>.mcpb` from the
   [latest release](https://github.com/Dinesh-AI-Academy-Claude-Works/clearquery/releases/latest).
2. Double-click it, or drag it into Claude Desktop > **Settings** > **Extensions**, and choose **Install**.
3. Fill in the form (host, port, read-only user, password; schema optional). For HANA Express turn
   **Validate TLS certificate** off. Your password is kept in Claude Desktop's secure storage.
4. Enable the extension. The first start downloads Python and the packages once (no `uv` or Python install needed).
   Then, in a new chat: *"Diagnose the connection, then list my tables."*

The chat has no `hana-analyst` skill, but the server gives Claude the same working rules.

## Built to keep working

| When this happens | ClearQuery does this |
|---|---|
| HANA or the network drops the connection | Notices before the next query, reconnects, and retries a failed query once (safe, because every query is read-only). |
| The connection sits idle for a long time | Checks it before reuse and replaces it if HANA closed it. |
| A brief network hiccup during login | Tries once more after a short pause. |
| A wrong password | Does **not** retry by itself, and pauses login attempts for two minutes, so your database user is not locked. |
| A table name in the wrong case, `SCHEMA.TABLE` in one field, or a synonym | Resolves it to the real object and says so. |
| A misspelled table, column or schema | Answers with "did you mean …" and the schema where the object does exist. |
| A HANA error (syntax, privileges, types, memory) | Translates it into what went wrong and what to do next. |
| A query runs too long | HANA stops it at the timeout you set (default 120 s), and Claude is told how to make it lighter. |
| A huge value, LOB or binary column, or a very wide result | Cuts it with a clear marker instead of flooding the conversation. |
| A bug or unexpected error | Catches it, logs it, answers politely, and keeps the server running. |

When setup is the problem, ask Claude to **diagnose the connection**. `diagnose_connection` checks settings, host name,
network port, TLS certificate, login, schema and privileges, and names the step that fails with the fix. It also warns
you if your login is an administrator or can change data.

### Troubleshooting at a glance

| Message mentions | Fix |
|---|---|
| "not set up yet" | Open Configure and fill in host, user and password. |
| "web server answered" / HTTP 503 | Your HANA Cloud instance is stopped. Start it in HANA Cloud Central and wait about 3 minutes. |
| "host name can't be found" | Copy the SQL endpoint from HANA Cloud Central, without `https://`. |
| "no answer" / timed out | Check the VPN, and add your IP to the instance's *Allowed connections*. |
| "certificate" | HANA Express or self-signed: set *Validate TLS certificate* to `false`. |
| "password was rejected" | Correct it in Configure, then `/reload-plugins`. |

## The 35 tools

| Group | Tools |
|---|---|
| Connection | `test_connection` · `diagnose_connection` · `get_session_info` · `show_config` · `show_env_vars` |
| Find | `list_schemas` · `list_tables` · `list_views` · `list_calculation_views` · `list_procedures` · `list_functions` · `list_sequences` · `list_synonyms` · `search_tables` · `search_columns` |
| Describe | `describe_table` · `explain_table` · `describe_view` · `describe_procedure` · `describe_function` · `describe_index` · `get_ddl` |
| Structure | `list_indexes` · `list_constraints` · `list_foreign_keys` · `get_dependencies` · `list_privileges` |
| Data | `run_query` · `query_next_page` · `get_sample_data` · `get_column_stats` |
| Performance | `get_table_stats` · `get_partition_info` · `explain_plan` · `get_expensive_queries` |

Every tool is marked read-only to Claude.

## Safety

- **Read-only twice over.** The server sends only single `SELECT` / `WITH` queries (and `EXPLAIN PLAN` of one). It
  checks queries with a parser that understands strings, quoted names and comments, and rejects several statements,
  data-changing keywords, `SELECT … INTO` and `FOR UPDATE` row locks. And you connect with a database user that only
  has SELECT, so the database enforces it too. Never use an admin user.
- Claude should never ask for your password in the chat. Settings are entered in Configure.

## What the plugin runs and sends (Privacy)

- **Runs:** `uv run --locked` starts one local Python program, `server/hana_mcp_server.py`, in this plugin. `uv` installs
  the exact packages pinned in `uv.lock` from PyPI: the MCP Python SDK and SAP's `hdbcli` database driver (SAP Developer
  License Agreement; you accept SAP's terms by installing it). Nothing else is downloaded or executed.
- **Connects to:** only the HANA host you enter, with the user and password you enter. Your password is kept in Claude
  Code's secure storage, not in a file. `diagnose_connection` also opens a plain network and TLS test connection to
  that same host and port.
- **Sends to Dinesh AI Academy:** nothing. There is no telemetry, no analytics and no account. Query results go to Claude
  in your conversation, like any tool result, so only connect data you are allowed to share with Claude.
- **Stores:** nothing. The server keeps no files and no history. Its diagnostic log (stderr, kept by Claude Code)
  records connection events and tool names, never your password or query results.
- **Data retention and sharing:** none by the plugin. Anthropic's own terms apply to your conversations.
- Privacy policy: <https://dineshaiacademy.com/privacy-policy.html> · Contact: support@dineshaiacademy.com

## For developers

Offline tests (no HANA needed; a fake driver simulates dropped connections, wrong passwords and HANA errors):

```
uv run --locked python tests/test_offline.py
```

Live tests against your own HANA (reads the connection from a git-ignored `.env` in this folder, only reads data):

```
uv run --locked python tests/test_live.py ACDOCA EKKO KNA1
```

Build the Claude Desktop extension (needs Node.js for `npx`; output in `dist/`):

```
uv run --locked python scripts/build_mcpb.py
```

To live-test a built extension the way Claude Desktop starts it, unpack it
(`npx @anthropic-ai/mcpb unpack dist/clearquery-<version>.mcpb <folder>`) and run the live tests with
`CLEARQUERY_EXTENSION_DIR=<folder>`.

## What's new in 2.2.0

- **Claude Desktop extension** (`.mcpb`): one-click install with a settings form, password in secure storage, and
  Python managed by Claude Desktop.
- Public synonyms such as `DUMMY` are found even when a default schema is set.
- `explain_plan` explains HANA's extra privilege rule for system views instead of giving generic advice.
- Live test suite (`tests/test_live.py`), verified against SAP HANA 2.0 SPS08 (HANA Express).

## What's new in 2.1.0

- Self-healing connection: reused, checked, reopened and retried automatically.
- Protection from account lockout after a wrong password.
- `diagnose_connection`: a step-by-step connection doctor with a least-privilege check.
- "Did you mean" suggestions for unknown tables, columns, schemas and indexes; automatic case, synonym and
  `SCHEMA.TABLE` resolution.
- A stronger read-only guard that understands strings and comments.
- A query timeout setting, plus limits on large values and results.
- Smarter paging with `next_offset`, for `WITH` queries too.

## License and trademarks

MIT License (see `LICENSE`). Brand, trademarks and third-party software: see `NOTICE.md`. SAP and SAP HANA are
trademarks of SAP SE. This plugin is an independent product of Dinesh AI Academy and is not affiliated with, sponsored
by or endorsed by SAP SE or Anthropic.

<p align="center">
  <b>ClearQuery</b> is crafted by <a href="https://dineshaiacademy.com">Dinesh AI Academy</a>: practical AI for real
  business data.<br>
  Source and issues: <a href="https://github.com/Dinesh-AI-Academy-Claude-Works/clearquery">GitHub</a> ·
  Support: support@dineshaiacademy.com
</p>
