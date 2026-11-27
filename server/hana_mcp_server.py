"""ClearQuery: Analyst for SAP HANA, by Dinesh AI Academy (https://dineshaiacademy.com).

A read-only SAP HANA MCP server, written with the MCP Python SDK + SAP's hdbcli driver (Python only).

Read-only twice over: connect as a database user that can only SELECT (for example MCP_READER), and every statement
this server sends is a single SELECT / WITH query (or an EXPLAIN PLAN of one). No tool can change data.

Built to keep working when things go wrong:
  * One connection is reused, checked before use, and silently reopened if HANA or the network dropped it. A query
    that fails because the link was lost is retried once (safe: every query is read-only).
  * A rejected password is never retried automatically, so a typo cannot lock the database user.
  * Names are forgiving: wrong upper/lower case, SCHEMA.TABLE in one argument, synonyms and views are resolved, and
    an unknown name comes back with "did you mean ..." suggestions and the schema where it does exist.
  * Every HANA error is translated into what went wrong and what to do next. Unexpected errors are caught, logged
    to stderr and answered politely: the server keeps running.
  * Results are bounded: rows, cell size, LOBs, binary values and total answer size, plus a query timeout.

35 tools, in six groups (names are what Claude sees):
  Connection  test_connection · diagnose_connection · get_session_info · show_config · show_env_vars
  Find        list_schemas · list_tables · list_views · list_calculation_views · list_procedures · list_functions ·
              list_sequences · list_synonyms · search_tables · search_columns
  Describe    describe_table · explain_table · describe_view · describe_procedure · describe_function ·
              describe_index · get_ddl
  Structure   list_indexes · list_constraints · list_foreign_keys · get_dependencies · list_privileges
  Data        run_query · query_next_page · get_sample_data · get_column_stats
  Performance get_table_stats · get_partition_info · explain_plan · get_expensive_queries

Claude Code starts this over stdio through the plugin's .mcp.json.
"""
from __future__ import annotations

import atexit
import difflib
import functools
import inspect
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from hdbcli import dbapi
from mcp.server.mcpserver import MCPServer  # mcp 2.x (was FastMCP in 1.x)
from mcp.server.mcpserver.exceptions import ToolError  # message reaches Claude; other exceptions are hidden
from mcp.types import ToolAnnotations

VERSION = "2.1.0"
BRAND = "Dinesh AI Academy"
SUPPORT = "support@dineshaiacademy.com"
WEBSITE = "https://dineshaiacademy.com"

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[ClearQuery] %(levelname)s %(message)s")
log = logging.getLogger("clearquery")

# True when running inside the Claude Desktop extension (manifest.json sits next to the server folder).
IS_EXTENSION = (Path(__file__).resolve().parent.parent / "manifest.json").exists()
# True when running as a Claude plugin (the plugin's .claude-plugin folder sits next to the server folder).
IS_PLUGIN = (Path(__file__).resolve().parent.parent / ".claude-plugin").is_dir()
_PRODUCT = "ClearQuery" if IS_PLUGIN else "SAP HANA Analyst"  # the name the user sees where this runs
_WHERE = ("Claude Desktop > Settings > Extensions > SAP HANA Analyst > Configure" if IS_EXTENSION
          else "Claude Code: type /plugin, open ClearQuery under Installed and choose Configure" if IS_PLUGIN
          else "the .env file in the project folder (then restart Claude)")
_RESTART = ("quit Claude Desktop from the tray icon and open it again" if IS_EXTENSION
            else "type /reload-plugins, or restart Claude Code" if IS_PLUGIN
            else "restart Claude")
_HANA_KEYS = ("HANA_HOST", "HANA_PORT", "HANA_USER", "HANA_PASSWORD", "HANA_SCHEMA", "HANA_ENCRYPT",
              "HANA_VALIDATE_CERT", "HANA_QUERY_TIMEOUT")

# ---------------------------------------------------------------------------------------------- limits
MAX_ROWS = 200                 # rows per query result
MAX_LIST_ROWS = 2000           # rows for catalog listings (tables, columns ...)
MAX_CELL_CHARS = 4000          # one value; longer text is cut with a marker
MAX_BINARY_BYTES = 64          # binary values are shown as hex, this many bytes at most
MAX_RESPONSE_CHARS = 150_000   # whole answer; keeps Claude's context usable
MAX_DDL_CHARS = 100_000        # a view / procedure definition
MAX_SQL_CHARS = 100_000        # size of a query Claude may send
MAX_CLIENT_SKIP = 100_000      # rows a page may skip when the query cannot be paged in SQL
CONNECT_TIMEOUT_MS = 10_000
PING_AFTER_IDLE_S = 60         # check an idle connection before reusing it
AUTH_COOLDOWN_S = 120          # after a rejected password, wait before trying again (protects the user from lockout)
SUGGEST_TIMEOUT_S = 15         # catalog look-ups used only for "did you mean" suggestions


# ---------------------------------------------------------------------------------------------- settings
def _clean_env() -> None:
    """Make the HANA_* settings safe to use, whoever supplied them (.env, Claude's settings form, a hand-edited config).
    Blank values and still-unfilled "${user_config.x}" placeholders count as not set. Quotes and spaces around values
    are dropped (never from the password). A host pasted as https://host:443/ becomes host, and its port is used
    when no port was given. [IPv6]:port is understood too."""
    for key in _HANA_KEYS:
        value = os.environ.get(key) or ""
        if key != "HANA_PASSWORD":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
        if not value.strip() or value.lstrip().startswith("${"):
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    host = os.environ.get("HANA_HOST", "")
    if host:
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host, flags=re.I)
        host = host.split("/")[0].split("?")[0].split("#")[0].strip()
        host = host.rsplit("@", 1)[-1]  # user@host pasted from a connection string
        port = ""
        bracketed = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", host)
        if bracketed:  # [IPv6] or [IPv6]:port
            host, port = bracketed.group(1), bracketed.group(2) or ""
        elif host.count(":") == 1:  # host:port (a bare IPv6 address has several colons)
            host, port = host.split(":")
        if port.isdigit():
            os.environ.setdefault("HANA_PORT", port)
        host = host.strip().rstrip(".")
        if host:
            os.environ["HANA_HOST"] = host
        else:
            os.environ.pop("HANA_HOST", None)


def _flag(key: str, default: bool = True) -> bool:
    """A true/false setting. Accepts true/false, yes/no, 1/0, on/off in any case; anything else keeps the default."""
    value = (os.environ.get(key) or "").strip().lower()
    return True if value in ("true", "yes", "1", "on", "y") else False if value in ("false", "no", "0", "off", "n") \
        else default


def _missing_settings() -> list[str]:
    labels = {"HANA_HOST": "the HANA host", "HANA_USER": "the user name", "HANA_PASSWORD": "the password"}
    return [label for key, label in labels.items() if not os.environ.get(key)]


def _port() -> int:
    raw = os.environ.get("HANA_PORT", "443")
    try:
        port = int(float(raw))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ToolError(f"The HANA port '{raw}' is not a valid port number (1 to 65535). HANA Cloud uses 443 and "
                        f"HANA Express 39041. Correct it in {_WHERE}.")
    return port


def _query_timeout() -> int:
    """Seconds a query may run before HANA cancels it. Setting: HANA_QUERY_TIMEOUT, 5 to 3600, default 120."""
    try:
        return max(5, min(3600, int(float(os.environ.get("HANA_QUERY_TIMEOUT", "120")))))
    except ValueError:
        return 120


_clean_env()


