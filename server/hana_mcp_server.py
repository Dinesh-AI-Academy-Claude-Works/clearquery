"""SAP HANA Analyst (sap-hana-analyst) by Dinesh AI Academy: a read-only SAP HANA MCP server, written with the
MCP Python SDK + hdbcli (Python only, no Node.js). https://dineshaiacademy.com

Read-only twice over: log in as a read-only HANA user (MCP_READER, see setup/README.md), and every SQL statement
this server sends is a SELECT / WITH (or EXPLAIN PLAN of one). There is no tool that can change data.

34 tools, in six groups (names are what Claude sees):
  Connection  test_connection · get_session_info · show_config · show_env_vars
  Find        list_schemas · list_tables · list_views · list_calculation_views · list_procedures · list_functions ·
              list_sequences · list_synonyms · search_tables · search_columns
  Describe    describe_table · explain_table · describe_view · describe_procedure · describe_function ·
              describe_index · get_ddl
  Structure   list_indexes · list_constraints · list_foreign_keys · get_dependencies · list_privileges
  Data        run_query · query_next_page · get_sample_data · get_column_stats
  Performance get_table_stats · get_partition_info · explain_plan · get_expensive_queries

Claude starts this over stdio; see .mcp.json (written by setup\\setup.ps1).
"""
import os
import re
import uuid
from pathlib import Path

from hdbcli import dbapi
from mcp.server.mcpserver import MCPServer  # mcp 2.x (was FastMCP in 1.x)
from mcp.server.mcpserver.exceptions import ToolError  # message reaches Claude; other exceptions are hidden

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
_HANA_KEYS = ("HANA_HOST", "HANA_PORT", "HANA_USER", "HANA_PASSWORD", "HANA_SCHEMA", "HANA_ENCRYPT", "HANA_VALIDATE_CERT")


def _clean_env() -> None:
    """Make the HANA_* settings safe to use, whoever supplied them (.env, Claude Desktop's form, a hand-edited config).
    Blank values and still-unfilled "${user_config.x}" placeholders count as not set. Quotes and spaces around values
    are dropped (never from the password). A host pasted as https://host:443/ becomes host, and its port is used
    when no port was given."""
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
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host, flags=re.I).split("/")[0].split("?")[0].strip()
        if host.count(":") == 1:  # host:port (a bare IPv6 address has several colons)
            host, port = host.split(":")
            if port.isdigit():
                os.environ.setdefault("HANA_PORT", port)
        os.environ["HANA_HOST"] = host


def _flag(key: str, default: bool = True) -> bool:
    """A true/false setting. Accepts true/false, yes/no, 1/0, on/off in any case; anything else keeps the default."""
    value = (os.environ.get(key) or "").strip().lower()
    return True if value in ("true", "yes", "1", "on") else False if value in ("false", "no", "0", "off") else default


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


# What the driver's error text means, and what to do about it. First match wins.
_CONNECT_HINTS = (
    (("authentication failed", "invalid user name or password"),
     "The user name or password was rejected. Check both in {where}. HANA passwords are case-sensitive."),
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
     "instances stop automatically): start it in SAP HANA Cloud Central and retry in about 3 minutes. Otherwise the "
     "port is wrong (HANA Cloud uses 443)."),
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
     "the instance's allowed list (Instance > Manage > Allowed connections)."),
    (("invalid schema name",),
     "The default schema does not exist or this user cannot see it. Correct it, or clear it, in {where}."),
)


def _explain_connect_error(text: str, host: str, port: int) -> str:
    """Turn a driver error into: what is wrong, what to do, and the technical detail (for Claude and for support)."""
    low = text.lower()
    meaning = next((hint for words, hint in _CONNECT_HINTS if any(w in low for w in words)),
                   "Check the host, port, user name and password in {where}, and that the database is running.")
    return f"Cannot connect to SAP HANA at {host}:{port}. {meaning.format(where=_WHERE)} (Details: {text[:240]})"


