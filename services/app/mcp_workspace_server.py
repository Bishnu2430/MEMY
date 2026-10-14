"""MCP server: a team-scoped project workspace (files agents build together).

The platform passes MEMY_TEAM_ID and MEMY_PROJECT in the environment of each session, so an agent can only
reach its own team's project folder; paths are resolved and checked to stay inside it.
"""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

BASE = Path(os.environ.get("ARTIFACT_DIR", "/data/artifacts")) / "workspace"
TEAM = os.environ.get("MEMY_TEAM_ID", "unknown")
PROJECT = "".join(c for c in os.environ.get("MEMY_PROJECT", "default") if c.isalnum() or c in "-_") or "default"
ROOT = (BASE / TEAM / PROJECT).resolve()

mcp = FastMCP("workspace")


def _path(rel: str) -> Path:
    p = (ROOT / rel.lstrip("/")).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("path escapes the project workspace")
    return p


@mcp.tool(description="List files in the team's project workspace (recursive, with sizes).",
          annotations=ToolAnnotations(readOnlyHint=True))
def list_files(path: str = ".") -> str:
    base = _path(path)
    if not base.exists():
        return f"(empty) project '{PROJECT}' has no files yet"
    rows = [f"{f.relative_to(ROOT)}  ({f.stat().st_size} bytes)" for f in sorted(base.rglob("*")) if f.is_file()]
    return "\n".join(rows[:500]) or "(no files)"


@mcp.tool(description="Read a text file from the project workspace.", annotations=ToolAnnotations(readOnlyHint=True))
def read_file(path: str) -> str:
    p = _path(path)
    if not p.is_file():
        return f"No such file: {path}"
    return p.read_text(errors="replace")[:60000]


@mcp.tool(description="Create or overwrite a text file in the project workspace (creates folders as needed).",
          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def write_file(path: str, content: str) -> str:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {p.relative_to(ROOT)} ({len(content.encode())} bytes) in project '{PROJECT}'."


@mcp.tool(description="Delete a file from the project workspace.",
          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def delete_file(path: str) -> str:
    p = _path(path)
    if not p.is_file():
        return f"No such file: {path}"
    p.unlink()
    return f"Deleted {path}."


if __name__ == "__main__":
    mcp.run()