def _env_schema() -> str:
    """HANA_SCHEMA, upper-cased unless written in double quotes (HANA stores unquoted names in upper case).
    Empty = the login's default schema."""
    raw = (os.environ.get("HANA_SCHEMA") or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('""', '"')
    return raw.upper()


# The schema Claude works in. Empty = the login's own default schema in HANA.
DEFAULT_SCHEMA = _env_schema()
_SCHEMA_TEXT = f"schema {DEFAULT_SCHEMA}" if DEFAULT_SCHEMA else "the login's default schema"
QUERY_TIMEOUT_S = _query_timeout()

mcp = MCPServer(
    "sap-hana-analyst",
    title=f"{_PRODUCT} · {BRAND}",
    description=("Read-only SAP HANA analyst by Dinesh AI Academy: business questions, schema exploration, DDL, "
                 "statistics and query plans, with self-healing connections and plain-language errors."),
    instructions=(
        f"{_PRODUCT} by {BRAND}: read-only access to SAP HANA. Names without a schema refer to {_SCHEMA_TEXT} "
        "(test_connection shows which one). Look before you query: list_tables / search_tables, then describe_table "
        "to check the real column names before writing SQL. Aggregate in SQL (GROUP BY, SUM, COUNT) instead of "
        f"fetching raw rows: results are capped at {MAX_ROWS} rows (use query_next_page for more). run_query accepts "
        "one SELECT or WITH statement; anything else is rejected. Error messages say what went wrong and what to do: "
        "follow them (they often include 'did you mean' names) instead of guessing. If the connection fails, call "
        "diagnose_connection once and relay its findings; do not retry in a loop. Use only numbers returned by the "
        "tools, and show the SQL you used when asked."
    ),
    website_url=WEBSITE,
    version=VERSION,
)


# ---------------------------------------------------------------------------------------------- error knowledge
# What the driver's connect error text means, and what to do about it. First match wins.
_CONNECT_HINTS = (
    (("authentication failed", "invalid user name or password"),
     "The user name or password was rejected. Check both in {where}. HANA passwords are case-sensitive. "
     "(ClearQuery does not retry a rejected password by itself, so the user is not locked by repeated attempts.)"),
    (("forced to change password", "password has expired", "password is expired", "password change"),
     "This HANA user must change its password first. Ask the database administrator to reset it, then update it "
     "in {where}."),
    (("locked", "deactivated"),
     "This HANA user is locked or deactivated (too many wrong passwords?). Ask the database administrator to "
     "unlock it."),
    (("certificate", "ssl", "tls", "handshake"),
     "The TLS certificate was not accepted. HANA Express and some on-premise systems use a self-signed certificate: "
     "turn off 'Validate TLS certificate' in {where}. For HANA Cloud keep it on, and check this PC's date and time."),
    (("received http", "invalid connect reply", "http 4", "http 5"),
     "A web server answered, not SAP HANA. For HANA Cloud this almost always means the instance is stopped (trial "
     "instances stop automatically every night): start it in SAP HANA Cloud Central and retry in about 3 minutes. "
     "Otherwise the port is wrong (HANA Cloud uses 443)."),
    (("socket closed by peer", "connection reset", "connection closed", "broken pipe"),
     "HANA answered and then hung up. It is usually still starting (HANA Express needs 4 to 10 minutes after "
     "`docker start`; `docker ps` must say healthy), or the encryption setting does not match the server (try "
     "switching 'Use TLS encryption' in {where})."),
    (("refused", "no connection could be made"),
     "Nothing is listening at that address. The database is not running, or the port is wrong (HANA Cloud 443, HANA "
     "Express 39041). Start the database and retry."),
    (("cannot resolve", "resolve host", "getaddrinfo", "name or service not known", "nodename nor servname",
      "unknown host", "host not found", "no such host", "temporary failure in name resolution"),
     "The host name can't be found. Check its spelling in {where}: copy the SQL endpoint from SAP HANA Cloud "
     "Central, without https://."),
    (("timed out", "timeout", "unreachable", "no route"),
     "No answer from the server. Check the internet connection and any VPN. For HANA Cloud your IP address must be on "
     "the instance's allowed list (Instance > Manage > Allowed connections), and the instance must be running."),
    (("invalid schema name",),
     "The default schema does not exist or this user cannot see it. Correct it, or clear it, in {where}."),
)

_AUTH_WORDS = ("authentication failed", "invalid user name or password", "forced to change password",
               "password has expired", "password is expired", "user is locked", "deactivated")
_TRANSIENT_CONNECT_WORDS = ("timed out", "timeout", "connection reset", "socket closed", "closed by peer",
                            "broken pipe", "temporarily", "try again")
# Errors that mean the session is gone (not that the query is wrong): reconnect and retry once.
_LOST_CODES = {-10807, -10108, -10821, -10806, -10709}
_LOST_WORDS = ("connection down", "session not connected", "connection is closed", "connection closed",
               "socket closed", "closed by peer", "connection reset", "broken pipe", "not connected",
               "session has been reconnected", "connection lost", "communication error", "invalid connection")


def _explain_connect_error(text: str, host: str, port: int) -> str:
    """Turn a driver error into: what is wrong, what to do, and the technical detail (for Claude and for support)."""
    low = text.lower()
    meaning = next((hint for words, hint in _CONNECT_HINTS if any(w in low for w in words)),
                   "Check the host, port, user name and password in {where}, and that the database is running.")
    return (f"Cannot connect to SAP HANA at {host}:{port}. {meaning.format(where=_WHERE)} "
            f"Tip: diagnose_connection checks each step and names the one that fails. (Details: {text[:240]})")


def _err_code(e: BaseException):
    code = getattr(e, "errorcode", None)
    return code if isinstance(code, int) else None


def _err_text(e: BaseException) -> str:
    return str(getattr(e, "errortext", None) or e)


def _is_connection_lost(e: BaseException) -> bool:
    if isinstance(e, (OSError, ConnectionError)):
        return True
    if _err_code(e) in _LOST_CODES:
        return True
    low = _err_text(e).lower()
    return any(w in low for w in _LOST_WORDS)


def _is_timeout(e: BaseException) -> bool:
    low = _err_text(e).lower()
    return _err_code(e) == 139 or "timeout" in low or "cancelled" in low or "canceled" in low


# HANA SQL error code -> what to do. The message HANA gives is always included as well.
_SQL_HINTS = {
    4: "HANA ran out of memory for this query. Filter earlier (WHERE), aggregate (GROUP BY), select fewer columns, "
       "or add TOP / LIMIT.",
    7: "This SQL feature is not supported here. Rewrite the query with standard SELECT syntax.",
    257: "SQL syntax error. HANA tips: use TOP n or LIMIT n (not FETCH FIRST in old versions), single quotes for text "
         "('ABC'), double quotes for mixed-case names (\"MyTable\"), || to join strings, and no trailing commas.",
    258: "The connected user lacks a privilege for this. Ask the database administrator for SELECT on the schema "
         "(GRANT SELECT ON SCHEMA <schema> TO <user>). list_privileges shows what this user may read.",
    259: "Table or view not found.",
    260: "Column not found. Call describe_table on the table and use only the column names it returns.",
    266: "Data types do not match. Convert explicitly: TO_DATE('2024-01-31'), TO_DECIMAL(col), TO_NVARCHAR(col), "
         "TO_INTEGER(col).",
    268: "A column name is ambiguous: it exists in more than one joined table. Prefix it with the table alias (t.COL).",
    274: "A value is too large for its target type. CAST to a larger type.",
    276: "A required parameter or value is missing.",
    286: "A query or subquery returns too many columns or rows for this context.",
    303: "Invalid DATE / TIME / TIMESTAMP value. Use ISO format: '2024-01-31' or TO_DATE('31.01.2024', 'DD.MM.YYYY').",
    305: "A subquery used as a single value returned more than one row. Add TOP 1, an aggregate, or a tighter WHERE.",
    314: "Numeric overflow. CAST to DECIMAL(38, 2) or BIGINT before summing.",
    328: "Function or procedure not found. Check its spelling, or list_functions.",
    339: "A text value could not be converted to a number. Filter out non-numeric values or use TO_DECIMAL on clean "
         "data only.",
    362: "Schema not found, or this user cannot see it.",
    2048: "HANA column-store error, usually memory or a very large intermediate result. Filter and aggregate earlier.",
}


# ---------------------------------------------------------------------------------------------- connection manager
@dataclass
class _Health:
    connects: int = 0
    reconnects: int = 0
    retried_queries: int = 0
    last_error: str = ""
    started: float = field(default_factory=time.time)


class _Hana:
    """One reused, self-healing connection. All database work runs through run(), one call at a time."""

    def __init__(self) -> None:
        self._conn = None
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._auth_failed_at = 0.0
        self._auth_error = ""
        self.health = _Health()

    # -- opening and checking
    def _open(self, bypass_cooldown: bool = False):
        missing = _missing_settings()
        if missing:
            raise ToolError(
                f"{_PRODUCT} is not set up yet. Missing: {', '.join(missing)}. Tell the user to open {_WHERE}, "
                "enter the HANA host, a read-only user and its password (the default schema is optional), save, and "
                f"try again. If this message stays, {_RESTART}. Nothing else is needed.")
        host, port = os.environ["HANA_HOST"], _port()
        waited = time.monotonic() - self._auth_failed_at
        if self._auth_failed_at and waited < AUTH_COOLDOWN_S and not bypass_cooldown:
            raise ToolError(
                f"The last login was rejected ({self._auth_error[:160]}). To protect the user from being locked, "
                f"{_PRODUCT} waits {int(AUTH_COOLDOWN_S - waited)} more seconds before it tries again. Fix the user "
                f"name or password in {_WHERE}, then {_RESTART}.")
        options = {"currentSchema": DEFAULT_SCHEMA} if DEFAULT_SCHEMA else {}
        for attempt in (1, 2):
            try:
                conn = dbapi.connect(address=host, port=port, user=os.environ["HANA_USER"],
                                     password=os.environ["HANA_PASSWORD"], encrypt=_flag("HANA_ENCRYPT"),
                                     sslValidateCertificate=_flag("HANA_VALIDATE_CERT"),
                                     connectTimeout=CONNECT_TIMEOUT_MS, **options)
            except (dbapi.Error, OSError) as e:
                text = _err_text(e)
                low = text.lower()
                self.health.last_error = text[:300]
                if any(w in low for w in _AUTH_WORDS):
                    self._auth_failed_at, self._auth_error = time.monotonic(), text
                    log.warning("login rejected; automatic retries paused for %ss", AUTH_COOLDOWN_S)
                elif attempt == 1 and any(w in low for w in _TRANSIENT_CONNECT_WORDS):
                    log.info("connect attempt failed (%s); retrying once", text[:120])
                    time.sleep(1.5)
                    continue
                raise ToolError(_explain_connect_error(text, host, port)) from e
            self._auth_failed_at, self._auth_error = 0.0, ""
            try:
                conn.setautocommit(True)
                conn.setclientinfo("APPLICATION", f"ClearQuery {VERSION} ({BRAND})")
            except Exception:  # client info is a courtesy for DBAs; never fail on it
                pass
            self.health.connects += 1
            log.info("connected to %s:%s", host, port)
            return conn
        raise ToolError(_explain_connect_error("connection failed", host, port))  # not reached

    def _alive(self, conn) -> bool:
        try:
            if not conn.isconnected():
                return False
            if time.monotonic() - self._last_used > PING_AFTER_IDLE_S:
                return bool(conn.ping())
            return True
        except Exception:
            return False

    def _connection(self, bypass_cooldown: bool = False):
        if self._conn is not None and not self._alive(self._conn):
            log.info("connection was closed by the server or network; reopening")
            self.drop()
            self.health.reconnects += 1
        if self._conn is None:
            self._conn = self._open(bypass_cooldown)
            self._last_used = time.monotonic()
        return self._conn

    def drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # -- running work
    def run(self, work, sql: str | None = None, bypass_cooldown: bool = False):
        """Run work(conn) on a healthy connection. A lost connection is reopened and the work retried once (safe:
        every statement is read-only). HANA errors become plain-language ToolErrors with suggestions."""
        with self._lock:
            for attempt in (1, 2):
                conn = self._connection(bypass_cooldown)
                try:
                    result = work(conn)
                    self._last_used = time.monotonic()
                    return result
                except ToolError:
                    self._last_used = time.monotonic()
                    raise
                except (dbapi.Error, OSError) as e:
                    self.health.last_error = _err_text(e)[:300]
                    if _is_connection_lost(e):
                        self.drop()
                        if attempt == 1:
                            log.info("connection lost during a query (%s); reconnecting and retrying once",
                                     _err_text(e)[:120])
                            self.health.reconnects += 1
                            self.health.retried_queries += 1
                            continue
                        raise ToolError(
                            "The connection to SAP HANA dropped twice in a row, so the query was stopped. HANA may be "
                            "restarting, or the network is unstable. Wait a minute and try again, or call "
                            f"diagnose_connection. (Details: {_err_text(e)[:200]})") from e
                    self._last_used = time.monotonic()
                    raise ToolError(_explain_sql_error(e, conn, sql)) from e
        raise ToolError("The query could not be completed.")  # not reached


_hana = _Hana()
atexit.register(_hana.drop)


# ---------------------------------------------------------------------------------------------- results
def _cell(v, limit: int = MAX_CELL_CHARS):
    """One value, made safe to send: LOBs read up to the limit, binary as hex, long text cut with a marker."""
    if v is None:
        return None
    if isinstance(v, dbapi.LOB):
        try:
            v = v.read(limit + 1)
        except Exception:
            return "[large object: could not be read]"
        if v is None:
            return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        text = "0x" + b[:MAX_BINARY_BYTES].hex().upper()
        return text + (f"… [binary, {len(b)} bytes]" if len(b) > MAX_BINARY_BYTES else "")
    if isinstance(v, Decimal):
        s = format(v, "f")
    elif isinstance(v, str):
        s = v
    else:
        s = str(v)
    if len(s) > limit:
        s = s[:limit] + f"… [cut at {limit:,} characters]"
    return s


def _result(cur, limit: int) -> dict:
    if not cur.description:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False,
                "note": "The statement returned no result set."}
    columns = [d[0] for d in cur.description]
    raw = cur.fetchmany(limit + 1)
    more = len(raw) > limit
    rows, size, trimmed = [], 0, False
    for r in raw[:limit]:
        row = [_cell(v) for v in r]
        size += sum(len(c) for c in row if c) + 4 * len(row)
        if size > MAX_RESPONSE_CHARS and rows:
            trimmed = True
            break
        rows.append(row)
    out = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": more or trimmed}
    if trimmed:
        out["note"] = (f"The answer was trimmed to {len(rows)} rows to stay readable (about {MAX_RESPONSE_CHARS:,} "
                       "characters). Select fewer or narrower columns, or aggregate in SQL.")
    return out