_clean_env()

MAX_ROWS = 200


def _env_schema() -> str:
    """HANA_SCHEMA from .env, upper-cased (HANA stores unquoted names in upper case). Empty = the login's default."""
    return (os.environ.get("HANA_SCHEMA") or "").strip().strip('"').upper()


# The schema Claude works in. Empty = the login's own default schema in HANA.
DEFAULT_SCHEMA = _env_schema()
_SCHEMA_TEXT = f"schema {DEFAULT_SCHEMA}" if DEFAULT_SCHEMA else "the login's default schema"

mcp = MCPServer(
    "sap-hana-analyst",
    title=f"{_PRODUCT} · Dinesh AI Academy",
    description="Read-only SAP HANA Cloud access: business questions, schema exploration, DDL, statistics and query plans.",
    instructions=(
        f"Read-only access to SAP HANA Cloud. Names without a schema refer to {_SCHEMA_TEXT} "
        "(test_connection shows which one). Look before you query: list_tables / search_tables, then describe_table "
        "to check the real column names before writing SQL. Aggregate in SQL (GROUP BY, SUM, COUNT) instead of "
        "fetching raw rows: results are capped at 200 rows (use query_next_page for more). run_query accepts one "
        "SELECT or WITH statement; anything else is rejected. Use only numbers returned by the tools, and show the "
        "SQL you used when asked."
    ),
    website_url="https://dineshaiacademy.com",
    version="2.0.0",
)

_user = os.environ.get("HANA_USER", "")
_password = os.environ.get("HANA_PASSWORD", "")


# ---------------------------------------------------------------------------------------------- connection
def _connect():
    missing = _missing_settings()
    if missing:
        raise ToolError(
            f"{_PRODUCT} is not set up yet: {', '.join(missing)} not entered. Tell the user to open {_WHERE}, "
            "enter the HANA host, a read-only user and its password (the default schema is optional), save, and try "
            f"again. If this message stays, {_RESTART}. Nothing else is needed.")
    host, port = os.environ["HANA_HOST"], _port()
    # currentSchema: unqualified table names resolve to DEFAULT_SCHEMA. Without it, HANA uses the login's default.
    schema = {"currentSchema": DEFAULT_SCHEMA} if DEFAULT_SCHEMA else {}
    try:
        return dbapi.connect(address=host, port=port, user=_user, password=_password,
                             encrypt=_flag("HANA_ENCRYPT"), sslValidateCertificate=_flag("HANA_VALIDATE_CERT"),
                             **schema)
    except (dbapi.Error, OSError) as e:
        raise ToolError(_explain_connect_error(str(e), host, port)) from e


