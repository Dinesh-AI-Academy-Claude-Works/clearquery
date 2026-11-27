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

## Set up in 3 steps

### 1. Pick where you use Claude

ClearQuery's HANA tools run **on your own computer**, because only your computer can reach your database (often
`localhost` or a company network).

| You use | Works with your HANA? | Go to |
|---|---|---|
| **Claude Desktop app**, normal chat | ✔ | [2a. Claude Desktop](#2a-claude-desktop-app) |
| **Claude Code**: terminal, VS Code, or the Desktop **Code** tab | ✔ | [2b. Claude Code](#2b-claude-code) |
| **claude.ai** in a web browser | Skill only: a browser can't run local tools. Claude explains this and can still review SQL you run yourself | Use one of the above for your data |
| **Cowork** | Skill; tools only when the session runs on your computer (no settings form) | Prefer Claude Desktop or Claude Code |

### 2a. Claude Desktop app

1. Download **`clearquery-<version>.mcpb`** from the
   [latest release](https://github.com/Dinesh-AI-Academy-Claude-Works/clearquery/releases/latest).
2. Double-click it, or drag it into Claude Desktop > **Settings** > **Extensions**, then choose **Install**.
3. The settings form opens: fill it in as in [step 3](#3-fill-in-the-settings-form), and make sure ClearQuery is
   turned **on**. Later: **Settings** > **Extensions** > **ClearQuery** > **Configure**.

Nothing else to install: Claude Desktop downloads Python and the packages on first start (about half a minute).

### 2b. Claude Code

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once (Windows:
   `winget install --id=astral-sh.uv -e`, macOS: `brew install uv`). Python itself is not needed.
2. Add the plugin:

   ```
   /plugin install clearquery --marketplace Dinesh-AI-Academy-Claude-Works/clearquery
   ```

   On older Claude Code versions: `/plugin marketplace add Dinesh-AI-Academy-Claude-Works/clearquery`, then
   `/plugin install clearquery@dinesh-ai-academy`.
3. Open the settings form: `/plugin` > **Installed** > **ClearQuery** > **Configure**. Fill it in as in
   [step 3](#3-fill-in-the-settings-form), then type `/reload-plugins`.
4. `/mcp` should show ClearQuery **connected** with 35 tools.

Start Claude Code from any folder except ClearQuery's own folder. To try a local copy:
`claude --plugin-dir <path to this folder>`.

### 3. Fill in the settings form

| Field | HANA Cloud | HANA Express (Docker) or on-premise |
|---|---|---|
| **Host** | SQL endpoint from HANA Cloud Central, without `https://` | `localhost`, or the server's name |
| **Port** | `443` | `39041` (HANA Express, tenant HXE); otherwise ask your admin |
| **User** | a **read-only** user, e.g. `MCP_READER` (never an admin) | same |
| **Password** | its password, kept in secure storage, never in a file or the chat | same |
| **Default schema** | optional, e.g. `SALES` | optional |
| **Use TLS encryption** | on | on |
| **Validate TLS certificate** | on | **off** for HANA Express or a self-signed certificate |
| **Query timeout** | `120` (default) | `120` (default) |

Then ask Claude: ***"Diagnose the connection."*** Every step should say **OK**. If one doesn't, it says what to fix.

**No read-only user yet?** An administrator runs this once:

```sql
CREATE USER MCP_READER PASSWORD "<a strong password>" NO FORCE_FIRST_PASSWORD_CHANGE;
GRANT SELECT ON SCHEMA <YOUR_SCHEMA> TO MCP_READER;
-- optional, for the performance tools:
GRANT CATALOG READ TO MCP_READER;
```

## What you can ask

- "Which schemas and tables do I have? Find the tables with a customer number."
- "Describe table VBRK and show five sample rows."
- "Total net sales by month for last year, highest first. Show the SQL."
- "Why is this query slow? Show its execution plan."
- "What depends on this view? What may my login read?"
- "The connection isn't working. What's wrong?"

## Supported computers

No Python installation is needed anywhere. Claude Desktop (extension) or `uv` (plugin) downloads a private Python and
the exact packages on first start, which needs internet access once: Python from `releases.astral.sh` (older uv
versions: `github.com`), packages from `pypi.org` and `files.pythonhosted.org`. After that, ClearQuery connects only to
your HANA host.

| Computer | ClearQuery |
|---|---|
| Windows 10 / 11 on Intel or AMD (64-bit) | ✔ |
| Windows 11 on ARM (Snapdragon, e.g. Surface Laptop 7) | ✔ Runs SAP's driver through Windows' built-in Intel emulation, automatically |
| macOS 13 or newer, Apple silicon or Intel | ✔ |
| Linux on x86_64, ARM64 or ppc64le | ✔ |
| Windows 10 on ARM | ✘ Windows 10 can't emulate 64-bit Intel programs. Upgrading to Windows 11 fixes it |
| 32-bit Windows, macOS 12 or older | ✘ SAP's driver or a required library is not available |

SAP publishes its HANA driver (`hdbcli`) only for these systems. On an unsupported computer ClearQuery does not just
"fail": Claude explains what happened and the options (for example "upgrade to Windows 11" or "use another
computer"). The same goes for a blocked download, a company proxy, a full disk or antivirus locking files, and
fixable problems are retried automatically first.

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
| "uv tool is not installed" (shown when Claude Code starts) | Install uv: `winget install --id=astral-sh.uv -e` (Windows), `brew install uv` (macOS), or [any system](https://docs.astral.sh/uv/getting-started/installation/). Open a new terminal and start Claude Code again. |
| "ClearQuery could not start: … could not be downloaded" | First start needs internet. Check VPN/proxy, or start once on another network; after that it runs from the cache. Then `/mcp` → Reconnect. |
| "ClearQuery could not start: … not supported" | This computer can't run SAP's driver. The message names the fix, if there is one (e.g. upgrade to Windows 11). |

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

Launcher tests (unsupported computers, blocked downloads, Windows on ARM; no HANA needed, about 1 minute):

```
uv run --locked python tests/test_launcher.py
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

## What's new in 2.3.1

- **Clear setup guide** at the top of this page, and inside the skill, so Claude can walk anyone through setup in
  their own app.
- **claude.ai in the browser:** Claude now explains that the HANA tools run on your computer and points to Claude
  Desktop or Claude Code, instead of giving install advice that doesn't apply there.
- **Dinesh AI Academy logo** announced by the server when it loads (64 px inline and 512 px), size-tagged icons in the
  Desktop extension, and documentation, support and privacy links for the plugin directory listing.

## What's new in 2.3.0

- **Works on more computers, and says so clearly when it can't.** A new launcher prepares the computer before the
  server starts. It runs Windows 11 on ARM laptops through Intel emulation. It retries a company proxy's
  certificates and antivirus file locks by itself. Otherwise it explains the problem (unsupported system, blocked
  download, full disk) with the user's options, and re-checks after a fix.
- **Missing `uv` is announced when Claude Code starts**, with install commands, instead of a silent "failed" server.
- Tests for both: `tests/test_launcher.py` (7 situations) and the start-up check in Git Bash, sh, PowerShell 5.1
  and 7.

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