def _execute(conn, sql: str, params: tuple = (), timeout: int | None = None):
    cur = conn.cursor()
    try:
        cur.setquerytimeout(timeout or QUERY_TIMEOUT_S)
    except Exception:
        pass
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)  # no binding: a '?' inside the user's text stays text
    return cur


def _rows(conn, sql: str, params: tuple = (), limit: int = MAX_ROWS, timeout: int | None = None) -> dict:
    cur = _execute(conn, sql, params, timeout)
    try:
        return _result(cur, limit)
    finally:
        _close(cur)


def _fetch(conn, sql: str, params: tuple = (), limit: int = 1000, timeout: int | None = None) -> list[tuple]:
    """Raw rows for the server's own catalog look-ups."""
    cur = _execute(conn, sql, params, timeout)
    try:
        return [tuple(r) for r in cur.fetchmany(limit)]
    finally:
        _close(cur)


def _close(cur) -> None:
    try:
        cur.close()
    except Exception:
        pass


def _query(sql: str, params: tuple = (), limit: int = MAX_ROWS) -> dict:
    return _hana.run(lambda conn: _rows(conn, sql, params, limit), sql)


def _table(columns: list[str], rows: list[list], **extra) -> dict:
    out = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}
    out.update({k: v for k, v in extra.items() if v})
    return out


def _with_notes(result: dict, *notes: str) -> dict:
    notes = [n for n in notes if n]
    if notes:
        existing = result.get("note")
        result["note"] = " ".join(([existing] if existing else []) + notes)
    return result


# ---------------------------------------------------------------------------------------------- SQL guard
_BLOCKED_WORDS = ("INSERT", "UPDATE", "DELETE", "UPSERT", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER", "GRANT",
                  "REVOKE", "CALL", "INTO", "COMMIT", "ROLLBACK", "RENAME", "COMMENT", "IMPORT", "EXPORT", "UNLOAD")


def _mask(sql: str) -> tuple[str, str]:
    """Two same-length copies of sql: cleaned (comments blanked) and masked (comments, 'strings' and "names"
    blanked). Checks run on masked text, so a ';' or keyword inside a literal never confuses them."""
    cleaned, masked = list(sql), list(sql)
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            for j in range(i, end):
                cleaned[j] = masked[j] = " "
            i = end
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ToolError("The query has a /* comment that is never closed with */.")
            for j in range(i, end + 2):
                if sql[j] != "\n":
                    cleaned[j] = masked[j] = " "
            i = end + 2
        elif ch in ("'", '"'):
            j = i + 1
            while True:
                j = sql.find(ch, j)
                if j < 0:
                    what = "text value (')" if ch == "'" else "quoted name (\")"
                    raise ToolError(f"The query has a {what} that is never closed.")
                if j + 1 < n and sql[j + 1] == ch:  # '' or "" is an escaped quote
                    j += 2
                    continue
                break
            for k in range(i + 1, j):
                masked[k] = "x" if ch == '"' else " "
            i = j + 1
        else:
            i += 1
    return "".join(cleaned), "".join(masked)


def _top_level(masked: str) -> str:
    """The masked text with everything inside parentheses blanked: only the outer statement remains."""
    out, depth = [], 0
    for ch in masked:
        if ch == "(":
            depth += 1
            out.append(" ")
        elif ch == ")":
            depth = max(0, depth - 1)
            out.append(" ")
        else:
            out.append(ch if depth == 0 else " ")
    return "".join(out)


@dataclass
class _Sql:
    text: str        # the statement to send: comments removed, trailing ';' removed
    masked: str      # same, with literals blanked (for checks)
    first: str       # SELECT or WITH

    def top_has(self, word: str) -> bool:
        return re.search(rf"\b{word}\b", _top_level(self.masked).upper()) is not None


def _read_only_sql(sql: str) -> _Sql:
    """The statement if it is a single read-only SELECT / WITH; otherwise a ToolError that tells Claude why."""
    if not sql or not sql.strip():
        raise ToolError("Give a SQL query: one SELECT or WITH statement.")
    if len(sql) > MAX_SQL_CHARS:
        raise ToolError(f"The query is {len(sql):,} characters long; the limit is {MAX_SQL_CHARS:,}. Simplify it.")
    if "\x00" in sql:
        raise ToolError("The query contains a NUL character. Remove it.")
    cleaned, masked = _mask(sql)
    end = len(masked.rstrip(" \t\r\n;"))
    cleaned, masked = cleaned[:end].strip(), masked[:end].strip()
    if not masked:
        raise ToolError("The query is empty (only comments). Give one SELECT or WITH statement.")
    if ";" in masked:
        raise ToolError("Only one statement is allowed. Remove the ';' between statements and run them one by one.")
    first = re.match(r"[\s(]*([A-Za-z]+)", masked)
    first_word = first.group(1).upper() if first else ""
    if first_word not in ("SELECT", "WITH"):
        raise ToolError(f"Only read-only SELECT / WITH queries are allowed (this one starts with {first_word or '?'}). "
                        f"{_PRODUCT} never changes data.")
    upper = masked.upper()
    if re.search(r"\bFOR\s+(UPDATE|SHARE)\b", upper):
        raise ToolError("FOR UPDATE / FOR SHARE locks rows and is not allowed. Remove it: a plain SELECT is enough.")
    for word in _BLOCKED_WORDS:
        if re.search(rf"(?<![\w$#.]){word}(?![\w$#])", upper):
            raise ToolError(f"The keyword {word} is not allowed in a read-only query. If {word} is a column or table "
                            f"name, write it in double quotes (\"{word}\").")
    return _Sql(text=cleaned, masked=masked, first=first_word)


# ---------------------------------------------------------------------------------------------- names
def _cap(n, top: int = MAX_ROWS) -> int:
    try:
        return max(1, min(int(n), top))
    except (TypeError, ValueError):
        return min(100, top)


def _unquote(part: str) -> str:
    """A name as HANA stores it: "Exact Case" keeps its case, anything else is upper-cased."""
    part = part.strip()
    if len(part) >= 2 and part[0] == part[-1] == '"':
        return part[1:-1].replace('""', '"')
    return part.upper()


def _split_name(text: str) -> list[str]:
    """Split SCHEMA.OBJECT at dots outside double quotes."""
    parts, buf, quoted = [], [], False
    for ch in text.strip():
        if ch == '"':
            quoted = not quoted
        if ch == "." and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _name(value: str, what: str) -> str:
    """A single name as HANA stores it. Must not be empty."""
    value = (value or "").strip()
    if not value or value in ('""',):
        raise ToolError(f"Give the {what} name.")
    if len(value) > 256:
        raise ToolError(f"The {what} name is too long (HANA names have at most 256 characters).")
    return _unquote(value)


def _qualified(value: str, schema: str, what: str) -> tuple[str | None, str]:
    """(schema or None, name) from a name that may be written SCHEMA.NAME, plus an optional schema argument."""
    value = (value or "").strip()
    if not value:
        raise ToolError(f"Give the {what} name.")
    parts = _split_name(value)
    if len(parts) == 2 and all(p.strip() for p in parts):
        sch, name = _name(parts[0], "schema"), _name(parts[1], what)
        given = _name(schema, "schema") if schema and schema.strip() else None  # explicit argument only
        if given and given.upper() != sch.upper():
            raise ToolError(f"'{value}' names schema {sch}, but schema='{schema}' was also given. Use one of them.")
        return sch, name
    if len(parts) > 2:
        raise ToolError(f"'{value}' is not a valid {what} name. Write NAME or SCHEMA.NAME.")
    return _schema_in(schema), _name(value, what)


def _schema_in(schema: str) -> str | None:
    """The schema argument as HANA stores it, else the configured default, else None (= the login's schema)."""
    if schema and schema.strip():
        return _name(schema, "schema")
    return DEFAULT_SCHEMA or None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _like(text: str) -> str:
    text = (text or "").strip().strip('"').upper()
    if not text:
        raise ToolError("Give the text to search for, for example 'ORDER'.")
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _closest(word: str, candidates, n: int = 5) -> list[str]:
    """Names that look like word: close spellings first, then names that contain it."""
    pool = {c.upper(): c for c in candidates if c}
    w = word.upper()
    picks = [pool[m] for m in difflib.get_close_matches(w, list(pool), n=n, cutoff=0.6)]
    if len(picks) < n and len(w) >= 3:
        picks += [orig for up, orig in pool.items() if w in up and orig not in picks][: n - len(picks)]
    return picks[:n]


def _current_schema(conn) -> str:
    try:
        return _fetch(conn, "SELECT CURRENT_SCHEMA FROM DUMMY", limit=1)[0][0]
    except Exception:
        return "the current schema"


