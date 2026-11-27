# ClearQuery: Analyst for SAP HANA

Ask Claude about your **SAP HANA** data. The plugin gives Claude 34 **read-only** tools to explore your database, plus a
skill (`hana-analyst`) that teaches Claude to look before it queries, aggregate in SQL, and show its work.

**ClearQuery** is built by [Dinesh AI Academy](https://dineshaiacademy.com). MIT licensed: free for personal and commercial use.

## What you can ask

- "Which schemas and tables do I have? Find the tables with a customer number."
- "Describe table VBRK and show five sample rows."
- "Total net sales by month for last year, highest first. Show the SQL."
- "Why is this query slow? Show its execution plan."
- "What depends on this view? Who has privileges on this table?"

## Where it works

| App | Works? |
|---|---|
| **Claude Code** (terminal, VS Code, desktop Code tab) | Yes. Prompts you for the connection details when you install. |
| **Cowork** (session on your computer) | Loads the skill. The server needs its connection details, which Cowork does not prompt for. |
| **claude.ai and the desktop app's normal chat** | Not the server: chat does not start local servers. For Claude Desktop chat, use the one-click extension instead (see below). |

## Install (Claude Code)

To try it from a copy of this repository before it is listed: `claude --plugin-dir <path to this folder>`.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once. It fetches the right Python and the
   packages for you.
2. Add the plugin from the Claude directory (`/plugin`), and fill in the form: your HANA host, a **read-only user** and
   its password. The schema is optional. Change them later with `/plugin` > Installed > ClearQuery > Configure.
3. The first start downloads the packages once (about half a minute). Then ask: "Test the connection, then list the
   tables."

If something is wrong, Claude answers with the likely cause and the fix in plain words: a wrong password, a stopped HANA
Cloud instance, a closed port, a self-signed certificate, a mistyped host.

For **Claude Desktop chat**, Dinesh AI Academy also provides a one-click `.mcpb` extension for Windows, macOS and Linux.
Ask at support@dineshaiacademy.com.

## Safety

- **Read-only twice over.** The server sends only `SELECT` or `WITH` statements and refuses everything else, and you
  should connect with a database user that has only the SELECT privilege (for example `MCP_READER`). Never use an admin
  user.
- Claude should never ask for your password in the chat. Settings are entered in Configure.

## What the plugin runs and sends (Privacy)

- **Runs:** `uv run --locked` starts one local Python program, `server/hana_mcp_server.py`, in this plugin. `uv` installs
  the exact packages pinned in `uv.lock` from PyPI: the MCP Python SDK and SAP's `hdbcli` database driver (SAP Developer
  License Agreement; you accept SAP's terms by installing it). Nothing else is downloaded or executed.
- **Connects to:** only the HANA host you enter, with the user and password you enter. Your password is kept in Claude
  Code's secure storage, not in a file.
- **Sends to Dinesh AI Academy:** nothing. There is no telemetry, no analytics and no account. Query results go to Claude
  in your conversation, like any tool result, so only connect data you are allowed to share with Claude.
- **Stores:** nothing. The server keeps no files and no history.
- **Data retention and sharing:** none by the plugin. Anthropic's own terms apply to your conversations.
- Privacy policy: <https://dineshaiacademy.com/privacy-policy.html> · Contact: support@dineshaiacademy.com

## License and trademarks

MIT License (see `LICENSE`). Brand, trademarks and third-party software: see `NOTICE.md`. SAP and SAP HANA are trademarks of SAP SE. This plugin is an independent product of Dinesh AI
Academy and is not affiliated with, sponsored by or endorsed by SAP SE or Anthropic.

Source and issues: <https://github.com/Dinesh-AI-Academy-Claude-Works/clearquery> · Dinesh AI Academy: <https://dineshaiacademy.com>
