"""AgentSpec registry: versioned configuration records run by one generic executor."""
import hashlib
import logging
from pathlib import Path

import jsonschema
import yaml

from . import db
from .config import settings
from .events import audit
from .gateway import gateway

log = logging.getLogger("agents")

SPEC_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "description", "model"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{1,40}$"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "mastery": {"type": "array", "items": {"type": "string"}},
        "model": {"type": "object", "required": ["primary"], "properties": {
            "primary": {"type": "string"}, "fallback": {"type": "string"}, "temperature": {"type": "number"}}},
        "instructions": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "object", "properties": {
            "builtin": {"type": "array", "items": {"type": "string"}},
            "mcp": {"type": "array", "items": {"type": "string"}}}},
        "knowledge_scopes": {"type": "array", "items": {"type": "string"}},
        "limits": {"type": "object", "properties": {
            "max_steps": {"type": "integer"}, "max_cost_usd": {"type": "number"}, "timeout_s": {"type": "integer"},
            "max_output_tokens": {"type": "integer"}}},
        "approval_required": {"type": "array", "items": {"type": "string"}},
        "verification": {"enum": ["none", "light", "code-default", "research", "diagram", "media", "strict"]},
        "examples": {"type": "array", "items": {"type": "string"}},
        "team": {"type": ["string", "null"]},
    },
}


def parse_spec(yaml_text: str) -> dict:
    spec = yaml.safe_load(yaml_text)
    if not isinstance(spec, dict):
        raise ValueError("spec must be a YAML mapping")
    spec.pop("version", None)
    jsonschema.validate(spec, SPEC_SCHEMA)
    spec.setdefault("mastery", [])
    spec.setdefault("skills", [])
    spec.setdefault("tools", {})
    spec["tools"].setdefault("builtin", [])
    spec["tools"].setdefault("mcp", [])
    spec.setdefault("knowledge_scopes", ["team"])
    spec.setdefault("limits", {})
    spec["limits"].setdefault("max_steps", 10)
    spec["limits"].setdefault("max_cost_usd", 1.0)
    spec["limits"].setdefault("timeout_s", settings.step_wall_time_s)
    spec.setdefault("approval_required", [])
    spec.setdefault("verification", "light")
    spec.setdefault("examples", [])
    spec.setdefault("instructions", "")
    return spec


async def get_spec(agent_id: str, version: int | None = None) -> dict | None:
    if version is None:
        row = await db.fetchrow(
            "SELECT v.spec, v.version FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version"
            " WHERE a.id=%s", agent_id)
    else:
        row = await db.fetchrow("SELECT spec, version FROM agent_versions WHERE agent_id=%s AND version=%s", agent_id, version)
    if not row:
        return None
    return {**row["spec"], "version": row["version"]}


async def list_agents(team_ids: list | None = None, include_disabled: bool = False) -> list[dict]:
    rows = await db.fetch(
        "SELECT a.id, a.team_id, a.enabled, a.current_version, a.cloned_from, v.spec, a.updated_at FROM agents a"
        " JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version ORDER BY a.id")
    out = []
    allowed = {str(t) for t in team_ids} if team_ids is not None else None
    for r in rows:
        if not include_disabled and not r["enabled"]:
            continue
        if allowed is not None and r["team_id"] is not None and str(r["team_id"]) not in allowed:
            continue
        out.append({"id": r["id"], "team_id": r["team_id"], "enabled": r["enabled"], "version": r["current_version"],
                    "cloned_from": r["cloned_from"], "updated_at": r["updated_at"], **{k: r["spec"].get(k) for k in (
                        "name", "description", "mastery", "model", "verification", "tools", "skills", "examples")}})
    return out


async def save_version(yaml_text: str, user_id=None, *, team_id=None, cloned_from=None, expected_id=None) -> dict:
    spec = parse_spec(yaml_text)
    if expected_id and spec["id"] != expected_id:
        raise ValueError("spec id does not match the agent being edited")
    async with db.tx() as conn:
        cur = await conn.execute("SELECT current_version FROM agents WHERE id=%s FOR UPDATE", (spec["id"],))
        row = await cur.fetchone()
        if row:
            version = row["current_version"] + 1
            cur = await conn.execute("SELECT max(version) AS m FROM agent_versions WHERE agent_id=%s", (spec["id"],))
            version = max(version, (await cur.fetchone())["m"] + 1)
            await conn.execute("UPDATE agents SET current_version=%s, updated_at=now() WHERE id=%s", (version, spec["id"]))
        else:
            version = 1
            await conn.execute("INSERT INTO agents(id, team_id, current_version, cloned_from) VALUES (%s,%s,1,%s)",
                               (spec["id"], team_id, cloned_from))
        await conn.execute("INSERT INTO agent_versions(agent_id, version, spec, yaml, created_by) VALUES (%s,%s,%s,%s,%s)",
                           (spec["id"], version, db.J(spec), yaml_text, user_id))
    await audit(user_id, "agent.save", spec["id"], {"version": version})
    await refresh_card(spec["id"])
    return {"id": spec["id"], "version": version}


