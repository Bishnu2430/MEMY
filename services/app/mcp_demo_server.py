"""Demo MCP server (stdio) for team notes. The add_note description is read from
infra/mcp/notes_description.txt so you can change it and watch the platform block the tool
until an admin re-approves the new hash."""
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DESC_FILE = Path(os.environ.get("INFRA_DIR", "/app/config/infra")) / "mcp" / "notes_description.txt"
STORE = Path(os.environ.get("ARTIFACT_DIR", "/data/artifacts")) / "mcp_notes.json"
desc = DESC_FILE.read_text().strip() if DESC_FILE.exists() else "Add a note to the shared team notes."

mcp = FastMCP("notes")


def _load() -> list:
    return json.loads(STORE.read_text()) if STORE.exists() else []


@mcp.tool(description=desc)
def add_note(text: str) -> str:
    notes = _load()
    notes.append(text)
    STORE.write_text(json.dumps(notes))
    return f"Note #{len(notes)} saved."


@mcp.tool(description="List the shared team notes.")
def list_notes() -> str:
    notes = _load()
    return "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes)) or "No notes yet."


if __name__ == "__main__":
    mcp.run()