def _resolve_schema(conn, schema: str | None) -> str | None:
    """The schema exactly as HANA stores it (wrong case is corrected), or a ToolError with suggestions."""
    if not schema:
        return None
    if _fetch(conn, "SELECT SCHEMA_NAME FROM SYS.SCHEMAS WHERE SCHEMA_NAME = ?", (schema,), 1):
        return schema
    found = _fetch(conn, "SELECT SCHEMA_NAME FROM SYS.SCHEMAS WHERE UPPER(SCHEMA_NAME) = UPPER(?)", (schema,), 5)
    if len(found) == 1:
        return found[0][0]
    if len(found) > 1:
        raise ToolError(f"Several schemas match '{schema}' in different upper/lower case: "
                        f"{', '.join(r[0] for r in found)}. Write the one you mean in double quotes.")
    try:
        names = [r[0] for r in _fetch(conn, "SELECT SCHEMA_NAME FROM SYS.SCHEMAS", limit=20000,
                                      timeout=SUGGEST_TIMEOUT_S)]
    except Exception:
        names = []
    close = _closest(schema, names)
    hint = f" Did you mean: {', '.join(close)}?" if close else " Call list_schemas to see the schemas you can read."
    raise ToolError(f"Schema {schema} does not exist, or this user cannot see it.{hint}")


@dataclass
class _Obj:
    schema: str
    name: str
    type: str
    note: str = ""

    @property
    def ref(self) -> str:
        return f"{_quote(self.schema)}.{_quote(self.name)}"

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}"


_TABLE_LIKE = ("TABLE", "VIEW", "SYNONYM")
_KIND_WORDS = {"TABLE": "table", "VIEW": "view", "SYNONYM": "synonym", "PROCEDURE": "procedure",
               "FUNCTION": "function", "SEQUENCE": "sequence", "INDEX": "index"}


def _kinds_text(types) -> str:
    words = [_KIND_WORDS.get(t, t.lower()) for t in types if t != "SYNONYM"] or ["object"]
    return " or ".join(words)


def _resolve(conn, value: str, schema: str, types=_TABLE_LIKE, what: str = "table",
             follow_synonyms: bool = True) -> _Obj:
    """Find an object by name, forgiving case and SCHEMA.NAME, following synonyms. ToolError with suggestions if absent."""
    sch, name = _qualified(value, schema, what)
    sch = _resolve_schema(conn, sch)
    tlist = ", ".join(f"'{t}'" for t in types)
    base = "SELECT SCHEMA_NAME, OBJECT_NAME, OBJECT_TYPE FROM SYS.OBJECTS WHERE OBJECT_TYPE IN (" + tlist + ") AND "
    found = _fetch(conn, base + "SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND OBJECT_NAME = ?", (sch, name), 5)
    if not found:
        found = _fetch(conn, base + "SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND UPPER(OBJECT_NAME) = UPPER(?)",
                       (sch, name), 5, timeout=SUGGEST_TIMEOUT_S)
        if len(found) > 1:
            raise ToolError(f"Several objects match '{name}' in different upper/lower case: "
                            f"{', '.join(r[1] for r in found)}. Write the one you mean in double quotes.")
    if not found and not sch and "SYNONYM" in types:  # public synonyms such as DUMMY
        found = _fetch(conn, base + "SCHEMA_NAME = 'PUBLIC' AND OBJECT_NAME = ?", (name,), 1)
    if not found:
        raise ToolError(_not_found(conn, name, sch, types, what))
    obj = _Obj(*found[0])
    hops = 0
    while follow_synonyms and obj.type == "SYNONYM" and hops < 5:
        target = _fetch(conn, "SELECT OBJECT_SCHEMA, OBJECT_NAME, OBJECT_TYPE FROM SYS.SYNONYMS "
                              "WHERE SCHEMA_NAME = ? AND SYNONYM_NAME = ?", (obj.schema, obj.name), 1)
        if not target:
            break
        note = f"{obj.label} is a synonym for {target[0][0]}.{target[0][1]}."
        obj = _Obj(target[0][0], target[0][1], target[0][2] or "TABLE", note=(obj.note + " " + note).strip())
        hops += 1
    if obj.name != name and not obj.note:
        obj.note = f"Using {obj.label} (the name is stored in this upper/lower case)."
    return obj


def _not_found(conn, name: str, sch: str | None, types, what: str) -> str:
    where = sch or _current_schema(conn)
    tlist = ", ".join(f"'{t}'" for t in types)
    msg = f"No {_kinds_text(types)} named {name} in schema {where}."
    try:
        elsewhere = _fetch(conn, "SELECT SCHEMA_NAME, OBJECT_NAME, OBJECT_TYPE FROM SYS.OBJECTS WHERE OBJECT_TYPE IN ("
                           + tlist + ") AND UPPER(OBJECT_NAME) = UPPER(?) AND SCHEMA_NAME <> ?",
                           (name, where), 5, timeout=SUGGEST_TIMEOUT_S)
        if elsewhere:
            msg += " It exists in: " + ", ".join(f"{s}.{o} ({t.lower()})" for s, o, t in elsewhere) + \
                   f". Pass schema='{elsewhere[0][0]}' (or write {elsewhere[0][0]}.{elsewhere[0][1]})."
        names = [r[0] for r in _fetch(conn, "SELECT OBJECT_NAME FROM SYS.OBJECTS WHERE OBJECT_TYPE IN (" + tlist +
                                      ") AND SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA)", (sch,), 50000,
                                      timeout=SUGGEST_TIMEOUT_S)]
        close = _closest(name, names)
        if close:
            msg += f" Did you mean: {', '.join(close)}?"
        elif not elsewhere:
            msg += " Call search_tables with part of the name to look for it."
    except Exception:
        msg += " Call search_tables with part of the name to look for it."
    return msg


def _columns(conn, obj: _Obj) -> list[tuple]:
    """(COLUMN_NAME, DATA_TYPE_NAME) of a table or view, in order."""
    view = obj.type == "VIEW"
    return _fetch(conn, f"SELECT COLUMN_NAME, DATA_TYPE_NAME FROM SYS.{'VIEW_COLUMNS' if view else 'TABLE_COLUMNS'} "
                        f"WHERE SCHEMA_NAME = ? AND {'VIEW_NAME' if view else 'TABLE_NAME'} = ? ORDER BY POSITION",
                  (obj.schema, obj.name), 10000)


def _resolve_column(conn, obj: _Obj, column: str) -> tuple[str, str]:
    """(exact column name, data type), forgiving case; ToolError with suggestions if absent."""
    want = _name(column, "column")
    cols = _columns(conn, obj)
    for exact in (True, False):
        for name, dtype in cols:
            if (name == want) if exact else (name.upper() == want.upper()):
                return name, dtype
    close = _closest(want, [c[0] for c in cols])
    hint = f" Did you mean: {', '.join(close)}?" if close else " Call describe_table to see its columns."
    raise ToolError(f"{obj.label} has no column {want}.{hint}")


def _schema_or_current(conn, schema: str) -> str | None:
    return _resolve_schema(conn, _schema_in(schema))


# ---------------------------------------------------------------------------------------------- SQL errors
_RE_TABLE = re.compile(r"could not find table/view\s+(\S+?)\s+in schema\s+(\S+?)\s*:", re.I)
_RE_COLUMN = re.compile(r"invalid column name:\s*([^:]+?)\s*:", re.I)
_RE_SCHEMA = re.compile(r"invalid schema name:\s*([^:]+?)\s*:", re.I)
_RE_FROM = re.compile(r'\b(?:FROM|JOIN)\s+((?:"[^"]+"|[\w$#/]+)(?:\s*\.\s*(?:"[^"]+"|[\w$#/]+))?)', re.I)


def _explain_sql_error(e: BaseException, conn, sql: str | None) -> str:
    code, text = _err_code(e), _err_text(e)
    if _is_timeout(e):
        return (f"The query was stopped after {QUERY_TIMEOUT_S} seconds (or cancelled by HANA). Make it lighter: "
                "filter with WHERE, aggregate with GROUP BY, add TOP / LIMIT, or check explain_plan. The timeout can be "
                f"raised in {_WHERE}. (HANA: {text[:200]})")
    hint = _SQL_HINTS.get(abs(code) if code is not None else -1, "")
    extra = ""
    try:
        if conn is not None:
            if code == 259 and (m := _RE_TABLE.search(text)):
                extra = " " + _not_found(conn, m.group(1).strip('"'), m.group(2).strip('"'), _TABLE_LIKE, "table")
            elif code == 260 and (m := _RE_COLUMN.search(text)) and sql:
                extra = _suggest_columns(conn, sql, m.group(1).strip().strip('"'))
            elif code == 362 and (m := _RE_SCHEMA.search(text)):
                _resolve_schema(conn, m.group(1).strip().strip('"'))
    except ToolError as te:
        extra = " " + str(te)
    except Exception:
        pass
    label = f"HANA error {code}" if code is not None else "HANA error"
    return f"{label}: {text[:600]}" + (f" What to do: {hint}" if hint else "") + extra


def _suggest_columns(conn, sql: str, bad: str) -> str:
    """For an unknown column: the closest real columns in the tables the query reads."""
    tables = []
    for ref in _RE_FROM.findall(sql)[:6]:
        parts = [p.strip() for p in _split_name(ref)]
        try:
            obj = _resolve(conn, parts[-1], parts[0] if len(parts) == 2 else "")
            tables.append(obj)
        except Exception:
            continue
    names = []
    for obj in tables:
        try:
            names += [f"{obj.name}.{c[0]}" if len(tables) > 1 else c[0] for c in _columns(conn, obj)]
        except Exception:
            continue
    close = _closest(bad, names) or _closest(bad, [n.split(".")[-1] for n in names])
    return f" Did you mean: {', '.join(close)}?" if close else ""


# ---------------------------------------------------------------------------------------------- tool registration
_NAME_ARGS = {"table": "table", "view": "view", "object_name": "object", "procedure": "procedure",
              "function": "function", "index": "index"}


def _check_names(sig: inspect.Signature, args, kwargs) -> None:
    """Reject malformed names before any database work, so the user sees the real problem, not a connection error."""
    bound = sig.bind_partial(*args, **kwargs).arguments
    for arg, what in _NAME_ARGS.items():
        if arg in bound:
            _qualified(bound[arg], bound.get("schema", ""), what)
    if "column" in bound:
        _name(bound["column"], "column")


