"""Offline tests for the ClearQuery server: no SAP HANA needed. A fake driver stands in for hdbcli.

Run:  uv run --locked python tests/test_offline.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.update({"HANA_HOST": " https://demo.hanacloud.ondemand.com:443/ ", "HANA_USER": "MCP_READER",
                   "HANA_PASSWORD": " keep spaces ", "HANA_SCHEMA": "retail", "HANA_ENCRYPT": "YES",
                   "HANA_VALIDATE_CERT": "${user_config.validate_cert}", "HANA_PORT": "",
                   "HANA_QUERY_TIMEOUT": "abc"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import hana_mcp_server as s  # noqa: E402
from hdbcli import dbapi  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

s.time.sleep = lambda _: None  # no waiting in tests
PASSED = []


def check(name):
    def wrap(fn):
        fn()
        PASSED.append(name)
        return fn
    return wrap


def raises(fn, *words):
    try:
        fn()
    except ToolError as e:
        text = str(e)
        missing = [w for w in words if w.lower() not in text.lower()]
        assert not missing, f"message lacks {missing}: {text}"
        return text
    raise AssertionError("expected a ToolError")


# ------------------------------------------------------------------ fake driver
class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def setquerytimeout(self, s):
        self.conn.timeout = s

    def execute(self, sql, params=()):
        self.conn.log.append((sql, params))
        plan = self.conn.script.pop(0) if self.conn.script else ("rows", ["X"], [])
        if plan[0] == "error":
            raise plan[1]
        _, cols, rows = plan
        self.description = [(c,) for c in cols]
        self._rows = list(rows)

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def close(self):
        pass


class FakeConn:
    def __init__(self, script):
        self.script, self.log, self.alive, self.timeout = script, [], True, None

    def cursor(self):
        return FakeCursor(self)

    def isconnected(self):
        return self.alive

    def ping(self):
        return self.alive

    def setautocommit(self, _):
        pass

    def setclientinfo(self, *_):
        pass

    def close(self):
        self.alive = False


def install(*conn_scripts, connect_errors=()):
    """Each dbapi.connect() call first raises the next connect error (if any), then returns the next FakeConn."""
    conns = [FakeConn(list(sc)) for sc in conn_scripts]
    errors = list(connect_errors)
    calls = {"n": 0}

    def connect(**kw):
        calls["n"] += 1
        calls["kw"] = kw
        if errors:
            raise errors.pop(0)
        return conns.pop(0)
    s.dbapi.connect = connect
    s._hana.drop()
    s._hana._auth_failed_at = 0.0
    return calls


# ------------------------------------------------------------------ settings
@check("settings are cleaned")
def _():
    assert os.environ["HANA_HOST"] == "demo.hanacloud.ondemand.com"
    assert os.environ["HANA_PORT"] == "443"
    assert os.environ["HANA_PASSWORD"] == " keep spaces "      # never trimmed
    assert "HANA_VALIDATE_CERT" not in os.environ                # unfilled placeholder = not set
    assert s._flag("HANA_ENCRYPT") is True and s._flag("HANA_VALIDATE_CERT") is True
    assert s.DEFAULT_SCHEMA == "RETAIL" and s.QUERY_TIMEOUT_S == 120


@check("IPv6 and user@host hosts")
def _():
    saved = dict(os.environ)
    try:
        os.environ.update({"HANA_HOST": "[fe80::1]:30015"}); os.environ.pop("HANA_PORT", None)
        s._clean_env()
        assert os.environ["HANA_HOST"] == "fe80::1" and os.environ["HANA_PORT"] == "30015"
        os.environ.update({"HANA_HOST": "user@db.example.com."}); s._clean_env()
        assert os.environ["HANA_HOST"] == "db.example.com"
    finally:
        os.environ.clear(); os.environ.update(saved)


# ------------------------------------------------------------------ SQL guard
@check("SQL guard allows real read-only queries")
def _():
    for sql in ("select 1 from dummy;", "  WITH a AS (SELECT 1 X FROM DUMMY) SELECT * FROM a ;; ",
                "(SELECT 1 FROM DUMMY)", "SELECT 'a;b' AS T, 'DROP TABLE x' AS U FROM DUMMY",
                'SELECT "UPDATE", "Insert Date" FROM T', "SELECT CREATE_DATE, UPDATED_BY FROM T",
                "SELECT 1 FROM DUMMY -- ; DELETE FROM T", "/* drop */ SELECT 1 FROM DUMMY",
                "SELECT * FROM T WHERE NAME LIKE '%?%'", "SELECT t.UPDATE FROM T t",
                "SELECT REPLACE(NAME, 'a', 'b') FROM T"):
        s._read_only_sql(sql)


@check("SQL guard blocks writes and tricks")
def _():
    raises(lambda: s._read_only_sql("DELETE FROM T"), "SELECT / WITH")
    raises(lambda: s._read_only_sql("SELECT 1 FROM DUMMY; DROP TABLE T"), "one statement")
    raises(lambda: s._read_only_sql("SELECT * FROM T FOR UPDATE"), "FOR UPDATE")
    raises(lambda: s._read_only_sql("WITH x AS (SELECT 1 FROM DUMMY) INSERT INTO T SELECT * FROM x"), "INSERT")
    raises(lambda: s._read_only_sql("SELECT * INTO T2 FROM T"), "INTO")
    raises(lambda: s._read_only_sql("SELECT 'open FROM T"), "never closed")
    raises(lambda: s._read_only_sql("SELECT 1 /* open"), "never closed")
    raises(lambda: s._read_only_sql("   -- only a comment"), "empty")
    raises(lambda: s._read_only_sql(""), "Give a SQL query")
    raises(lambda: s._read_only_sql("SELECT 1" + " " * s.MAX_SQL_CHARS), "limit")


@check("top-level keyword detection ignores subqueries")
def _():
    q = s._read_only_sql("SELECT * FROM (SELECT * FROM T ORDER BY A LIMIT 5) X")
    assert not q.top_has("ORDER") and not q.top_has("LIMIT")
    q = s._read_only_sql("SELECT TOP 5 * FROM T ORDER BY A")
    assert q.top_has("ORDER") and q.top_has("TOP")


# ------------------------------------------------------------------ values and names
@check("values are bounded and readable")
def _():
    from decimal import Decimal
    assert s._cell(Decimal("1E+3")) == "1000"
    assert s._cell(b"\x01\xff") == "0x01FF"
    assert "bytes" in s._cell(b"x" * 500)
    assert "cut at" in s._cell("y" * 10_000) and len(s._cell("y" * 10_000)) < 4100
    assert s._cell(None) is None


@check("names: quoting, case and SCHEMA.TABLE")
def _():
    assert s._qualified("sales.orders", "", "table") == ("SALES", "ORDERS")
    assert s._qualified('"MySchema"."My.Table"', "", "table") == ("MySchema", "My.Table")
    assert s._qualified("orders", "", "table") == ("RETAIL", "ORDERS")   # default schema
    raises(lambda: s._qualified("a.b", "c", "table"), "Use one of them")
    raises(lambda: s._qualified("a.b.c", "", "table"), "SCHEMA.NAME")
    raises(lambda: s._name("  ", "table"), "Give the table name")
    assert s._closest("ORDRS", ["ORDERS", "ORDER_ITEMS", "CUSTOMERS"])[0] == "ORDERS"


# ------------------------------------------------------------------ connection: self-healing
@check("missing settings give a setup message, no connect attempt")
def _():
    saved = os.environ.pop("HANA_PASSWORD")
    try:
        calls = install()
        raises(lambda: s.run_query("SELECT 1 FROM DUMMY"), "not set up", "password", "Configure")
        assert calls["n"] == 0
    finally:
        os.environ["HANA_PASSWORD"] = saved


@check("connection is reused across calls")
def _():
    calls = install([("rows", ["A"], [(1,)]), ("rows", ["A"], [(2,)])])
    assert s.run_query("SELECT 1 AS A FROM DUMMY")["rows"] == [["1"]]
    assert s.run_query("SELECT 2 AS A FROM DUMMY")["rows"] == [["2"]]
    assert calls["n"] == 1 and calls["kw"]["connectTimeout"] == s.CONNECT_TIMEOUT_MS
    assert calls["kw"]["currentSchema"] == "RETAIL"


@check("lost connection is reopened and the query retried once")
def _():
    lost = dbapi.Error(-10807, "Connection down")
    calls = install([("error", lost)], [("rows", ["A"], [(7,)])])
    before = s._hana.health.retried_queries
    assert s.run_query("SELECT 7 AS A FROM DUMMY")["rows"] == [["7"]]
    assert calls["n"] == 2 and s._hana.health.retried_queries == before + 1


@check("connection lost twice stops with a clear message")
def _():
    lost = dbapi.Error(-10807, "Connection down")
    install([("error", lost)], [("error", lost)])
    raises(lambda: s.run_query("SELECT 1 FROM DUMMY"), "dropped twice")


@check("dead idle connection is replaced before use")
def _():
    calls = install([("rows", ["A"], [(1,)])], [("rows", ["A"], [(2,)])])
    s.run_query("SELECT 1 AS A FROM DUMMY")
    s._hana._conn.alive = False
    assert s.run_query("SELECT 2 AS A FROM DUMMY")["rows"] == [["2"]]
    assert calls["n"] == 2


@check("transient connect error is retried once")
def _():
    calls = install([("rows", ["A"], [(1,)])],
                    connect_errors=[dbapi.Error(-10709, "Connection failed (RTE:[89008] Socket closed by peer)")])
    assert s.run_query("SELECT 1 AS A FROM DUMMY")["row_count"] == 1
    assert calls["n"] == 2


@check("rejected password is not retried (protects from lockout)")
def _():
    auth = dbapi.Error(10, "authentication failed")
    calls = install(connect_errors=[auth, auth, auth])
    raises(lambda: s.run_query("SELECT 1 FROM DUMMY"), "rejected", "password")
    raises(lambda: s.run_query("SELECT 1 FROM DUMMY"), "waits")
    assert calls["n"] == 1


@check("stopped HANA Cloud instance is explained")
def _():
    install(connect_errors=[dbapi.Error(-10709, "Connection failed (RTE:[89006] received HTTP 503)")] * 2)
    raises(lambda: s.run_query("SELECT 1 FROM DUMMY"), "instance is stopped", "diagnose_connection")


# ------------------------------------------------------------------ errors become guidance
@check("unknown table: did you mean + other schema")
def _():
    err = dbapi.Error(259, "invalid table name:  Could not find table/view ORDRS in schema RETAIL: line 1 col 15")
    install([("error", err),
             ("rows", ["S", "O", "T"], [("SALES", "ORDRS", "TABLE")]),         # exists elsewhere
             ("rows", ["N"], [("ORDERS",), ("CUSTOMERS",)])])                  # names in RETAIL
    raises(lambda: s.run_query("SELECT * FROM ORDRS"), "259", "Did you mean: ORDERS", "SALES.ORDRS")


@check("unknown column: did you mean")
def _():
    err = dbapi.Error(260, "invalid column name: CUSTMER_ID: line 1 col 8")
    install([("error", err),
             ("rows", ["S"], [("RETAIL",)]),                                    # schema exact
             ("rows", ["S", "O", "T"], [("RETAIL", "ORDERS", "TABLE")]),        # object exact
             ("rows", ["C", "T"], [("ORDER_ID", "INTEGER"), ("CUSTOMER_ID", "INTEGER")])])
    raises(lambda: s.run_query("SELECT CUSTMER_ID FROM ORDERS"), "260", "describe_table", "CUSTOMER_ID")


@check("query timeout is explained")
def _():
    install([("error", dbapi.Error(139, "current operation cancelled by request and transaction rolled back"))])
    raises(lambda: s.run_query("SELECT * FROM BIG"), "stopped after", "GROUP BY")


@check("privilege error names the GRANT")
def _():
    install([("error", dbapi.Error(258, "insufficient privilege: Not authorized"))])
    raises(lambda: s.run_query("SELECT * FROM HR.SALARY"), "258", "GRANT SELECT")


# ------------------------------------------------------------------ tools
@check("wrong case is corrected automatically")
def _():
    install([("rows", ["S"], [("RETAIL",)]),                       # schema exact
             ("rows", ["S", "O", "T"], []),                         # exact name: none
             ("rows", ["S", "O", "T"], [("RETAIL", "Orders", "TABLE")]),  # case-insensitive: one
             ("rows", ["COLUMN_NAME"], [("ID",)])])
    out = s.describe_table("orders")
    assert out["object"] == "RETAIL.Orders" and "upper/lower case" in out["note"]


@check("synonyms are followed")
def _():
    install([("rows", ["S"], [("RETAIL",)]),
             ("rows", ["S", "O", "T"], [("RETAIL", "KUNDEN", "SYNONYM")]),
             ("rows", ["S", "O", "T"], [("SAPHANADB", "KNA1", "TABLE")]),
             ("rows", ["A", "B"], [(1, 2)])])
    out = s.get_sample_data("kunden", rows=999)
    assert out["object"] == "SAPHANADB.KNA1" and "synonym" in out["note"]
    assert s._hana._conn.log[-1][0] == 'SELECT TOP 50 * FROM "SAPHANADB"."KNA1"'   # rows capped at 50


@check("paging adds LIMIT/OFFSET and reports the next offset")
def _():
    install([("rows", ["A"], [(i,) for i in range(11)])])
    out = s.query_next_page("SELECT A FROM T ORDER BY A", offset=20, max_rows=10)
    assert s._hana._conn.log[-1][0].endswith("LIMIT 11 OFFSET 20")
    assert out["row_count"] == 10 and out["next_offset"] == 30


@check("an unexpected bug never crashes the server")
def _():
    install([("rows", ["A"], [(1,)])])
    original, s._result = s._result, lambda *a: 1 / 0
    try:
        raises(lambda: s.run_query("SELECT 1 FROM DUMMY"), "unexpected problem", "still running", s.SUPPORT)
    finally:
        s._result = original


@check("bad names are rejected before connecting")
def _():
    calls = install()
    raises(lambda: s.get_sample_data("a.b.c"), "SCHEMA.NAME")
    raises(lambda: s.get_column_stats("ORDERS", "  "), "column name")
    raises(lambda: s.describe_table("sales.orders", schema="hr"), "Use one of them")
    assert calls["n"] == 0


@check("all 35 tools are registered as read-only")
def _():
    tools = asyncio.run(s.mcp.list_tools())
    assert len(tools) == 35, len(tools)
    assert all(t.annotations and t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    names = {t.name for t in tools}
    assert {"run_query", "diagnose_connection", "describe_table"} <= names
    sig = next(t for t in tools if t.name == "run_query").input_schema
    assert "sql" in sig["properties"] and "max_rows" in sig["properties"]


print(f"OK: {len(PASSED)} checks passed")
for name in PASSED:
    print(f"  ✓ {name}")
