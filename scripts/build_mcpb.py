"""Build the ClearQuery Claude Desktop extension (.mcpb) from this repository.

Run from the repository root:

    uv run --locked python scripts/build_mcpb.py

Output: dist/clearquery-<version>.mcpb  (double-click it, or drag it into Claude Desktop > Settings > Extensions)

What it does:
  1. Checks that every version number agrees (server, pyproject, plugin, marketplace, extension manifest).
  2. Reads the tool list from the server itself, so the manifest can never drift from the code.
  3. Stages only what the extension needs (no .claude-plugin folder: that is how the server knows it runs in
     Claude Desktop and points users to the right settings screen).
  4. Validates the manifest and packs it with the official mcpb CLI (Node.js needed for npx).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGE = DIST / "clearquery-mcpb"
MCPB = "@anthropic-ai/mcpb@2.1.2"  # pinned: the manifest is written for this CLI's schema (manifest_version 0.4)

# repository path -> path inside the extension
FILES = {
    "desktop-extension/manifest.json": "manifest.json",
    "desktop-extension/README.md": "README.md",
    "assets/icon.png": "icon.png",
    "server/hana_mcp_server.py": "server/hana_mcp_server.py",
    "pyproject.toml": "pyproject.toml",
    "uv.lock": "uv.lock",
    "LICENSE": "LICENSE",
    "NOTICE.md": "NOTICE.md",
}


def fail(message: str) -> None:
    print(f"BUILD FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def versions() -> str:
    sys.path.insert(0, str(ROOT / "server"))
    import hana_mcp_server as server  # noqa: E402  (imported for VERSION and the tool list)

    found = {
        "server VERSION": server.VERSION,
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "plugin.json": json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"],
        "marketplace.json": json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        ["plugins"][0]["version"],
        "desktop manifest": json.loads((ROOT / "desktop-extension/manifest.json").read_text(encoding="utf-8"))
        ["version"],
    }
    if len(set(found.values())) != 1:
        fail("version numbers differ: " + ", ".join(f"{k}={v}" for k, v in found.items()))
    return server.VERSION


def tool_list() -> list[dict]:
    import hana_mcp_server as server

    tools = asyncio.run(server.mcp.list_tools())
    out = []
    for t in tools:
        first = " ".join((t.description or "").split("\n\n")[0].split())
        out.append({"name": t.name, "description": first})
    return out


def npx() -> str:
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        fail("npx not found. Install Node.js (https://nodejs.org) to pack the extension.")
    return exe


def main() -> None:
    version = versions()
    tools = tool_list()
    print(f"ClearQuery {version}: {len(tools)} tools")

    shutil.rmtree(STAGE, ignore_errors=True)
    for src, dst in FILES.items():
        target = STAGE / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / src, target)

    manifest_path = STAGE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tools"] = tools
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if (STAGE / ".claude-plugin").exists() or (STAGE / ".mcp.json").exists():
        fail("plugin files must not be inside the extension")

    run = [npx(), "--yes", MCPB]
    subprocess.run(run + ["validate", str(manifest_path)], check=True)
    out = DIST / f"clearquery-{version}.mcpb"
    out.unlink(missing_ok=True)
    subprocess.run(run + ["pack", str(STAGE), str(out)], check=True)

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"\nBuilt {out.relative_to(ROOT)}  ({out.stat().st_size / 1024:.0f} KB)\nSHA-256 {digest}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