def _tool(title: str):
    """Register a read-only tool. Any unexpected error becomes a polite message; the server keeps running."""
    def decorate(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            started = time.monotonic()
            try:
                _check_names(sig, args, kwargs)
                return fn(*args, **kwargs)
            except ToolError:
                log.info("%s answered with guidance (an error the user or Claude can act on)", fn.__name__)
                raise
            except (dbapi.Error, OSError) as e:
                raise ToolError(_explain_sql_error(e, None, None)) from e
            except RecursionError as e:
                raise ToolError("The input is too deeply nested to process. Simplify it.") from e
            except Exception as e:  # a bug: never crash the server, never hide what happened
                log.exception("unexpected error in %s", fn.__name__)
                raise ToolError(
                    f"{_PRODUCT} hit an unexpected problem in {fn.__name__} ({type(e).__name__}: {str(e)[:200]}). "
                    f"The server is still running, so you can try again or another way. If it repeats, report it to "
                    f"{SUPPORT} with this message.") from e
            finally:
                log.debug("%s took %.2fs", fn.__name__, time.monotonic() - started)

        mcp.tool(title=title, annotations=ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False,
                                                          idempotentHint=True, openWorldHint=False))(guarded)
        return guarded
    return decorate


def _list(sql_from_where: str, schema: str, columns: str, order: str, limit: int = MAX_LIST_ROWS,
          empty: str = "") -> dict:
    """A catalog listing for one schema, with the schema checked first (wrong case corrected, typos suggested)."""
    def work(conn):
        sch = _schema_or_current(conn, schema)
        out = _rows(conn, f"SELECT {columns} FROM {sql_from_where} AND SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) "
                          f"ORDER BY {order}", (sch,), limit)
        out["schema"] = sch or _current_schema(conn)
        if not out["rows"] and empty:
            out["note"] = empty.format(schema=out["schema"])
        elif out["truncated"]:
            _with_notes(out, f"Only the first {len(out['rows'])} are shown. Use search_tables / search_columns "
                             "to narrow down.")
        return out
    return _hana.run(work)


# ---------------------------------------------------------------------------------------------- connection tools
@_tool("Test connection")
def test_connection() -> dict:
    """Check the HANA connection and return the current user, schema, database version and server time."""
    def work(conn):
        started = time.monotonic()
        out = _rows(conn, "SELECT CURRENT_USER AS USER_NAME, CURRENT_SCHEMA AS SCHEMA_NAME, VERSION, "
                          "CURRENT_UTCTIMESTAMP AS SERVER_TIME_UTC FROM SYS.M_DATABASE")
        out["round_trip_ms"] = round((time.monotonic() - started) * 1000)
        out["server"] = f"{_PRODUCT} {VERSION} by {BRAND}"
        user = (out["rows"][0][0] if out["rows"] else "") or ""
        if user.upper() in ("SYSTEM", "DBADMIN", "SYS"):
            out["warning"] = (f"Connected as {user}, an administrator. {_PRODUCT} only reads, but for safety use a "
                              f"read-only user such as MCP_READER. Change it in {_WHERE}.")
        return out
    return _hana.run(work)


@_tool("Diagnose connection")
def diagnose_connection() -> dict:
    """Check the connection step by step and say which step fails and how to fix it: settings, host name (DNS),
    network port, TLS certificate, login, schema and privileges. Use when test_connection fails."""
    steps: list[list[str]] = []

    def add(step, status, detail):
        steps.append([step, status, detail])

    def done(next_step: str) -> dict:
        return _table(["STEP", "RESULT", "DETAIL"], steps, next_step=next_step,
                      server=f"{_PRODUCT} {VERSION} by {BRAND}")

    missing = _missing_settings()
    if missing:
        add("Settings", "FAIL", f"Not entered: {', '.join(missing)}.")
        return done(f"Open {_WHERE}, fill in the missing settings and save. Then {_RESTART}.")
    host = os.environ["HANA_HOST"]
    try:
        port = _port()
    except ToolError as e:
        add("Settings", "FAIL", str(e))
        return done(f"Correct the port in {_WHERE}.")
    encrypt, validate = _flag("HANA_ENCRYPT"), _flag("HANA_VALIDATE_CERT")
    add("Settings", "OK", f"host {host}, port {port}, user {os.environ['HANA_USER']}, TLS {'on' if encrypt else 'off'}"
                          f", certificate check {'on' if validate else 'off'}, schema "
                          f"{DEFAULT_SCHEMA or '(login default)'}, query timeout {QUERY_TIMEOUT_S}s")
    if "hanacloud" in host.lower() and port != 443:
        add("Settings", "WARN", f"This looks like HANA Cloud, which listens on port 443, not {port}.")
    if "hanacloud" in host.lower() and not encrypt:
        add("Settings", "WARN", "HANA Cloud requires TLS: turn 'Use TLS encryption' on.")

    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        add("Host name (DNS)", "OK", f"{host} resolves to {', '.join(addrs[:3])}")
    except OSError as e:
        add("Host name (DNS)", "FAIL", f"{host} cannot be resolved ({e}).")
        return done(f"Check the host spelling in {_WHERE}: copy the SQL endpoint from SAP HANA Cloud Central without "
                    "https://. Check the internet connection or VPN.")

    try:
        with socket.create_connection((host, port), timeout=6):
            add("Network port", "OK", f"Port {port} accepts connections.")
    except OSError as e:
        text = str(e).lower()
        why = ("Nothing is listening: the database is stopped or the port is wrong." if "refused" in text
               else "No answer: a firewall, VPN, the HANA Cloud allowed-IP list, or a stopped instance.")
        add("Network port", "FAIL", f"{why} ({e})")
        return done("Start the database (HANA Cloud Central > Start for HANA Cloud) and check the port: HANA Cloud 443, "
                    "HANA Express 39041. For HANA Cloud also add this computer's IP to Allowed connections.")

    if encrypt:
        ctx = ssl.create_default_context()
        try:
            with socket.create_connection((host, port), timeout=6) as raw, \
                    ctx.wrap_socket(raw, server_hostname=host) as tls:
                add("TLS certificate", "OK", f"Trusted certificate, {tls.version()}.")
        except ssl.SSLCertVerificationError as e:
            add("TLS certificate", "OK" if not validate else "WARN",
                f"The certificate is not trusted by this computer ({e.verify_message}). "
                + ("The certificate check is off, so this is accepted." if not validate else
                   "If this is HANA Express or a self-signed system, turn off 'Validate TLS certificate'."))
        except (ssl.SSLError, OSError) as e:
            add("TLS certificate", "INFO", f"No standard TLS handshake on this port ({type(e).__name__}). If the login "
                                           "below fails, try switching 'Use TLS encryption'.")

    try:
        _hana.drop()
        info = _hana.run(lambda conn: _fetch(conn, "SELECT CURRENT_USER, CURRENT_SCHEMA, VERSION FROM SYS.M_DATABASE",
                                             limit=1), bypass_cooldown=True)
        user, schema, version = info[0]
        add("Login", "OK", f"Logged in as {user}. SAP HANA {version}.")
    except ToolError as e:
        add("Login", "FAIL", str(e))
        return done("Fix what the Login step names, save in Configure, then " + _RESTART + ".")

    def checks(conn):
        notes = []
        if DEFAULT_SCHEMA:
            try:
                _resolve_schema(conn, DEFAULT_SCHEMA)
                add("Default schema", "OK", f"Schema {DEFAULT_SCHEMA} exists.")
            except ToolError as e:
                add("Default schema", "FAIL", str(e))
                notes.append(f"Correct or clear the default schema in {_WHERE}.")
        else:
            add("Default schema", "OK", f"Not set; using the login's schema {schema}.")
        try:
            readable = _fetch(conn, "SELECT COUNT(DISTINCT SCHEMA_NAME) FROM SYS.EFFECTIVE_PRIVILEGES WHERE USER_NAME "
                                    "= CURRENT_USER AND PRIVILEGE = 'SELECT' AND OBJECT_TYPE IN ('SCHEMA', 'TABLE', "
                                    "'VIEW')", limit=1)[0][0]
            add("Read access", "OK" if readable else "WARN",
                f"SELECT on objects in {readable} schema(s)." if readable else
                "No SELECT privileges found. Ask the administrator: GRANT SELECT ON SCHEMA <schema> TO " + user + ".")
            writes = _fetch(conn, "SELECT DISTINCT PRIVILEGE FROM SYS.EFFECTIVE_PRIVILEGES WHERE USER_NAME = "
                                  "CURRENT_USER AND PRIVILEGE IN ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', "
                                  "'CREATE ANY', 'DATA ADMIN', 'USER ADMIN', 'ROLE ADMIN') ORDER BY PRIVILEGE", limit=20)
            if writes or str(user).upper() in ("SYSTEM", "DBADMIN"):
                add("Least privilege", "WARN",
                    "This login can change data or the database (" + (", ".join(w[0] for w in writes) or user) +
                    f"). {_PRODUCT} never writes, but use a read-only user (SELECT only) for safety.")
            else:
                add("Least privilege", "OK", "The login cannot change data. Good.")
        except Exception as e:  # monitoring views may be hidden from small users; not a failure
            add("Privileges", "INFO", f"Could not read SYS.EFFECTIVE_PRIVILEGES ({_err_text(e)[:120]}).")
        return notes

    notes = _hana.run(checks)
    if any(s[1] == "FAIL" for s in steps):
        return done(" ".join(notes) or "Fix the step marked FAIL.")
    return done("Everything works. Ask a question about your data.")


@_tool("Session info")
def get_session_info() -> dict:
    """Show this database session: user, schema, connection id, status and client, plus connection health."""
    out = _query("SELECT USER_NAME, CURRENT_SCHEMA AS SCHEMA_NAME, CONNECTION_ID, CONNECTION_STATUS, START_TIME, "
                 "CLIENT_HOST FROM SYS.M_CONNECTIONS WHERE CONNECTION_ID = CURRENT_CONNECTION")
    h = _hana.health
    out["health"] = {"connections_opened": h.connects, "automatic_reconnects": h.reconnects,
                     "queries_retried_after_reconnect": h.retried_queries,
                     "server_uptime_s": round(time.time() - h.started)}
    return out


@_tool("Show settings")
def show_config() -> dict:
    """Show the settings this server uses (host, port, user, schema, TLS, limits). The password is never shown."""
    missing = _missing_settings()
    rows = [["PRODUCT", f"{_PRODUCT} {VERSION} by {BRAND} ({WEBSITE})"],
            ["SETUP", "complete" if not missing else f"not entered yet: {', '.join(missing)}. Change it in {_WHERE}"],
            ["HANA_HOST", os.environ.get("HANA_HOST", "")], ["HANA_PORT", os.environ.get("HANA_PORT", "443")],
            ["HANA_USER", os.environ.get("HANA_USER", "")],
            ["HANA_SCHEMA", DEFAULT_SCHEMA or "(login's default schema)"],
            ["HANA_ENCRYPT", str(_flag("HANA_ENCRYPT")).lower()],
            ["HANA_VALIDATE_CERT", str(_flag("HANA_VALIDATE_CERT")).lower()],
            ["query timeout (seconds)", str(QUERY_TIMEOUT_S)], ["max rows per result", str(MAX_ROWS)],
            ["max characters per value", str(MAX_CELL_CHARS)], ["support", SUPPORT]]
    return _table(["SETTING", "VALUE"], rows)