def _result(cur, limit: int) -> dict:
    columns = [d[0] for d in cur.description]
    rows = cur.fetchmany(limit + 1)
    truncated = len(rows) > limit
    rows = [[None if v is None else str(v) for v in r] for r in rows[:limit]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def _query(sql: str, params: tuple = (), limit: int = MAX_ROWS) -> dict:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return _result(cur, limit)
    except dbapi.Error as e:
        # SQL errors (unknown table, syntax, privilege) go back to Claude so it can fix the query.
        raise ToolError(f"HANA error: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------- helpers
def _schema_param(schema: str):
    """Schema for a metadata query: the one Claude asked for, else DEFAULT_SCHEMA, else None (= CURRENT_SCHEMA in SQL)."""
    return (schema.strip().upper() if schema and schema.strip() else DEFAULT_SCHEMA) or None


def _name(value: str, what: str) -> str:
    """A table / view / column name as HANA stores it (upper case). Must not be empty."""
    value = (value or "").strip()
    if not value:
        raise ToolError(f"Give the {what} name.")
    return value.upper()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _obj(table: str, schema: str = "") -> str:
    """A safe, quoted SQL reference to a table or view: "SCHEMA"."TABLE", or just "TABLE" (login's default schema)."""
    name = _quote(_name(table, "table"))
    sch = _schema_param(schema)
    return f"{_quote(sch)}.{name}" if sch else name


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql).strip()


def _read_only_sql(sql: str) -> str:
    """The cleaned statement if it is a single SELECT / WITH; otherwise a ToolError that tells Claude why."""
    cleaned = _strip_comments(sql).rstrip(";").strip()
    if ";" in cleaned:
        raise ToolError("Only one statement is allowed.")
    first_word = cleaned.split(None, 1)[0].upper() if cleaned else ""
    if first_word not in ("SELECT", "WITH"):
        raise ToolError("Only read-only SELECT / WITH queries are allowed by this server.")
    return cleaned


def _cap(n: int, top: int = MAX_ROWS) -> int:
    return max(1, min(int(n), top))


_SCHEMA_SQL = "SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA)"


# ---------------------------------------------------------------------------------------------- connection tools
@mcp.tool()
def test_connection() -> dict:
    """Check the HANA connection and return the current user, schema and database version."""
    return _query("SELECT CURRENT_USER AS USER_NAME, CURRENT_SCHEMA AS SCHEMA_NAME, VERSION FROM SYS.M_DATABASE")


@mcp.tool()
def get_session_info() -> dict:
    """Show this database session: user, schema, connection id, status and client."""
    return _query("SELECT USER_NAME, CURRENT_SCHEMA AS SCHEMA_NAME, CONNECTION_ID, CONNECTION_STATUS, START_TIME, "
                  "CLIENT_HOST FROM SYS.M_CONNECTIONS WHERE CONNECTION_ID = CURRENT_CONNECTION")


@mcp.tool()
def show_config() -> dict:
    """Show the settings this server uses (host, port, user, schema, SSL, row limit). The password is never shown."""
    missing = _missing_settings()
    rows = [["SETUP", "complete" if not missing else f"not entered yet: {', '.join(missing)}. Change it in {_WHERE}"],
            ["HANA_HOST", os.environ.get("HANA_HOST", "")], ["HANA_PORT", os.environ.get("HANA_PORT", "443")],
            ["HANA_USER", _user], ["HANA_SCHEMA", DEFAULT_SCHEMA or "(login's default schema)"],
            ["HANA_ENCRYPT", str(_flag("HANA_ENCRYPT")).lower()],
            ["HANA_VALIDATE_CERT", str(_flag("HANA_VALIDATE_CERT")).lower()], ["max rows per result", str(MAX_ROWS)]]
    return {"columns": ["SETTING", "VALUE"], "rows": rows, "row_count": len(rows), "truncated": False}


@mcp.tool()
def show_env_vars() -> dict:
    """List which HANA_* settings from .env are set. Values are shown except the password, which is hidden."""
    keys = ["HANA_HOST", "HANA_PORT", "HANA_USER", "HANA_PASSWORD", "HANA_SCHEMA", "HANA_ENCRYPT", "HANA_VALIDATE_CERT"]
    rows = [[k, ("set (hidden)" if os.environ.get(k) else "not set") if k == "HANA_PASSWORD"
             else (os.environ.get(k) or "not set")] for k in keys]
    return {"columns": ["VARIABLE", "VALUE"], "rows": rows, "row_count": len(rows), "truncated": False}


# ---------------------------------------------------------------------------------------------- find
@mcp.tool()
def list_schemas() -> dict:
    """List schemas visible to the connected user (system schemas excluded)."""
    return _query(
        "SELECT SCHEMA_NAME, SCHEMA_OWNER FROM SYS.SCHEMAS "
        "WHERE SCHEMA_NAME NOT LIKE '\\_SYS%' ESCAPE '\\' AND SCHEMA_NAME NOT IN ('SYS', 'SYSTEM') "
        "ORDER BY SCHEMA_NAME",
        limit=1000,
    )


@mcp.tool()
def list_tables(schema: str = "") -> dict:
    """List tables in a schema with their row counts. Defaults to the working schema (see test_connection)."""
    return _query(
        "SELECT TABLE_NAME, TABLE_TYPE, RECORD_COUNT FROM SYS.M_TABLES "
        f"WHERE {_SCHEMA_SQL} ORDER BY TABLE_NAME",
        (_schema_param(schema),),
        limit=1000,
    )


@mcp.tool()
def list_views(schema: str = "") -> dict:
    """List views in a schema (SQL views and calculation views)."""
    return _query(f"SELECT VIEW_NAME, VIEW_TYPE, IS_VALID FROM SYS.VIEWS WHERE {_SCHEMA_SQL} ORDER BY VIEW_NAME",
                  (_schema_param(schema),), limit=1000)


@mcp.tool()
def list_calculation_views(schema: str = "") -> dict:
    """List calculation views in a schema."""
    return _query("SELECT VIEW_NAME, VIEW_TYPE, IS_VALID FROM SYS.VIEWS "
                  f"WHERE {_SCHEMA_SQL} AND VIEW_TYPE = 'CALC' ORDER BY VIEW_NAME", (_schema_param(schema),), limit=1000)


@mcp.tool()
def list_procedures(schema: str = "") -> dict:
    """List stored procedures in a schema."""
    return _query(f"SELECT PROCEDURE_NAME, PROCEDURE_TYPE, IS_VALID FROM SYS.PROCEDURES WHERE {_SCHEMA_SQL} "
                  "ORDER BY PROCEDURE_NAME", (_schema_param(schema),), limit=1000)


@mcp.tool()
def list_functions(schema: str = "") -> dict:
    """List user-defined functions in a schema."""
    return _query(f"SELECT FUNCTION_NAME, FUNCTION_TYPE, IS_VALID FROM SYS.FUNCTIONS WHERE {_SCHEMA_SQL} "
                  "ORDER BY FUNCTION_NAME", (_schema_param(schema),), limit=1000)


@mcp.tool()
def list_sequences(schema: str = "") -> dict:
    """List sequences in a schema."""
    return _query(f"SELECT SEQUENCE_NAME, START_NUMBER, MIN_VALUE, MAX_VALUE, INCREMENT_BY, IS_CYCLED "
                  f"FROM SYS.SEQUENCES WHERE {_SCHEMA_SQL} ORDER BY SEQUENCE_NAME", (_schema_param(schema),), limit=1000)


@mcp.tool()
def list_synonyms(schema: str = "") -> dict:
    """List synonyms in a schema and the objects they point to."""
    return _query(f"SELECT SYNONYM_NAME, OBJECT_SCHEMA, OBJECT_NAME, OBJECT_TYPE FROM SYS.SYNONYMS "
                  f"WHERE {_SCHEMA_SQL} ORDER BY SYNONYM_NAME", (_schema_param(schema),), limit=1000)


@mcp.tool()
def search_tables(pattern: str, schema: str = "") -> dict:
    """Find tables whose name contains the text (case-insensitive), e.g. 'order'. Searches the working schema,
    or the schema you name."""
    return _query(f"SELECT SCHEMA_NAME, TABLE_NAME, TABLE_TYPE, RECORD_COUNT FROM SYS.M_TABLES WHERE {_SCHEMA_SQL} "
                  "AND UPPER(TABLE_NAME) LIKE ? ESCAPE '\\' ORDER BY TABLE_NAME",
                  (_schema_param(schema), "%" + _like(pattern) + "%"), limit=500)


@mcp.tool()
def search_columns(pattern: str, schema: str = "") -> dict:
    """Find columns whose name contains the text (case-insensitive), e.g. 'plant', across all tables of a schema."""
    return _query(f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE_NAME, LENGTH FROM SYS.TABLE_COLUMNS WHERE {_SCHEMA_SQL} "
                  "AND UPPER(COLUMN_NAME) LIKE ? ESCAPE '\\' ORDER BY TABLE_NAME, POSITION",
                  (_schema_param(schema), "%" + _like(pattern) + "%"), limit=500)


def _like(text: str) -> str:
    text = _name(text, "search text")
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------------------------- describe
@mcp.tool()
def describe_table(table: str, schema: str = "") -> dict:
    """Show the columns of a table: name, data type, length, nullability, default."""
    return _query(
        "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE, DEFAULT_VALUE "
        f"FROM SYS.TABLE_COLUMNS WHERE {_SCHEMA_SQL} AND TABLE_NAME = ? ORDER BY POSITION",
        (_schema_param(schema), _name(table, "table")),
        limit=1000,
    )


@mcp.tool()
def explain_table(table: str, schema: str = "") -> dict:
    """Explain a table in business terms: columns with their comments, the primary key and the row count."""
    sch, tbl = _schema_param(schema), _name(table, "table")
    cols = _query(
        "SELECT C.COLUMN_NAME, C.DATA_TYPE_NAME, C.LENGTH, C.SCALE, C.IS_NULLABLE, C.COMMENTS, "
        "CASE WHEN K.COLUMN_NAME IS NULL THEN '' ELSE 'PRIMARY KEY' END AS KEY_PART "
        "FROM SYS.TABLE_COLUMNS C LEFT JOIN SYS.CONSTRAINTS K ON K.SCHEMA_NAME = C.SCHEMA_NAME "
        "AND K.TABLE_NAME = C.TABLE_NAME AND K.COLUMN_NAME = C.COLUMN_NAME AND K.IS_PRIMARY_KEY = 'TRUE' "
        "WHERE C.SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND C.TABLE_NAME = ? ORDER BY C.POSITION",
        (sch, tbl), limit=1000)
    info = _query("SELECT TABLE_NAME, TABLE_TYPE, RECORD_COUNT, TABLE_SIZE FROM SYS.M_TABLES "
                  "WHERE SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND TABLE_NAME = ?", (sch, tbl))
    cols["table"] = dict(zip(info["columns"], info["rows"][0])) if info["rows"] else {}
    return cols


@mcp.tool()
def describe_view(view: str, schema: str = "") -> dict:
    """Show the columns of a view."""
    return _query("SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE FROM SYS.VIEW_COLUMNS "
                  f"WHERE {_SCHEMA_SQL} AND VIEW_NAME = ? ORDER BY POSITION",
                  (_schema_param(schema), _name(view, "view")), limit=1000)


@mcp.tool()
def describe_procedure(procedure: str, schema: str = "") -> dict:
    """Show the parameters of a stored procedure (direction, type, position)."""
    return _query("SELECT PARAMETER_NAME, PARAMETER_TYPE, DATA_TYPE_NAME, LENGTH, SCALE, POSITION "
                  f"FROM SYS.PROCEDURE_PARAMETERS WHERE {_SCHEMA_SQL} AND PROCEDURE_NAME = ? ORDER BY POSITION",
                  (_schema_param(schema), _name(procedure, "procedure")), limit=1000)


@mcp.tool()
def describe_function(function: str, schema: str = "") -> dict:
    """Show the parameters of a user-defined function (direction, type, position)."""
    return _query("SELECT PARAMETER_NAME, PARAMETER_TYPE, DATA_TYPE_NAME, LENGTH, SCALE, POSITION "
                  f"FROM SYS.FUNCTION_PARAMETERS WHERE {_SCHEMA_SQL} AND FUNCTION_NAME = ? ORDER BY POSITION",
                  (_schema_param(schema), _name(function, "function")), limit=1000)


@mcp.tool()
def describe_index(index: str, schema: str = "") -> dict:
    """Show an index: its table, type and the columns in order."""
    return _query("SELECT I.INDEX_NAME, I.TABLE_NAME, I.INDEX_TYPE, I.CONSTRAINT, C.COLUMN_NAME, C.POSITION "
                  "FROM SYS.INDEXES I JOIN SYS.INDEX_COLUMNS C ON C.SCHEMA_NAME = I.SCHEMA_NAME "
                  "AND C.TABLE_NAME = I.TABLE_NAME AND C.INDEX_NAME = I.INDEX_NAME "
                  "WHERE I.SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND I.INDEX_NAME = ? ORDER BY C.POSITION",
                  (_schema_param(schema), _name(index, "index")), limit=1000)


_TYPE_WITH_LENGTH = {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "VARBINARY", "ALPHANUM", "SHORTTEXT"}


def _column_ddl(name, dtype, length, scale, nullable, default) -> str:
    if dtype in _TYPE_WITH_LENGTH and length:
        dtype = f"{dtype}({length})"
    elif dtype == "DECIMAL" and length:
        dtype = f"DECIMAL({length}, {scale or 0})"
    null = "" if str(nullable).upper() == "TRUE" else " NOT NULL"
    dflt = f" DEFAULT {default}" if default not in (None, "") else ""
    return f"{_quote(name)} {dtype}{dflt}{null}"


@mcp.tool()
def get_ddl(object_name: str, schema: str = "", object_type: str = "") -> dict:
    """Get the CREATE statement of a table, view, procedure or function. object_type (TABLE, VIEW, PROCEDURE,
    FUNCTION) is optional: the server finds it. For tables, the statement is rebuilt from the catalog."""
    sch, name = _schema_param(schema), _name(object_name, "object")
    wanted = object_type.strip().upper()
    for kind, view, col in (("VIEW", "SYS.VIEWS", "VIEW_NAME"), ("PROCEDURE", "SYS.PROCEDURES", "PROCEDURE_NAME"),
                            ("FUNCTION", "SYS.FUNCTIONS", "FUNCTION_NAME")):
        if wanted in ("", kind):
            found = _query(f"SELECT DEFINITION FROM {view} WHERE {_SCHEMA_SQL} AND {col} = ?", (sch, name))
            if found["rows"]:
                return {"object_type": kind, "ddl": found["rows"][0][0]}
    if wanted in ("", "TABLE"):
        cols = _query("SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE, DEFAULT_VALUE "
                      f"FROM SYS.TABLE_COLUMNS WHERE {_SCHEMA_SQL} AND TABLE_NAME = ? ORDER BY POSITION", (sch, name), 1000)
        if cols["rows"]:
            kind = _query(f"SELECT TABLE_TYPE FROM SYS.TABLES WHERE {_SCHEMA_SQL} AND TABLE_NAME = ?", (sch, name))
            ttype = kind["rows"][0][0] if kind["rows"] else "COLUMN"
            pk = _query("SELECT COLUMN_NAME FROM SYS.CONSTRAINTS WHERE "
                        f"{_SCHEMA_SQL} AND TABLE_NAME = ? AND IS_PRIMARY_KEY = 'TRUE' ORDER BY POSITION", (sch, name), 100)
            lines = [_column_ddl(*r) for r in cols["rows"]]
            if pk["rows"]:
                lines.append("PRIMARY KEY (" + ", ".join(_quote(r[0]) for r in pk["rows"]) + ")")
            target = f"{_quote(sch)}.{_quote(name)}" if sch else _quote(name)
            return {"object_type": "TABLE", "ddl": f"CREATE {ttype} TABLE {target} (\n  " + ",\n  ".join(lines) + "\n)"}
    raise ToolError(f"No {wanted.lower() or 'table, view, procedure or function'} named {name} found.")


# ---------------------------------------------------------------------------------------------- structure
@mcp.tool()
def list_indexes(table: str, schema: str = "") -> dict:
    """List the indexes of a table."""
    return _query("SELECT INDEX_NAME, INDEX_TYPE, CONSTRAINT FROM SYS.INDEXES "
                  f"WHERE {_SCHEMA_SQL} AND TABLE_NAME = ? ORDER BY INDEX_NAME",
                  (_schema_param(schema), _name(table, "table")), limit=1000)


@mcp.tool()
def list_constraints(table: str, schema: str = "") -> dict:
    """List the primary-key and unique constraints of a table."""
    return _query("SELECT CONSTRAINT_NAME, COLUMN_NAME, POSITION, IS_PRIMARY_KEY, IS_UNIQUE_KEY FROM SYS.CONSTRAINTS "
                  f"WHERE {_SCHEMA_SQL} AND TABLE_NAME = ? ORDER BY CONSTRAINT_NAME, POSITION",
                  (_schema_param(schema), _name(table, "table")), limit=1000)


@mcp.tool()
def list_foreign_keys(table: str, schema: str = "") -> dict:
    """List the foreign keys of a table and the tables they point to."""
    return _query("SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_SCHEMA_NAME, REFERENCED_TABLE_NAME, "
                  "REFERENCED_COLUMN_NAME, DELETE_RULE, UPDATE_RULE FROM SYS.REFERENTIAL_CONSTRAINTS "
                  f"WHERE {_SCHEMA_SQL} AND TABLE_NAME = ? ORDER BY CONSTRAINT_NAME, POSITION",
                  (_schema_param(schema), _name(table, "table")), limit=1000)


@mcp.tool()
def get_dependencies(object_name: str, schema: str = "") -> dict:
    """Show what depends on an object (views, procedures that use it) and what it depends on."""
    sch, name = _schema_param(schema), _name(object_name, "object")
    return _query(
        "SELECT 'USES' AS DIRECTION, BASE_SCHEMA_NAME AS SCHEMA_NAME, BASE_OBJECT_NAME AS OBJECT_NAME, "
        "BASE_OBJECT_TYPE AS OBJECT_TYPE FROM SYS.OBJECT_DEPENDENCIES "
        "WHERE DEPENDENT_SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND DEPENDENT_OBJECT_NAME = ? "
        "UNION ALL SELECT 'USED BY', DEPENDENT_SCHEMA_NAME, DEPENDENT_OBJECT_NAME, DEPENDENT_OBJECT_TYPE "
        "FROM SYS.OBJECT_DEPENDENCIES WHERE BASE_SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND BASE_OBJECT_NAME = ?",
        (sch, name, sch, name), limit=1000)


@mcp.tool()
def list_privileges(schema: str = "") -> dict:
    """List what the connected user may do: privileges on a schema (default: the working schema) and system privileges."""
    return _query("SELECT DISTINCT PRIVILEGE, OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME FROM SYS.EFFECTIVE_PRIVILEGES "
                  "WHERE USER_NAME = CURRENT_USER AND (SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) "
                  "OR OBJECT_TYPE = 'SYSTEMPRIVILEGE') ORDER BY OBJECT_TYPE, PRIVILEGE",
                  (_schema_param(schema),), limit=1000)


# ---------------------------------------------------------------------------------------------- data
@mcp.tool()
def run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a single read-only SQL query (SELECT or WITH ... SELECT). Writes are rejected.

    Results are capped at max_rows (hard limit 200). For more rows use query_next_page.
    """
    return _query(_read_only_sql(sql), limit=_cap(max_rows))


@mcp.tool()
def query_next_page(sql: str, offset: int, max_rows: int = 100) -> dict:
    """Get the next page of a query: pass the same SELECT again with the offset to skip (0, 100, 200 ...).
    Include an ORDER BY so the pages are stable."""
    n = _cap(max_rows)
    return _query(f"SELECT * FROM ({_read_only_sql(sql)}) LIMIT {n} OFFSET {max(0, int(offset))}", limit=n)


@mcp.tool()
def get_sample_data(table: str, schema: str = "", rows: int = 10) -> dict:
    """Show a few example rows of a table or view (default 10, at most 50)."""
    n = _cap(rows, 50)
    return _query(f"SELECT TOP {n} * FROM {_obj(table, schema)}", limit=n)


@mcp.tool()
def get_column_stats(table: str, column: str, schema: str = "") -> dict:
    """Profile one column: row count, filled values, distinct values, smallest and largest value."""
    col = _quote(_name(column, "column"))
    return _query(f"SELECT COUNT(*) AS ROW_COUNT, COUNT({col}) AS FILLED, COUNT(*) - COUNT({col}) AS EMPTY, "
                  f"COUNT(DISTINCT {col}) AS DISTINCT_VALUES, MIN({col}) AS MIN_VALUE, MAX({col}) AS MAX_VALUE "
                  f"FROM {_obj(table, schema)}")


# ---------------------------------------------------------------------------------------------- performance
@mcp.tool()
def get_table_stats(table: str, schema: str = "") -> dict:
    """Show a table's size: rows, memory, disk and delta-store details."""
    return _query("SELECT T.TABLE_NAME, T.RECORD_COUNT, T.TABLE_SIZE, C.MEMORY_SIZE_IN_TOTAL, C.MEMORY_SIZE_IN_MAIN, "
                  "C.MEMORY_SIZE_IN_DELTA, C.RAW_RECORD_COUNT_IN_DELTA, C.LAST_MERGE_TIME "
                  "FROM SYS.M_TABLES T LEFT JOIN SYS.M_CS_TABLES C ON C.SCHEMA_NAME = T.SCHEMA_NAME "
                  "AND C.TABLE_NAME = T.TABLE_NAME "
                  "WHERE T.SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND T.TABLE_NAME = ?",
                  (_schema_param(schema), _name(table, "table")))


@mcp.tool()
def get_partition_info(table: str, schema: str = "") -> dict:
    """Show how a table is partitioned: each partition with its rows and memory. One row means not partitioned."""
    return _query("SELECT PART_ID, RECORD_COUNT, MEMORY_SIZE_IN_TOTAL FROM SYS.M_CS_TABLES "
                  f"WHERE {_SCHEMA_SQL} AND TABLE_NAME = ? ORDER BY PART_ID",
                  (_schema_param(schema), _name(table, "table")), limit=1000)


@mcp.tool()
def explain_plan(sql: str) -> dict:
    """Show how HANA would run a SELECT, without running it: the execution plan, operator by operator."""
    cleaned = _read_only_sql(sql)
    statement = "MCP_" + uuid.uuid4().hex[:12]  # the plan lives in HANA's session-local plan table
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"EXPLAIN PLAN SET STATEMENT_NAME = '{statement}' FOR {cleaned}")
        cur.execute("SELECT OPERATOR_ID, PARENT_OPERATOR_ID, OPERATOR_NAME, OPERATOR_DETAILS, EXECUTION_ENGINE, "
                    "SCHEMA_NAME, TABLE_NAME, OUTPUT_SIZE, SUBTREE_COST FROM SYS.EXPLAIN_PLAN_TABLE "
                    "WHERE STATEMENT_NAME = ? ORDER BY OPERATOR_ID", (statement,))
        return _result(cur, MAX_ROWS)
    except dbapi.Error as e:
        raise ToolError(f"HANA error: {e}") from e
    finally:
        conn.close()


@mcp.tool()
def get_expensive_queries(limit: int = 10) -> dict:
    """Show the slowest recent statements (needs HANA's expensive-statement trace and monitoring rights)."""
    n = _cap(limit, 50)
    return _query(f"SELECT TOP {n} START_TIME, DURATION_MICROSEC, DB_USER, SCHEMA_NAME, MEMORY_SIZE, CPU_TIME, STATEMENT_STRING "
                  "FROM SYS.M_EXPENSIVE_STATEMENTS ORDER BY DURATION_MICROSEC DESC", limit=n)


if __name__ == "__main__":
    mcp.run()
