"""ClearQuery launcher, by Dinesh AI Academy: prepares the computer, then starts the ClearQuery server.

Claude starts this file (plugin: .mcp.json, Claude Desktop extension: manifest.json) instead of the server itself,
so that problems *before* the server can run are explained, not just reported as "failed":

  1. Checks the computer. SAP's HANA driver exists only for some systems. Windows 11 on ARM is handled by running
     an Intel 64-bit Python, which Windows emulates. Unsupported systems get a clear explanation.
  2. Installs the exact pinned packages (uv sync --locked). Fixable problems are retried automatically: a company
     proxy that replaces HTTPS certificates (retried with the computer's own certificate store) and files briefly
     locked by antivirus software.
  3. Starts the real server (server/hana_mcp_server.py) with the same input and output.

If ClearQuery still cannot start, the launcher becomes a tiny stand-in MCP server with one tool,
clearquery_setup_status, so Claude can tell the user what happened, why, and what their options are, and can check
again after a fix.

Standard library only, and compatible with old Python versions, because it runs before any package is installed.
"""
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(HERE, "hana_mcp_server.py")
IS_EXTENSION = os.path.exists(os.path.join(ROOT, "manifest.json"))
SUPPORT = "support@dineshaiacademy.com"
SYNC_TIMEOUT_S = 900  # first start downloads Python and the packages
# Where the first start downloads from (also declared in .claude-plugin/plugin.json "network.hosts").
DOWNLOAD_HOSTS = "pypi.org, files.pythonhosted.org and releases.astral.sh (older uv versions: github.com)"
SUPPORTED = ("Windows 10 or 11 on Intel/AMD (64-bit), Windows 11 on ARM (through Windows' Intel emulation), "
             "macOS 13 or newer (Apple silicon or Intel), and Linux on x86_64, ARM64 or ppc64le")
RESTART = ("turn ClearQuery off and on again in Claude Desktop > Settings > Extensions" if IS_EXTENSION
           else "in Claude Code type /mcp, select the ClearQuery server and choose Reconnect (or type "
                "/reload-plugins)")


def log(message):
    sys.stderr.write("[ClearQuery launcher] " + message + "\n")
    sys.stderr.flush()


def icons():
    """The Dinesh AI Academy logo for the stand-in server (same as the real server announces)."""
    out = []
    for path in (os.path.join(ROOT, "assets", "icon-64.png"), os.path.join(ROOT, "icon-64.png")):
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            out.append({"src": "data:image/png;base64," + data, "mimeType": "image/png", "sizes": ["64x64"]})
            break
        except OSError:
            continue
    out.append({"src": "https://raw.githubusercontent.com/Dinesh-AI-Academy-Claude-Works/clearquery/main/assets/"
                       "icon.png", "mimeType": "image/png", "sizes": ["512x512"]})
    return out


def version():
    try:
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


# ------------------------------------------------------------------------------------------- the computer
class Problem(Exception):
    """A reason ClearQuery cannot start, written for the user: what happened, why, what they can do."""

    def __init__(self, title, why, options, details=""):
        Exception.__init__(self, title)
        self.title, self.why, self.options, self.details = title, why, options, details

    def text(self):
        lines = ["ClearQuery could not start: " + self.title + ".", "", "Why: " + self.why, "",
                 "What you can do:"]
        lines += ["  " + str(i) + ". " + o for i, o in enumerate(self.options, 1)]
        lines += ["", "This is not an error in your data or your HANA settings. Nothing was changed on this "
                      "computer or in the database."]
        if self.details:
            lines += ["", "Technical details (for support, " + SUPPORT + "):", self.details.strip()[-1500:]]
        return "\n".join(lines)