@_tool("Show environment")
def show_env_vars() -> dict:
    """List which HANA_* settings are set. Values are shown except the password, which is hidden."""
    rows = [[k, ("set (hidden)" if os.environ.get(k) else "not set") if k == "HANA_PASSWORD"
             else (os.environ.get(k) or "not set")] for k in _HANA_KEYS]
    return _table(["VARIABLE", "VALUE"], rows)


# ---------------------------------------------------------------------------------------------- find
@_tool("List schemas")
def list_schemas() -> dict:
    """List schemas visible to the connected user (system schemas excluded)."""
    return _query("SELECT SCHEMA_NAME, SCHEMA_OWNER FROM SYS.SCHEMAS "
                  "WHERE SCHEMA_NAME NOT LIKE '\\_SYS%' ESCAPE '\\' AND SCHEMA_NAME NOT IN ('SYS', 'SYSTEM') "
                  "ORDER BY SCHEMA_NAME", limit=5000)


@_tool("List tables")
def list_tables(schema: str = "") -> dict:
    """List tables in a schema with their row counts. Defaults to the working schema (see test_connection)."""
    return _list("SYS.M_TABLES WHERE 1 = 1", schema, "TABLE_NAME, TABLE_TYPE, RECORD_COUNT", "TABLE_NAME",
                 empty="Schema {schema} has no tables this user can see. Try list_views, or another schema "
                       "(list_schemas).")


@_tool("List views")
def list_views(schema: str = "") -> dict:
    """List views in a schema (SQL views and calculation views)."""
    return _list("SYS.VIEWS WHERE 1 = 1", schema, "VIEW_NAME, VIEW_TYPE, IS_VALID", "VIEW_NAME",
                 empty="Schema {schema} has no views this user can see.")


@_tool("List calculation views")
def list_calculation_views(schema: str = "") -> dict:
    """List calculation views in a schema."""
    return _list("SYS.VIEWS WHERE VIEW_TYPE = 'CALC'", schema, "VIEW_NAME, VIEW_TYPE, IS_VALID", "VIEW_NAME",
                 empty="Schema {schema} has no calculation views. Calculation views of HDI projects live in their "
                       "container schema: see list_schemas.")


@_tool("List procedures")
def list_procedures(schema: str = "") -> dict:
    """List stored procedures in a schema."""
    return _list("SYS.PROCEDURES WHERE 1 = 1", schema, "PROCEDURE_NAME, PROCEDURE_TYPE, IS_VALID", "PROCEDURE_NAME",
                 empty="Schema {schema} has no procedures this user can see.")


@_tool("List functions")
def list_functions(schema: str = "") -> dict:
    """List user-defined functions in a schema."""
    return _list("SYS.FUNCTIONS WHERE 1 = 1", schema, "FUNCTION_NAME, FUNCTION_TYPE, IS_VALID", "FUNCTION_NAME",
                 empty="Schema {schema} has no user-defined functions.")


@_tool("List sequences")
def list_sequences(schema: str = "") -> dict:
    """List sequences in a schema."""
    return _list("SYS.SEQUENCES WHERE 1 = 1", schema,
                 "SEQUENCE_NAME, START_NUMBER, MIN_VALUE, MAX_VALUE, INCREMENT_BY, IS_CYCLED", "SEQUENCE_NAME",
                 empty="Schema {schema} has no sequences.")


@_tool("List synonyms")
def list_synonyms(schema: str = "") -> dict:
    """List synonyms in a schema and the objects they point to."""
    return _list("SYS.SYNONYMS WHERE 1 = 1", schema, "SYNONYM_NAME, OBJECT_SCHEMA, OBJECT_NAME, OBJECT_TYPE",
                 "SYNONYM_NAME", empty="Schema {schema} has no synonyms.")


@_tool("Search tables")
def search_tables(pattern: str, schema: str = "") -> dict:
    """Find tables and views whose name contains the text (case-insensitive), e.g. 'order'. Searches the working
    schema, or the schema you name; if nothing matches there, other schemas are searched too."""
    like = _like(pattern)

    def work(conn):
        sch = _schema_or_current(conn, schema)
        out = _rows(conn, "SELECT SCHEMA_NAME, OBJECT_NAME AS TABLE_NAME, OBJECT_TYPE AS TABLE_TYPE FROM SYS.OBJECTS "
                          "WHERE OBJECT_TYPE IN ('TABLE', 'VIEW', 'SYNONYM') AND SCHEMA_NAME = COALESCE(?, "
                          "CURRENT_SCHEMA) AND UPPER(OBJECT_NAME) LIKE ? ESCAPE '\\' ORDER BY OBJECT_NAME",
                    (sch, like), 500)
        if not out["rows"]:
            out = _rows(conn, "SELECT SCHEMA_NAME, OBJECT_NAME AS TABLE_NAME, OBJECT_TYPE AS TABLE_TYPE FROM "
                              "SYS.OBJECTS WHERE OBJECT_TYPE IN ('TABLE', 'VIEW') AND UPPER(OBJECT_NAME) LIKE ? "
                              "ESCAPE '\\' AND SCHEMA_NAME NOT LIKE '\\_SYS%' ESCAPE '\\' "
                              "ORDER BY SCHEMA_NAME, OBJECT_NAME", (like,), 200)
            _with_notes(out, (f"Nothing matched in schema {sch or _current_schema(conn)}; these are from other "
                              "schemas.") if out["rows"] else
                        f"No table or view name contains '{pattern.strip()}' in any schema this user can read. "
                        "Try a shorter or different word, or search_columns.")
        return out
    return _hana.run(work)


@_tool("Search columns")
def search_columns(pattern: str, schema: str = "") -> dict:
    """Find columns whose name contains the text (case-insensitive), e.g. 'plant', across all tables of a schema."""
    like = _like(pattern)

    def work(conn):
        sch = _schema_or_current(conn, schema)
        out = _rows(conn, "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE_NAME, LENGTH FROM SYS.TABLE_COLUMNS WHERE "
                          "SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND UPPER(COLUMN_NAME) LIKE ? ESCAPE '\\' "
                          "ORDER BY TABLE_NAME, POSITION", (sch, like), 500)
        if not out["rows"]:
            out["note"] = (f"No column name contains '{pattern.strip()}' in schema {sch or _current_schema(conn)}. "
                           "Try a shorter word, a synonym (e.g. CUST for customer), or another schema.")
        return out
    return _hana.run(work)


# ---------------------------------------------------------------------------------------------- describe
def _describe_columns(conn, obj: _Obj, with_comments: bool = False) -> dict:
    view = obj.type == "VIEW"
    source = "SYS.VIEW_COLUMNS" if view else "SYS.TABLE_COLUMNS"
    key = "VIEW_NAME" if view else "TABLE_NAME"
    cols = "COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE" + \
           (", COMMENTS" if with_comments else ", DEFAULT_VALUE" if not view else "")
    out = _rows(conn, f"SELECT {cols} FROM {source} WHERE SCHEMA_NAME = ? AND {key} = ? ORDER BY POSITION",
                (obj.schema, obj.name), MAX_LIST_ROWS)
    out["object"] = obj.label
    out["object_type"] = obj.type
    return _with_notes(out, obj.note)


@_tool("Describe table")
def describe_table(table: str, schema: str = "") -> dict:
    """Show the columns of a table (or view or synonym): name, data type, length, nullability, default.
    Accepts TABLE or SCHEMA.TABLE, in any upper/lower case."""
    return _hana.run(lambda conn: _describe_columns(conn, _resolve(conn, table, schema)))


@_tool("Explain table")
def explain_table(table: str, schema: str = "") -> dict:
    """Explain a table in business terms: columns with their comments, the primary key and the row count."""
    def work(conn):
        obj = _resolve(conn, table, schema)
        if obj.type == "VIEW":
            out = _describe_columns(conn, obj, with_comments=True)
            return _with_notes(out, f"{obj.label} is a view: it has no primary key or row count of its own.")
        cols = _rows(conn,
                     "SELECT C.COLUMN_NAME, C.DATA_TYPE_NAME, C.LENGTH, C.SCALE, C.IS_NULLABLE, C.COMMENTS, "
                     "CASE WHEN K.COLUMN_NAME IS NULL THEN '' ELSE 'PRIMARY KEY' END AS KEY_PART "
                     "FROM SYS.TABLE_COLUMNS C LEFT JOIN SYS.CONSTRAINTS K ON K.SCHEMA_NAME = C.SCHEMA_NAME "
                     "AND K.TABLE_NAME = C.TABLE_NAME AND K.COLUMN_NAME = C.COLUMN_NAME AND K.IS_PRIMARY_KEY = 'TRUE' "
                     "WHERE C.SCHEMA_NAME = ? AND C.TABLE_NAME = ? ORDER BY C.POSITION",
                     (obj.schema, obj.name), MAX_LIST_ROWS)
        info = _rows(conn, "SELECT T.TABLE_NAME, T.TABLE_TYPE, M.RECORD_COUNT, M.TABLE_SIZE, T.COMMENTS "
                           "FROM SYS.TABLES T LEFT JOIN SYS.M_TABLES M ON M.SCHEMA_NAME = T.SCHEMA_NAME AND "
                           "M.TABLE_NAME = T.TABLE_NAME WHERE T.SCHEMA_NAME = ? AND T.TABLE_NAME = ?",
                     (obj.schema, obj.name))
        cols["table"] = dict(zip(info["columns"], info["rows"][0])) if info["rows"] else {}
        cols["object"] = obj.label
        return _with_notes(cols, obj.note)
    return _hana.run(work)


@_tool("Describe view")
def describe_view(view: str, schema: str = "") -> dict:
    """Show the columns of a view (also works for tables and synonyms)."""
    return _hana.run(lambda conn: _describe_columns(conn, _resolve(conn, view, schema, what="view")))


