"""Tests for server/launcher.py: what happens on computers where ClearQuery cannot simply start. No HANA needed.

Each case starts the launcher exactly like Claude does (uv run --no-project server/launcher.py) and talks to it with
a real MCP client. Platforms are simulated with CLEARQUERY_PLATFORM; the download failure is real (a dead proxy and an
empty download cache). Throwaway environments are used, so the project's .venv is not touched.

Run:  uv run --locked python tests/test_launcher.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="clearquery-launcher-"))
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("HANA_", "VIRTUAL_ENV", "CLEARQUERY_"))}
BASE_ENV.update({"PYTHONIOENCODING": "utf-8", "UV_PROJECT_ENVIRONMENT": str(TMP / "venv")})
RESULTS = []


async def start(env):
    params = StdioServerParameters(command="uv", args=["run", "--quiet", "--no-project",
                                                       str(ROOT / "server" / "launcher.py")], env={**BASE_ENV, **env})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            first = await s.call_tool(tools[0].name if len(tools) == 1 else "test_connection", {})
            text = " ".join(getattr(c, "text", "") for c in first.content)
            return init, tools, text


def case(name, env, expect_tools, *words):
    try:
        init, tools, text = asyncio.run(start(env))
        problems = []
        if len(tools) != expect_tools:
            problems.append(f"expected {expect_tools} tools, got {len(tools)}")
        missing = [w for w in words if w.lower() not in text.lower()]
        if missing:
            problems.append(f"answer lacks {missing}")
        if expect_tools == 1 and "not started" not in (init.server_info.title or ""):
            problems.append("stand-in server title should say 'not started'")
        if expect_tools == 1 and "clearquery_setup_status" not in (init.instructions or ""):
            problems.append("instructions should tell Claude to call clearquery_setup_status")
        RESULTS.append((name, not problems, "; ".join(problems) or text.splitlines()[0][:150]))
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))


def classification():
    """uv's real error texts (captured from uv 0.11) map to the right explanation."""
    sys.path.insert(0, str(ROOT / "server"))
    import launcher
    samples = {
        "unsupported system": ("error: Distribution `hdbcli==2.30.27 @ registry+https://pypi.org/simple` can't be "
                               "installed because it doesn't have a source distribution or wheel for the current "
                               "platform", "not supported by SAP's HANA driver"),
        "company proxy certificate": ("error: Request failed: invalid peer certificate: UnknownIssuer (TLS)",
                                      "certificate problem"),
        "blocked download": ("× Failed to download `starlette==1.10.0` ├─▶ Request failed after 3 retries",
                             "could not be downloaded"),
        "full disk": ("error: failed to write: No space left on device (os error 28)", "disk is full"),
        "antivirus lock": ("error: failed to remove file: Access is denied. (os error 5)", "access denied"),
        "packaging mistake": ("error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.",
                              "package is inconsistent"),
        "anything else": ("error: something new and strange", "could not be installed"),
    }
    for name, (text, expected) in samples.items():
        got = launcher.classify(text)
        ok = expected.lower() in got.title.lower() and got.options and text in got.text()
        RESULTS.append((f"explains: {name}", ok, got.title))


try:
    classification()
    case("Windows 10 on ARM: clear 'not supported' with upgrade option", {"CLEARQUERY_PLATFORM": "windows:ARM64:19045"},
         1, "Windows 10 on ARM is not supported", "Upgrade this computer to Windows 11", "Nothing was changed")
    case("32-bit Windows: clear 'not supported'", {"CLEARQUERY_PLATFORM": "windows:X86:19045"},
         1, "32-bit Windows is not supported", "64-bit computer")
    case("macOS 12: update option", {"CLEARQUERY_PLATFORM": "macos:12.7.4"},
         1, "macOS 12.7.4 is too old", "Software Update")
    case("Linux on RISC-V: clear 'not supported'", {"CLEARQUERY_PLATFORM": "linux:riscv64"},
         1, "processor (riscv64) is not supported")
    case("Windows 11 on ARM: real server on Intel 64-bit Python",
         {"CLEARQUERY_PLATFORM": "windows:ARM64:22631"}, 35, "not set up yet")
    case("download blocked: explained, with network options",
         {"UV_CACHE_DIR": str(TMP / "empty-cache"), "UV_PROJECT_ENVIRONMENT": str(TMP / "venv-offline"),
          "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "UV_HTTP_RETRIES": "0"},
         1, "could not be downloaded", "phone hotspot", "HTTPS_PROXY")
    case("normal computer: real server starts", {}, 35, "not set up yet")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

for name, ok, detail in RESULTS:
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
failed = sum(1 for _, ok, _ in RESULTS if not ok)
print(f"\n==== {len(RESULTS) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