def windows_native_arch():
    """The processor of the computer itself, even when this Python runs under emulation."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        process, native = wintypes.USHORT(), wintypes.USHORT()
        if kernel32.IsWow64Process2(kernel32.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            return {0x8664: "AMD64", 0xAA64: "ARM64", 0x014C: "X86"}.get(native.value, hex(native.value))
    except Exception:
        pass
    return (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE")
            or platform.machine()).upper()


def describe_platform():
    """(system, detail) for this computer. CLEARQUERY_PLATFORM overrides it (used by the tests)."""
    forced = os.environ.get("CLEARQUERY_PLATFORM", "")
    if forced:
        system, _, detail = forced.partition(":")
        return system, detail
    if sys.platform == "win32":
        return "windows", "%s:%d" % (windows_native_arch(), sys.getwindowsversion().build)
    if sys.platform == "darwin":
        return "macos", platform.mac_ver()[0] or "0"
    return "linux", platform.machine().lower()


def python_choice():
    """The --python request for this computer (None = uv's normal choice), or a Problem if it cannot work."""
    system, detail = describe_platform()
    if system == "windows":
        arch, _, build = detail.partition(":")
        build = int(build or 0)
        if arch == "AMD64":
            return None
        if arch == "ARM64" and build >= 22000:  # Windows 11 on ARM emulates Intel 64-bit programs
            log("Windows on ARM: using an Intel 64-bit Python (Windows emulation) for SAP's HANA driver")
            return "cpython-3.12-windows-x86_64-none"
        if arch == "ARM64":
            raise Problem(
                "Windows 10 on ARM is not supported",
                "SAP publishes its HANA driver (hdbcli) for Intel/AMD Windows only. Windows 11 on ARM can run it "
                "through Intel 64-bit emulation, but Windows 10 on ARM can only emulate 32-bit programs, and a "
                "security library ClearQuery needs is not available as 32-bit.",
                ["Upgrade this computer to Windows 11 if Windows Update offers it (Settings > Windows Update). "
                 "ClearQuery then works automatically.",
                 "Or use ClearQuery from another computer: " + SUPPORTED + "."])
        raise Problem(
            "32-bit Windows is not supported",
            "This computer runs 32-bit Windows. A security library ClearQuery needs is only available for 64-bit.",
            ["Use ClearQuery from a 64-bit computer: " + SUPPORTED + "."])
    if system == "macos":
        try:
            major = int(detail.split(".")[0])
        except ValueError:
            major = 0
        if 0 < major < 13:
            raise Problem(
                "macOS %s is too old" % detail,
                "SAP publishes its HANA driver (hdbcli) for macOS 13 (Ventura) or newer.",
                ["Update macOS to version 13 or newer (Apple menu > System Settings > General > Software "
                 "Update). ClearQuery then works automatically.",
                 "Or use ClearQuery from another computer: " + SUPPORTED + "."])
        return None
    if detail not in ("x86_64", "amd64", "aarch64", "arm64", "ppc64le"):
        raise Problem(
            "this processor (%s) is not supported" % detail,
            "SAP publishes its HANA driver (hdbcli) for Linux on x86_64, ARM64 and ppc64le only.",
            ["Use ClearQuery from another computer: " + SUPPORTED + "."])
    return None


# ------------------------------------------------------------------------------------------- packages
def uv_exe():
    exe = os.environ.get("UV") or shutil.which("uv")
    if not exe:
        raise Problem(
            "the uv tool was not found",
            "ClearQuery uses uv to download Python and its packages. uv started this launcher but could not be "
            "found again (UV is not set and uv is not on PATH).",
            ["Install uv: https://docs.astral.sh/uv/getting-started/installation/ (Windows: winget install "
             "--id=astral-sh.uv -e; macOS: brew install uv), then " + RESTART + "."])
    return exe


def classify(output):
    """Turn uv's error output into a Problem the user can act on."""
    low = output.lower()
    if ("wheel for the current platform" in low or "no wheels with a matching platform tag" in low
            or "no matching distribution" in low):
        return Problem(
            "this computer's system is not supported by SAP's HANA driver",
            "SAP publishes its HANA driver (hdbcli) only as ready-built packages, and none exists for this "
            "computer's system or Python version.",
            ["Use ClearQuery from a supported computer: " + SUPPORTED + ".",
             "If this computer is one of those, report it to " + SUPPORT + " with the details below."],
            output)
    if ("certificate" in low or "unknownissuer" in low or "invalid peer" in low) and ("tls" in low or "ssl" in low
                                                                                      or "certificate" in low):
        return Problem(
            "a secure download was blocked by a certificate problem",
            "The first start downloads Python and packages from " + DOWNLOAD_HOSTS + ". A company proxy or "
            "antivirus that inspects HTTPS replaces their certificates, and the download was refused even with "
            "this computer's own certificate store.",
            ["Ask IT to allow " + DOWNLOAD_HOSTS + " (or to install the company certificate on this computer), "
             "then " + RESTART + ".",
             "Or start ClearQuery once on a network without the proxy (for example a phone hotspot). After the "
             "first start, it works offline from the download cache."],
            output)
    if any(w in low for w in ("failed to download", "failed to fetch", "error sending request", "dns error",
                              "connection refused", "timed out", "connection reset", "network",
                              "could not connect", "proxy")):
        return Problem(
            "Python or its packages could not be downloaded",
            "The first start (and the first start after an update) downloads Python and the exact packages "
            "ClearQuery needs from " + DOWNLOAD_HOSTS + ". This computer could not reach them.",
            ["Check the internet connection and any VPN, then " + RESTART + ".",
             "Behind a company proxy: set HTTPS_PROXY for this computer, or ask IT to allow those sites.",
             "Or start ClearQuery once on another network (for example a phone hotspot). After that it works "
             "from the download cache."],
            output)
    if "no space left" in low or "os error 112" in low or "os error 28" in low or "disk full" in low:
        return Problem(
            "the disk is full",
            "There was not enough free disk space to install Python and the packages (about 200 MB).",
            ["Free some disk space, then " + RESTART + "."],
            output)
    if "access is denied" in low or "os error 5" in low or "permission denied" in low or "os error 32" in low:
        return Problem(
            "files could not be written (access denied)",
            "Another program held ClearQuery's files: usually antivirus scanning them, or a second Claude window "
            "starting ClearQuery at the same time. ClearQuery already retried.",
            ["Close other Claude windows that use ClearQuery, wait a minute, then " + RESTART + ".",
             "If it repeats, ask IT to allow Claude's plugin folder in the antivirus software."],
            output)
    if "lockfile" in low and ("update" in low or "out of date" in low or "--locked" in low):
        return Problem(
            "this ClearQuery package is inconsistent",
            "The list of exact package versions shipped with ClearQuery does not match its requirements. This is "
            "a packaging mistake on our side, not a problem with your computer.",
            ["Update ClearQuery to the latest version, or report it to " + SUPPORT + " with the details below."],
            output)
    return Problem(
        "its packages could not be installed",
        "uv reported an unexpected problem while installing Python or the packages.",
        ["Try again: " + RESTART + ".",
         "If it repeats, report it to " + SUPPORT + " with the details below."],
        output)


def sync(python):
    """Install the pinned packages. Retries what can be fixed automatically; raises Problem otherwise."""
    cmd = [uv_exe(), "sync", "--locked", "--quiet", "--project", ROOT]
    if python:
        cmd += ["--python", python]
    env = dict(os.environ)
    attempts = [("first try", {}), ("computer's own certificate store", {"UV_NATIVE_TLS": "1"}),
                ("after waiting for locked files", {"UV_NATIVE_TLS": "1"})]
    output = ""
    for label, extra in attempts:
        env.update(extra)
        try:
            done = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=SYNC_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise Problem(
                "installing Python and the packages took too long",
                "The download did not finish in %d minutes. The connection may be very slow or blocked."
                % (SYNC_TIMEOUT_S // 60),
                ["Check the internet connection, then " + RESTART + "."]) from None
        except OSError as e:
            raise Problem("uv could not be run", "Starting uv failed (%s)." % e,
                          ["Reinstall uv (https://docs.astral.sh/uv/getting-started/installation/), then "
                           + RESTART + "."]) from None
        output = done.stdout.decode("utf-8", "replace")
        if done.returncode == 0:
            if label != "first try":
                log("packages installed (" + label + ")")
            return
        low = output.lower()
        if label == "first try" and "certificate" in low and "UV_NATIVE_TLS" not in os.environ:
            log("certificate problem; retrying with the computer's own certificate store")
            continue
        if label != "after waiting for locked files" and ("access is denied" in low or "os error 5" in low
                                                         or "os error 32" in low):
            log("files locked (antivirus?); retrying in 5 seconds")
            time.sleep(5)
            continue
        break
    raise classify(output)


# ------------------------------------------------------------------------------------------- stand-in server
def serve_problem(problem):
    """A minimal MCP server (JSON-RPC over stdio) that explains the problem instead of failing silently."""
    log(problem.text().replace("\n", " | "))
    tool = {
        "name": "clearquery_setup_status",
        "title": "ClearQuery setup status",
        "description": ("ClearQuery could not start on this computer (" + problem.title + "). Call this tool to get "
                        "the explanation and the user's options, and to check again after the user fixed it. "
                        "Explain the result to the user in plain words."),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "ClearQuery setup status", "readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    }
    instructions = ("ClearQuery (SAP HANA analyst by Dinesh AI Academy) could not start. Its HANA tools are not "
                    "available. When the user asks about SAP HANA, call clearquery_setup_status and explain the "
                    "result in plain words. Do not try to work around it.\n\n" + problem.text())

    def reply(msg_id, result=None, error=None):
        body = {"jsonrpc": "2.0", "id": msg_id}
        body.update({"error": error} if error else {"result": result})
        sys.stdout.buffer.write(json.dumps(body).encode("utf-8") + b"\n")  # exactly \n, also on Windows
        sys.stdout.buffer.flush()

    for raw in sys.stdin.buffer:
        raw = raw.decode("utf-8", "replace").strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict) or "id" not in msg or "method" not in msg:
            continue  # notifications and responses need no answer
        method, params = msg["method"], msg.get("params") or {}
        if method == "initialize":
            reply(msg["id"], {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "sap-hana-analyst", "version": version(),
                                             "title": "ClearQuery · Dinesh AI Academy (not started)",
                                             "icons": icons()},
                              "instructions": instructions})
        elif method == "ping":
            reply(msg["id"], {})
        elif method == "tools/list":
            reply(msg["id"], {"tools": [tool]})
        elif method == "tools/call":
            reply(msg["id"], {"content": [{"type": "text", "text": recheck(problem)}], "isError": True})
        elif method in ("resources/list", "prompts/list", "resources/templates/list"):
            key = {"resources/list": "resources", "prompts/list": "prompts",
                   "resources/templates/list": "resourceTemplates"}[method]
            reply(msg["id"], {key: []})
        else:
            reply(msg["id"], error={"code": -32601, "message": "ClearQuery is not running: " + problem.title})


def recheck(problem):
    """Check again (the user may have fixed it). Returns the text for Claude."""
    try:
        sync(python_choice())
    except Problem as now:
        return now.text()
    except Exception as e:  # never let the stand-in crash
        return problem.text() + "\n\n(Checking again failed too: %s)" % e
    return ("The problem is fixed: ClearQuery is now ready on this computer. To start it, " + RESTART + ". "
            "Then ask your question again.")


# ------------------------------------------------------------------------------------------- main
def main():
    try:
        python = python_choice()
        sync(python)
    except Problem as problem:
        serve_problem(problem)
        return 0
    except Exception as e:  # a bug in the launcher must still be explained, not crash
        serve_problem(Problem("the launcher hit an unexpected problem", "%s: %s" % (type(e).__name__, e),
                              ["Try again: " + RESTART + ".", "If it repeats, report it to " + SUPPORT + "."]))
        return 0
    cmd = [uv_exe(), "run", "--no-sync", "--quiet", "--project", ROOT]
    if python:
        cmd += ["--python", python]
    cmd += ["python", SERVER]
    if sys.platform != "win32":
        os.execv(cmd[0], cmd)  # replace this process: signals and stdio go straight to the server
    return subprocess.call(cmd)  # Windows: same input and output, exit code passed on


if __name__ == "__main__":
    sys.exit(main())