def _describe_params(kind: str, value: str, schema: str) -> dict:
    def work(conn):
        obj = _resolve(conn, value, schema, (kind, "SYNONYM"), kind.lower())
        if obj.type != kind:
            raise ToolError(f"{obj.label} is a {obj.type.lower()}, not a {kind.lower()}.")
        out = _rows(conn, f"SELECT PARAMETER_NAME, PARAMETER_TYPE, DATA_TYPE_NAME, LENGTH, SCALE, POSITION FROM "
                          f"SYS.{kind}_PARAMETERS WHERE SCHEMA_NAME = ? AND {kind}_NAME = ? ORDER BY POSITION",
                    (obj.schema, obj.name), MAX_LIST_ROWS)
        out["object"] = obj.label
        if not out["rows"]:
            out["note"] = f"{obj.label} takes no parameters."
        return _with_notes(out, obj.note)
    return _hana.run(work)


@_tool("Describe procedure")
def describe_procedure(procedure: str, schema: str = "") -> dict:
    """Show the parameters of a stored procedure (direction, type, position). It is described, never run."""
    return _describe_params("PROCEDURE", procedure, schema)


@_tool("Describe function")
def describe_function(function: str, schema: str = "") -> dict:
    """Show the parameters of a user-defined function (direction, type, position)."""
    return _describe_params("FUNCTION", function, schema)


@_tool("Describe index")
def describe_index(index: str, schema: str = "") -> dict:
    """Show an index: its table, type and the columns in order."""
    def work(conn):
        sch, name = _qualified(index, schema, "index")
        sch = _resolve_schema(conn, sch)
        sql = ("SELECT I.SCHEMA_NAME, I.INDEX_NAME, I.TABLE_NAME, I.INDEX_TYPE, I.CONSTRAINT, C.COLUMN_NAME, "
               "C.POSITION FROM SYS.INDEXES I JOIN SYS.INDEX_COLUMNS C ON C.SCHEMA_NAME = I.SCHEMA_NAME AND "
               "C.TABLE_NAME = I.TABLE_NAME AND C.INDEX_NAME = I.INDEX_NAME WHERE I.SCHEMA_NAME = COALESCE(?, "
               "CURRENT_SCHEMA) AND {cond} ORDER BY C.POSITION")
        out = _rows(conn, sql.format(cond="I.INDEX_NAME = ?"), (sch, name), MAX_LIST_ROWS)
        if not out["rows"]:
            out = _rows(conn, sql.format(cond="UPPER(I.INDEX_NAME) = UPPER(?)"), (sch, name), MAX_LIST_ROWS)
        if not out["rows"]:
            names = [r[0] for r in _fetch(conn, "SELECT INDEX_NAME FROM SYS.INDEXES WHERE SCHEMA_NAME = COALESCE(?, "
                                                "CURRENT_SCHEMA)", (sch,), 20000, timeout=SUGGEST_TIMEOUT_S)]
            close = _closest(name, names)
            raise ToolError(f"No index named {name} in schema {sch or _current_schema(conn)}."
                            + (f" Did you mean: {', '.join(close)}?" if close else
                               " Call list_indexes with the table name to see its indexes."))
        return out
    return _hana.run(work)


_TYPE_WITH_LENGTH = {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "VARBINARY", "BINARY", "ALPHANUM", "SHORTTEXT"}
_TEXT_TYPES = {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "ALPHANUM", "SHORTTEXT"}


def _column_ddl(name, dtype, length, scale, nullable, default) -> str:
    base = dtype
    if dtype in _TYPE_WITH_LENGTH and length:
        dtype = f"{dtype}({length})"
    elif dtype == "DECIMAL" and length:
        dtype = f"DECIMAL({length}, {scale or 0})"
    null = "" if str(nullable).upper() == "TRUE" else " NOT NULL"
    if default in (None, ""):
        dflt = ""
    elif base in _TEXT_TYPES and not str(default).startswith("'"):
        dflt = " DEFAULT '" + str(default).replace("'", "''") + "'"
    else:
        dflt = f" DEFAULT {default}"
    return f"{_quote(name)} {dtype}{dflt}{null}"


@_tool("Get DDL")
def get_ddl(object_name: str, schema: str = "", object_type: str = "") -> dict:
    """Get the CREATE statement of a table, view, procedure, function or synonym. object_type (TABLE, VIEW,
    PROCEDURE, FUNCTION, SYNONYM) is optional: the server finds it. For tables, the statement is rebuilt from the
    catalog (columns and primary key)."""
    wanted = (object_type or "").strip().upper()
    allowed = ("TABLE", "VIEW", "PROCEDURE", "FUNCTION", "SYNONYM")
    if wanted and wanted not in allowed:
        raise ToolError(f"object_type must be one of {', '.join(allowed)} (or empty to detect it).")
    types = (wanted,) if wanted else allowed

    def work(conn):
        obj = _resolve(conn, object_name, schema, types, (wanted or "object").lower(), follow_synonyms=False)
        if obj.type == "SYNONYM":
            t = _fetch(conn, "SELECT OBJECT_SCHEMA, OBJECT_NAME FROM SYS.SYNONYMS WHERE SCHEMA_NAME = ? AND "
                             "SYNONYM_NAME = ?", (obj.schema, obj.name), 1)
            target = f"{_quote(t[0][0])}.{_quote(t[0][1])}" if t else "?"
            return {"object": obj.label, "object_type": "SYNONYM", "ddl": f"CREATE SYNONYM {obj.ref} FOR {target}"}
        if obj.type in ("VIEW", "PROCEDURE", "FUNCTION"):
            found = _fetch(conn, f"SELECT DEFINITION FROM SYS.{obj.type}S WHERE SCHEMA_NAME = ? AND {obj.type}_NAME = ?",
                           (obj.schema, obj.name), 1)
            ddl = _cell(found[0][0], MAX_DDL_CHARS) if found else None
            if not ddl:
                raise ToolError(f"{obj.label} exists, but HANA does not show its definition to this user (it may be "
                                "encrypted, or need the owner's privileges).")
            if obj.type == "VIEW" and not ddl.lstrip().upper().startswith("CREATE"):
                ddl = f"CREATE VIEW {obj.ref} AS {ddl}"
            return {"object": obj.label, "object_type": obj.type, "ddl": ddl}
        cols = _fetch(conn, "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE, DEFAULT_VALUE FROM "
                            "SYS.TABLE_COLUMNS WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? ORDER BY POSITION",
                      (obj.schema, obj.name), 10000)
        kind = _fetch(conn, "SELECT TABLE_TYPE FROM SYS.TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
                      (obj.schema, obj.name), 1)
        pk = _fetch(conn, "SELECT COLUMN_NAME FROM SYS.CONSTRAINTS WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? AND "
                          "IS_PRIMARY_KEY = 'TRUE' ORDER BY POSITION", (obj.schema, obj.name), 1000)
        lines = [_column_ddl(*r) for r in cols]
        if pk:
            lines.append("PRIMARY KEY (" + ", ".join(_quote(r[0]) for r in pk) + ")")
        ttype = (kind[0][0] if kind else "COLUMN") or "COLUMN"
        ddl = f"CREATE {ttype} TABLE {obj.ref} (\n  " + ",\n  ".join(lines) + "\n)"
        return {"object": obj.label, "object_type": "TABLE", "ddl": ddl,
                "note": "Rebuilt from the catalog: columns and primary key. Indexes, partitions and other options "
                        "are not included (see list_indexes, get_partition_info)."}
    return _hana.run(work)


# ---------------------------------------------------------------------------------------------- structure
def _table_listing(table: str, schema: str, sql: str, empty: str) -> dict:
    def work(conn):
        obj = _resolve(conn, table, schema)
        out = _rows(conn, sql, (obj.schema, obj.name), MAX_LIST_ROWS)
        out["object"] = obj.label
        if not out["rows"]:
            out["note"] = empty.format(obj=obj.label, kind=obj.type.lower())
        return _with_notes(out, obj.note)
    return _hana.run(work)


@_tool("List indexes")
def list_indexes(table: str, schema: str = "") -> dict:
    """List the indexes of a table."""
    return _table_listing(table, schema, "SELECT INDEX_NAME, INDEX_TYPE, CONSTRAINT FROM SYS.INDEXES WHERE "
                                         "SCHEMA_NAME = ? AND TABLE_NAME = ? ORDER BY INDEX_NAME",
                          "{obj} ({kind}) has no indexes.")


@_tool("List constraints")
def list_constraints(table: str, schema: str = "") -> dict:
    """List the primary-key and unique constraints of a table."""
    return _table_listing(table, schema, "SELECT CONSTRAINT_NAME, COLUMN_NAME, POSITION, IS_PRIMARY_KEY, "
                                         "IS_UNIQUE_KEY FROM SYS.CONSTRAINTS WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? "
                                         "ORDER BY CONSTRAINT_NAME, POSITION",
                          "{obj} ({kind}) has no primary-key or unique constraints.")


@_tool("List foreign keys")
def list_foreign_keys(table: str, schema: str = "") -> dict:
    """List the foreign keys of a table and the tables they point to."""
    return _table_listing(table, schema, "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_SCHEMA_NAME, "
                                         "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, DELETE_RULE, UPDATE_RULE "
                                         "FROM SYS.REFERENTIAL_CONSTRAINTS WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? "
                                         "ORDER BY CONSTRAINT_NAME, POSITION",
                          "{obj} has no foreign keys. (Many SAP systems, such as S/4HANA, keep relationships in the "
                          "application, not as foreign keys: use search_columns on the key column name instead.)")


@_tool("Get dependencies")
def get_dependencies(object_name: str, schema: str = "") -> dict:
    """Show what depends on an object (views, procedures that use it) and what it depends on."""
    def work(conn):
        obj = _resolve(conn, object_name, schema, ("TABLE", "VIEW", "SYNONYM", "PROCEDURE", "FUNCTION", "SEQUENCE"),
                       "object", follow_synonyms=False)
        out = _rows(conn,
                    "SELECT 'USES' AS DIRECTION, BASE_SCHEMA_NAME AS SCHEMA_NAME, BASE_OBJECT_NAME AS OBJECT_NAME, "
                    "BASE_OBJECT_TYPE AS OBJECT_TYPE FROM SYS.OBJECT_DEPENDENCIES "
                    "WHERE DEPENDENT_SCHEMA_NAME = ? AND DEPENDENT_OBJECT_NAME = ? "
                    "UNION ALL SELECT 'USED BY', DEPENDENT_SCHEMA_NAME, DEPENDENT_OBJECT_NAME, DEPENDENT_OBJECT_TYPE "
                    "FROM SYS.OBJECT_DEPENDENCIES WHERE BASE_SCHEMA_NAME = ? AND BASE_OBJECT_NAME = ?",
                    (obj.schema, obj.name, obj.schema, obj.name), MAX_LIST_ROWS)
        out["object"] = obj.label
        if not out["rows"]:
            out["note"] = f"Nothing depends on {obj.label}, and it depends on nothing this user can see."
        return out
    return _hana.run(work)


