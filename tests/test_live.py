"""Live test of ClearQuery against a real SAP HANA, launched exactly like Claude Code launches the plugin.

Reads the connection from .env in the plugin folder (HANA_HOST, HANA_PORT, HANA_USER, HANA_PASSWORD, HANA_SCHEMA,
HANA_ENCRYPT, HANA_VALIDATE_CERT). Never prints the password. Every call is read-only.

Run:  uv run --locked python tests/test_live.py [TABLE ...]
      e.g. uv run --locked python tests/test_live.py ACDOCA EKKO KNA1

To test a built Claude Desktop extension instead, unpack the .mcpb and point CLEARQUERY_EXTENSION_DIR at that
folder: the server is then started the way Claude Desktop starts it (uv run --directory <folder>).
"""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
EXTENSION = os.environ.get("CLEARQUERY_EXTENSION_DIR", "")
# Same commands as desktop-extension/manifest.json and .mcp.json: uv starts the launcher, which starts the server.
LAUNCH = (["run", "--quiet", "--no-project", "--directory", EXTENSION, "server/launcher.py"] if EXTENSION else
          ["run", "--quiet", "--no-project", f"{ROOT}/server/launcher.py"])
env = dict(os.environ)
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
SECRET = env.get("HANA_PASSWORD", "")


def short(text, n=700):
    text = text.replace(SECRET, "<hidden>") if SECRET else text
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "rows" in d:
            rows = d["rows"][:5]
            extra = {k: v for k, v in d.items() if k not in ("rows", "columns")}
            text = f"columns={d['columns']}\nrows(first 5)={rows}\n{extra}"
    except Exception:
        pass
    return text[:n]


async def main(calls):
    params = StdioServerParameters(command="uv", args=LAUNCH, env=env)
    ok = bad = 0
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for label, name, args, expect_error in calls:
                res = await s.call_tool(name, args)
                err = bool(getattr(res, "is_error", getattr(res, "isError", False)))
                text = " ".join(getattr(c, "text", "") for c in res.content)
                good = err == expect_error
                ok, bad = ok + good, bad + (not good)
                print(f"\n{'PASS' if good else 'FAIL'}  {label}  [{name}]\n{short(text)}")
    print(f"\n==== {ok} passed, {bad} failed")


tables = [a.upper() for a in sys.argv[1:]]
calls = [
    ("connection doctor", "diagnose_connection", {}, False),
    ("test connection", "test_connection", {}, False),
    ("session health", "get_session_info", {}, False),
    ("list schemas", "list_schemas", {}, False),
    ("list tables in RETAIL", "list_tables", {}, False),
    ("schema in wrong case", "list_tables", {"schema": "retail"}, False),
    ("misspelled schema -> did you mean", "list_tables", {"schema": "RETAL"}, True),
    ("search tables", "search_tables", {"pattern": "ord"}, False),
    ("search columns", "search_columns", {"pattern": "id"}, False),
    ("privileges", "list_privileges", {}, False),
    ("guard: DELETE rejected", "run_query", {"sql": "DELETE FROM X"}, True),
    ("guard: two statements rejected", "run_query", {"sql": "SELECT 1 FROM DUMMY; SELECT 2 FROM DUMMY"}, True),
    ("guard: ';' inside a string is fine", "run_query", {"sql": "SELECT 'a;b' AS T FROM DUMMY;"}, False),
    ("syntax error explained", "run_query", {"sql": "SELEC 1 FROM DUMMY"}, True),
    ("unknown table -> did you mean", "run_query", {"sql": "SELECT * FROM NO_SUCH_TABL"}, True),
    ("public synonym DUMMY", "get_sample_data", {"table": "dummy"}, False),
    ("WITH query paging", "query_next_page",
     {"sql": "WITH n AS (SELECT TOP 30 ROW_NUMBER() OVER () AS N FROM SYS.OBJECTS) SELECT N FROM n ORDER BY N",
      "offset": 10, "max_rows": 5}, False),
    ("explain plan on SYS view gives a clear message", "explain_plan", {"sql": "SELECT COUNT(*) FROM SYS.OBJECTS"}, True),
    ("FOR UPDATE blocked", "run_query", {"sql": "SELECT * FROM DUMMY FOR UPDATE"}, True),
    ("show config", "show_config", {}, False),
]
for t in tables:  # per-table checks, using real table names found in RETAIL
    lower = t.lower()
    calls += [
        (f"describe {lower} (lower case)", "describe_table", {"table": lower}, False),
        (f"explain {t}", "explain_table", {"table": t}, False),
        (f"sample {t}", "get_sample_data", {"table": f"RETAIL.{t}", "rows": 3}, False),
        (f"DDL {t}", "get_ddl", {"object_name": t}, False),
        (f"indexes {t}", "list_indexes", {"table": t}, False),
        (f"constraints {t}", "list_constraints", {"table": t}, False),
        (f"foreign keys {t}", "list_foreign_keys", {"table": t}, False),
        (f"dependencies {t}", "get_dependencies", {"object_name": t}, False),
        (f"table stats {t}", "get_table_stats", {"table": t}, False),
        (f"partitions {t}", "get_partition_info", {"table": t}, False),
        (f"misspelled table {t[:-1]} -> did you mean", "describe_table", {"table": t[:-1]}, True),
        (f"bad column in query on {t}", "run_query", {"sql": f"SELECT NO_SUCH_COLUMN_X FROM {t}"}, True),
        (f"bad column in stats on {t}", "get_column_stats", {"table": t, "column": "no_such_col"}, True),
        (f"count rows {t}", "run_query", {"sql": f"SELECT COUNT(*) AS N FROM {t}"}, False),
        (f"explain plan on {t}", "explain_plan", {"sql": f"SELECT COUNT(*) FROM {t}"}, False),
    ]
sys.exit(1 if asyncio.run(main(calls)) else 0)
