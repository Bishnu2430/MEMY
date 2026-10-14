"""Idempotent seed: org, two teams, test users, agents, skills, MCP servers, grants, instructions."""
import logging

from . import db
from .agents import index_skills, refresh_missing_cards, seed_from_files
from .config import settings
from .security import hash_password
from .tools import mcp_client

log = logging.getLogger("seed")

MCP_SERVERS = [
    ("time", "Time", "stdio", "python", ["-m", "mcp_server_time", "--local-timezone", "UTC"]),
    ("notes", "Team notes (demo)", "stdio", "python", ["-m", "app.mcp_demo_server"]),
]


async def seed():
    org = await db.fetchval("SELECT id FROM orgs LIMIT 1")
    if not org:
        org = await db.fetchval("INSERT INTO orgs(name) VALUES ('Default Org') RETURNING id")
    teams = {}
    for slug, name in (("platform", "Platform"), ("research", "Research")):
        tid = await db.fetchval("SELECT id FROM teams WHERE slug=%s", slug)
        if not tid:
            tid = await db.fetchval("INSERT INTO teams(org_id, name, slug) VALUES (%s,%s,%s) RETURNING id", org, name, slug)
        teams[slug] = tid
    memberships = {"admin": [("platform", "owner"), ("research", "owner")],
                   "alice": [("platform", "member")],
                   "bob": [("research", "member")]}
    admin_id = None
    for key, email, pw in settings.seed_users:
        if not pw:
            continue
        uid = await db.fetchval("SELECT id FROM users WHERE email=%s", email)
        if not uid:
            uid = await db.fetchval("INSERT INTO users(org_id, email, name, password_hash, is_owner) VALUES (%s,%s,%s,%s,%s)"
                                    " RETURNING id", org, email, key.title(), hash_password(pw), key == "admin")
        if key == "admin":
            admin_id = uid
        for slug, role in memberships[key]:
            await db.execute("INSERT INTO memberships(user_id, team_id, role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                             uid, teams[slug], role)

    if not await db.fetchval("SELECT count(*) FROM instructions"):
        await db.execute("INSERT INTO instructions(scope_type, scope_id, version, body) VALUES ('org','default',1,%s)",
                         "Be accurate and concise. Prefer verified facts over speculation and say when you are unsure.")
        await db.execute("INSERT INTO instructions(scope_type, scope_id, version, body) VALUES ('team',%s,1,%s)",
                         str(teams["platform"]), "Platform team: Python 3.12, type hints, pytest. Follow PEP 8.")
        await db.execute("INSERT INTO instructions(scope_type, scope_id, version, body) VALUES ('team',%s,1,%s)",
                         str(teams["research"]), "Research team: always cite sources with URLs; flag uncertainty.")

    await index_skills(auto_approve_new=True)
    await seed_from_files(admin_id)

    for sid, name, transport, command, args in MCP_SERVERS:
        exists = await db.fetchval("SELECT 1 FROM mcp_servers WHERE id=%s", sid)
        if not exists:
            await db.execute("INSERT INTO mcp_servers(id, name, transport, command, args) VALUES (%s,%s,%s,%s,%s)",
                             sid, name, transport, command, db.J(args))
            res = await mcp_client.refresh_server(sid, auto_approve=True)
            log.info("mcp server %s: %s", sid, res)
    for slug in ("platform", "research"):
        for tool, needs in (("mcp.time.*", False), ("mcp.notes.list_notes", False), ("mcp.notes.add_note", True)):
            await db.execute("INSERT INTO tool_grants(team_id, tool, needs_approval) VALUES (%s,%s,%s)"
                             " ON CONFLICT DO NOTHING", teams[slug], tool, needs)
    try:
        await refresh_missing_cards()
    except Exception as e:
        log.warning("capability cards not embedded yet: %s", e)
