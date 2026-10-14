"""MCP client: stateless per-call sessions (stdio or streamable HTTP) with tool-description pinning."""
import hashlib
import json
import logging
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from .. import db
from ..events import audit

log = logging.getLogger("mcp")


def tool_hash(name: str, description: str, schema: dict) -> str:
    return hashlib.sha256(json.dumps({"name": name, "description": description or "", "inputSchema": schema or {}},
                                     sort_keys=True).encode()).hexdigest()


async def _secrets(mapping: dict) -> dict:
    """Map ENV_VAR/header -> credential provider name, decrypted at call time; never logged or shown to agents."""
    from ..security import decrypt
    out = {}
    for key, provider in (mapping or {}).items():
        row = await db.fetchrow("SELECT ciphertext, nonce FROM credentials WHERE provider=%s", provider)
        if row:
            out[key] = decrypt(row["ciphertext"], row["nonce"])
    return out


@asynccontextmanager
async def session(server: dict, context: dict | None = None):
    if server["transport"] == "stdio":
        env = {**get_default_environment(), "PYTHONPATH": "/app", "HOME": "/home/appuser",
               "ARTIFACT_DIR": "/data/artifacts", "INFRA_DIR": "/app/config/infra"}
        env.update(await _secrets(server.get("secret_env") or {}))
        if server.get("context_env") and context:
            env.update({f"MEMY_{k.upper()}": str(v) for k, v in context.items() if v is not None})
        params = StdioServerParameters(command=server["command"], args=list(server["args"] or []), env=env, cwd="/app")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                yield s
    else:
        from mcp.client.streamable_http import streamablehttp_client
        headers = await _secrets(server.get("headers") or {})
        async with streamablehttp_client(server["url"], headers=headers or None) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                yield s


async def probe(server: dict) -> dict:
    """Connect to a server definition (saved or not) and list its tools, without changing anything."""
    try:
        async with session(server, {"team_id": "probe", "project": "probe"}) as s:
            tools = _describe((await s.list_tools()).tools)
        return {"ok": True, "tools": [{k: t[k] for k in ("name", "description", "side_effect", "input_schema")} for t in tools]}
    except Exception as e:
        msg = str(e) or type(e).__name__
        if hasattr(e, "exceptions"):
            msg = "; ".join(str(x) for x in e.exceptions)
        return {"ok": False, "error": msg[:800]}


def _describe(tools) -> list[dict]:
    out = []
    for t in tools:
        ann = getattr(t, "annotations", None)
        read_only = bool(ann and getattr(ann, "readOnlyHint", False))
        out.append({"name": t.name, "description": t.description or "", "input_schema": t.inputSchema or {},
                    "hash": tool_hash(t.name, t.description or "", t.inputSchema or {}), "side_effect": not read_only})
    return out


async def refresh_server(server_id: str, auto_approve: bool = False) -> dict:
    """Re-list a server's tools. A changed hash disables that tool until an admin re-approves it."""
    server = await db.fetchrow("SELECT * FROM mcp_servers WHERE id=%s", server_id)
    if not server:
        return {"ok": False, "error": "unknown server"}
    try:
        async with session(server, {"team_id": "refresh", "project": "refresh"}) as s:
            tools = _describe((await s.list_tools()).tools)
    except Exception as e:
        log.warning("mcp refresh %s failed: %s", server_id, e)
        await db.execute("UPDATE mcp_servers SET last_error=%s, refreshed_at=now() WHERE id=%s", str(e)[:500], server_id)
        return {"ok": False, "error": str(e)}
    changed = []
    for t in tools:
        row = await db.fetchrow("SELECT pinned_hash, status FROM mcp_tools WHERE server_id=%s AND name=%s", server_id, t["name"])
        if row is None:
            pinned = t["hash"] if auto_approve else None
            await db.execute(
                "INSERT INTO mcp_tools(server_id, name, description, input_schema, current_hash, pinned_hash, status, side_effect)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", server_id, t["name"], t["description"], db.J(t["input_schema"]),
                t["hash"], pinned, "approved" if pinned else "needs_review", t["side_effect"])
        else:
            status = "approved" if row["pinned_hash"] == t["hash"] else "needs_review"
            if row["pinned_hash"] and status == "needs_review" and row["status"] == "approved":
                changed.append(t["name"])
            await db.execute(
                "UPDATE mcp_tools SET description=%s, input_schema=%s, current_hash=%s, status=%s, side_effect=%s,"
                " updated_at=now() WHERE server_id=%s AND name=%s", t["description"], db.J(t["input_schema"]), t["hash"],
                status, t["side_effect"], server_id, t["name"])
    await db.execute("UPDATE mcp_servers SET last_error=NULL, refreshed_at=now() WHERE id=%s", server_id)
    if changed:
        await audit(None, "mcp.tool_changed", server_id, {"tools": changed})
    return {"ok": True, "tools": len(tools), "changed": changed}


async def approve_tool(server_id: str, name: str, user_id) -> bool:
    n = await db.execute("UPDATE mcp_tools SET pinned_hash=current_hash, status='approved', updated_at=now()"
                         " WHERE server_id=%s AND name=%s", server_id, name)
    await audit(user_id, "mcp.tool_approved", f"{server_id}.{name}")
    return n > 0


async def call(server_id: str, name: str, args: dict, context: dict | None = None) -> tuple[bool, str]:
    """Call a pinned tool. The live description is re-hashed in the same session; a mismatch blocks the call."""
    server = await db.fetchrow("SELECT * FROM mcp_servers WHERE id=%s", server_id)
    pinned = await db.fetchrow("SELECT pinned_hash FROM mcp_tools WHERE server_id=%s AND name=%s", server_id, name)
    if not server or not pinned or not pinned["pinned_hash"]:
        return False, "tool is not approved"
    async with session(server, context) as s:
        live = {t["name"]: t for t in _describe((await s.list_tools()).tools)}
        t = live.get(name)
        if not t or t["hash"] != pinned["pinned_hash"]:
            await db.execute("UPDATE mcp_tools SET status='needs_review', current_hash=%s, description=%s, updated_at=now()"
                             " WHERE server_id=%s AND name=%s", t["hash"] if t else "missing",
                             t["description"] if t else "", server_id, name)
            await audit(None, "mcp.tool_blocked", f"{server_id}.{name}", {"reason": "description hash changed"})
            return False, "BLOCKED: tool description changed since approval; an admin must re-approve it"
        res = await s.call_tool(name, args)
        texts = []
        for c in res.content or []:
            texts.append(getattr(c, "text", None) or json.dumps(c.model_dump() if hasattr(c, "model_dump") else str(c)))
        return not getattr(res, "isError", False), "\n".join(texts)