@_tool("List privileges")
def list_privileges(schema: str = "") -> dict:
    """List what the connected user may do: privileges on a schema (default: the working schema) and system
    privileges."""
    def work(conn):
        sch = _schema_or_current(conn, schema)
        return _rows(conn, "SELECT DISTINCT PRIVILEGE, OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME FROM "
                           "SYS.EFFECTIVE_PRIVILEGES WHERE USER_NAME = CURRENT_USER AND (SCHEMA_NAME = COALESCE(?, "
                           "CURRENT_SCHEMA) OR OBJECT_TYPE = 'SYSTEMPRIVILEGE') ORDER BY OBJECT_TYPE, PRIVILEGE",
                     (sch,), MAX_LIST_ROWS)
    return _hana.run(work)


# ---------------------------------------------------------------------------------------------- data
@_tool("Run query")
def run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a single read-only SQL query (SELECT or WITH ... SELECT). Writes are rejected.

    Results are capped at max_rows (hard limit 200). For more rows use query_next_page.
    """
    q = _read_only_sql(sql)
    n = _cap(max_rows)

    def work(conn):
        out = _rows(conn, q.text, limit=n)
        if out["truncated"] and "note" not in out:
            out["note"] = (f"Only the first {n} rows are shown. Aggregate in SQL, or get the next rows with "
                           f"query_next_page(sql, offset={n})" + ("" if q.top_has("ORDER") else
                                                                  " after adding an ORDER BY") + ".")
            out["next_offset"] = n
        return out
    return _hana.run(work, q.text)


@_tool("Next page of a query")
def query_next_page(sql: str, offset: int, max_rows: int = 100) -> dict:
    """Get the next page of a query: pass the same SELECT again with the offset to skip (0, 100, 200 ...).
    Include an ORDER BY so the pages are stable."""
    q = _read_only_sql(sql)
    n = _cap(max_rows)
    try:
        skip = max(0, int(offset))
    except (TypeError, ValueError):
        raise ToolError("offset must be a whole number of rows to skip (0, 100, 200 ...).") from None
    notes = [] if q.top_has("ORDER") else ["The query has no ORDER BY, so rows may move between pages. Add one."]
    has_limit = q.top_has("LIMIT") or q.top_has("TOP") or q.top_has("OFFSET")

    def work(conn):
        if not has_limit:  # page in SQL: works for SELECT and WITH alike
            out = _rows(conn, f"{q.text}\nLIMIT {n + 1} OFFSET {skip}", limit=n)
        elif q.first == "SELECT":
            out = _rows(conn, f"SELECT * FROM (\n{q.text}\n) LIMIT {n + 1} OFFSET {skip}", limit=n)
        else:  # WITH ... that has its own LIMIT / TOP: skip rows here
            if skip > MAX_CLIENT_SKIP:
                raise ToolError(f"This query cannot be paged in SQL, and skipping {skip:,} rows is too many. Remove its "
                                "own TOP / LIMIT, or narrow it with WHERE.")
            cur = _execute(conn, q.text)
            try:
                left = skip
                while left > 0:
                    got = cur.fetchmany(min(left, 1000))
                    if not got:
                        break
                    left -= len(got)
                out = _result(cur, n)
            finally:
                _close(cur)
        out["offset"] = skip
        if out["truncated"] and "note" not in out:
            out["next_offset"] = skip + out["row_count"]
        elif not out["rows"]:
            notes.append(f"No rows after offset {skip}: this is past the end of the result.")
        return _with_notes(out, *notes)
    return _hana.run(work, q.text)


@_tool("Sample data")
def get_sample_data(table: str, schema: str = "", rows: int = 10) -> dict:
    """Show a few example rows of a table or view (default 10, at most 50)."""
    n = _cap(rows, 50)

    def work(conn):
        obj = _resolve(conn, table, schema)
        out = _rows(conn, f"SELECT TOP {n} * FROM {obj.ref}", limit=n)
        out["object"] = obj.label
        if not out["rows"]:
            out["note"] = f"{obj.label} is empty (no rows)."
        return _with_notes(out, obj.note)
    return _hana.run(work)


_NO_MINMAX = {"BLOB", "CLOB", "NCLOB", "TEXT", "BINTEXT", "ST_GEOMETRY", "ST_POINT", "REAL_VECTOR", "HALF_VECTOR"}


@_tool("Column statistics")
def get_column_stats(table: str, column: str, schema: str = "") -> dict:
    """Profile one column: row count, filled values, distinct values, smallest and largest value."""
    def work(conn):
        obj = _resolve(conn, table, schema)
        name, dtype = _resolve_column(conn, obj, column)
        col = _quote(name)
        if (dtype or "").upper() in _NO_MINMAX:
            out = _rows(conn, f"SELECT COUNT(*) AS ROW_COUNT, COUNT({col}) AS FILLED, COUNT(*) - COUNT({col}) AS EMPTY "
                              f"FROM {obj.ref}")
            out["note"] = f"{name} is {dtype}: distinct, smallest and largest values cannot be computed for this type."
        else:
            out = _rows(conn, f"SELECT COUNT(*) AS ROW_COUNT, COUNT({col}) AS FILLED, COUNT(*) - COUNT({col}) AS EMPTY, "
                              f"COUNT(DISTINCT {col}) AS DISTINCT_VALUES, MIN({col}) AS MIN_VALUE, "
                              f"MAX({col}) AS MAX_VALUE FROM {obj.ref}")
        out["object"], out["column"], out["data_type"] = obj.label, name, dtype
        return _with_notes(out, obj.note)
    return _hana.run(work)


# ---------------------------------------------------------------------------------------------- performance
@_tool("Table statistics")
def get_table_stats(table: str, schema: str = "") -> dict:
    """Show a table's size: rows, memory, disk and delta-store details."""
    def work(conn):
        obj = _resolve(conn, table, schema)
        if obj.type == "VIEW":
            raise ToolError(f"{obj.label} is a view: it stores no data, so it has no size. Ask for the tables it "
                            "reads (get_dependencies).")
        out = _rows(conn, "SELECT T.TABLE_NAME, T.RECORD_COUNT, T.TABLE_SIZE, C.MEMORY_SIZE_IN_TOTAL, "
                          "C.MEMORY_SIZE_IN_MAIN, C.MEMORY_SIZE_IN_DELTA, C.RAW_RECORD_COUNT_IN_DELTA, C.LAST_MERGE_TIME "
                          "FROM SYS.M_TABLES T LEFT JOIN (SELECT SCHEMA_NAME, TABLE_NAME, SUM(MEMORY_SIZE_IN_TOTAL) AS "
                          "MEMORY_SIZE_IN_TOTAL, SUM(MEMORY_SIZE_IN_MAIN) AS MEMORY_SIZE_IN_MAIN, "
                          "SUM(MEMORY_SIZE_IN_DELTA) AS MEMORY_SIZE_IN_DELTA, SUM(RAW_RECORD_COUNT_IN_DELTA) AS "
                          "RAW_RECORD_COUNT_IN_DELTA, MAX(LAST_MERGE_TIME) AS LAST_MERGE_TIME FROM SYS.M_CS_TABLES "
                          "WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? GROUP BY SCHEMA_NAME, TABLE_NAME) C "
                          "ON C.SCHEMA_NAME = T.SCHEMA_NAME AND C.TABLE_NAME = T.TABLE_NAME "
                          "WHERE T.SCHEMA_NAME = ? AND T.TABLE_NAME = ?",
                    (obj.schema, obj.name, obj.schema, obj.name))
        out["object"] = obj.label
        if not out["rows"]:
            out["note"] = f"No runtime statistics for {obj.label} (it may not be loaded yet, or is a virtual table)."
        return _with_notes(out, "Sizes are in bytes.", obj.note)
    return _hana.run(work)


@_tool("Partition info")
def get_partition_info(table: str, schema: str = "") -> dict:
    """Show how a table is partitioned: each partition with its rows and memory. One row means not partitioned."""
    return _table_listing(table, schema, "SELECT PART_ID, RECORD_COUNT, MEMORY_SIZE_IN_TOTAL FROM SYS.M_CS_TABLES "
                                         "WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? ORDER BY PART_ID",
                          "No column-store partitions found for {obj}: it is a row table, a view, or not loaded in "
                          "memory yet.")


@_tool("Explain plan")
def explain_plan(sql: str) -> dict:
    """Show how HANA would run a SELECT, without running it: the execution plan, operator by operator."""
    q = _read_only_sql(sql)
    statement = "MCP_" + uuid.uuid4().hex[:12]  # the plan lives in HANA's session-local plan table

    def work(conn):
        # The plan rows live in a session-local table and disappear when the session ends.
        _close(_execute(conn, f"EXPLAIN PLAN SET STATEMENT_NAME = '{statement}' FOR {q.text}"))
        return _rows(conn, "SELECT OPERATOR_ID, PARENT_OPERATOR_ID, OPERATOR_NAME, OPERATOR_DETAILS, "
                           "EXECUTION_ENGINE, SCHEMA_NAME, TABLE_NAME, OUTPUT_SIZE, SUBTREE_COST FROM "
                           "SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = ? ORDER BY OPERATOR_ID", (statement,))
    return _hana.run(work, q.text)


@_tool("Expensive queries")
def get_expensive_queries(limit: int = 10) -> dict:
    """Show the slowest recent statements (needs HANA's expensive-statement trace and monitoring rights)."""
    n = _cap(limit, 50)

    def work(conn):
        out = _rows(conn, f"SELECT TOP {n} START_TIME, DURATION_MICROSEC, DB_USER, SCHEMA_NAME, MEMORY_SIZE, CPU_TIME, "
                          "STATEMENT_STRING FROM SYS.M_EXPENSIVE_STATEMENTS ORDER BY DURATION_MICROSEC DESC", limit=n)
        if not out["rows"]:
            out["note"] = ("No expensive statements recorded. The expensive-statement trace is probably off, or this "
                           "user lacks the MONITORING role. An administrator can turn it on in SAP HANA Cockpit "
                           "(Monitoring > Expensive Statements).")
        return out
    return _hana.run(work)


if __name__ == "__main__":
    log.info("%s %s by %s starting (read-only SAP HANA analyst, %s)", _PRODUCT, VERSION, BRAND, WEBSITE)
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass
    finally:
        _hana.drop()