async def rollback(agent_id: str, version: int, user_id=None) -> dict:
    row = await db.fetchrow("SELECT yaml FROM agent_versions WHERE agent_id=%s AND version=%s", agent_id, version)
    if not row:
        raise ValueError("version not found")
    res = await save_version(row["yaml"], user_id, expected_id=agent_id)
    await audit(user_id, "agent.rollback", agent_id, {"to": version, "new_version": res["version"]})
    return res


async def card_text(spec: dict) -> str:
    skills = await db.fetch("SELECT name, description FROM skills WHERE name = ANY(%s)", spec.get("skills", []))
    parts = [f"{spec['name']}: {spec['description']}",
             "Mastery: " + ", ".join(spec.get("mastery", [])),
             "Skills: " + "; ".join(f"{s['name']} - {s['description']}" for s in skills),
             "Example tasks: " + " | ".join(spec.get("examples", []))]
    return "\n".join(parts)


async def refresh_card(agent_id: str):
    spec = await get_spec(agent_id)
    if not spec:
        return
    text = await card_text(spec)
    try:
        emb = (await gateway().embed([text], kind="document"))[0]
        vec = "[" + ",".join(f"{x:.6f}" for x in emb) + "]"
    except Exception as e:  # embeddings not ready yet; router falls back to keyword shortlist
        log.warning("card embedding failed for %s: %s", agent_id, e)
        vec = None
    await db.execute(
        "INSERT INTO agent_cards(agent_id, version, card_text, embedding) VALUES (%s,%s,%s,%s::vector)"
        " ON CONFLICT (agent_id) DO UPDATE SET version=EXCLUDED.version, card_text=EXCLUDED.card_text,"
        " embedding=EXCLUDED.embedding", agent_id, spec["version"], text, vec)


async def seed_from_files(user_id=None):
    for path in sorted(Path(settings.agents_dir).glob("*.yaml")):
        text = path.read_text()
        try:
            spec = parse_spec(text)
        except Exception as e:
            log.error("invalid agent spec %s: %s", path, e)
            continue
        cur = await db.fetchrow(
            "SELECT v.yaml FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version"
            " WHERE a.id=%s", spec["id"])
        team_id = None
        if spec.get("team"):
            team_id = await db.fetchval("SELECT id FROM teams WHERE slug=%s", spec["team"])
        file_hash = hashlib.sha256(text.encode()).hexdigest()
        seed_hash = await db.fetchval("SELECT seed_hash FROM agents WHERE id=%s", spec["id"])
        stale, file_changed = False, False
        if cur is not None:
            old = parse_spec(cur["yaml"])
            stale = any(a and a not in gateway().models for a in (old["model"]["primary"], old["model"].get("fallback")))
            # A changed seed file updates the agent only if nobody edited it in the UI since the last seed.
            cur_hash = hashlib.sha256(cur["yaml"].encode()).hexdigest()
            file_changed = file_hash != cur_hash and (seed_hash is None or seed_hash == cur_hash)
        if cur is None or stale or file_changed:
            await save_version(text, user_id, team_id=team_id)
            log.info("seeded agent %s%s", spec["id"], " (updated from file)" if cur is not None else "")
        await db.execute("UPDATE agents SET seed_hash=%s WHERE id=%s", file_hash, spec["id"])


async def refresh_missing_cards():
    for r in await db.fetch("SELECT a.id FROM agents a LEFT JOIN agent_cards c ON c.agent_id=a.id"
                            " WHERE c.agent_id IS NULL OR c.embedding IS NULL OR c.version <> a.current_version"):
        await refresh_card(r["id"])


# ---------------- skills ----------------
def _skill_meta(md: str) -> dict:
    meta = {}
    if md.startswith("---"):
        end = md.find("---", 3)
        if end > 0:
            meta = yaml.safe_load(md[3:end]) or {}
    return meta


async def index_skills(auto_approve_new: bool = True):
    """Index skill folders. New skills seeded by the system are approved; any later edit
    changes the content hash and disables the skill until an admin re-approves it."""
    for d in sorted(Path(settings.skills_dir).glob("*/SKILL.md")):
        md = d.read_text()
        meta = _skill_meta(md)
        name = meta.get("name") or d.parent.name
        h = hashlib.sha256(md.encode()).hexdigest()
        row = await db.fetchrow("SELECT approved_hash FROM skills WHERE name=%s", name)
        if row is None:
            approved = h if auto_approve_new else None
            await db.execute("INSERT INTO skills(name, description, storage_path, content_hash, approved_hash, enabled)"
                             " VALUES (%s,%s,%s,%s,%s,%s)", name, meta.get("description", ""), str(d), h, approved,
                             approved == h)
        else:
            await db.execute("UPDATE skills SET description=%s, storage_path=%s, content_hash=%s,"
                             " enabled=(approved_hash=%s), updated_at=now() WHERE name=%s",
                             meta.get("description", ""), str(d), h, h, name)


async def load_skill(name: str) -> str | None:
    row = await db.fetchrow("SELECT storage_path, content_hash, approved_hash, enabled FROM skills WHERE name=%s", name)
    if not row or not row["enabled"]:
        return None
    md = Path(row["storage_path"]).read_text()
    if hashlib.sha256(md.encode()).hexdigest() != row["approved_hash"]:
        await db.execute("UPDATE skills SET enabled=false WHERE name=%s", name)
        return None
    return md
