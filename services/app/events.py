"""Append-only event log. Every message, tool call, routing decision and cost lands here."""
import json
import logging

from . import db

log = logging.getLogger("events")


def _clip(value, limit=4000):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [{len(value) - limit} more chars]"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:50]]
    return value


async def emit(type_: str, *, task_id=None, step_id=None, team_id=None, actor: str = "system",
               payload: dict | None = None) -> int:
    payload = json.loads(json.dumps(_clip(payload or {}), default=str))
    if team_id is None and task_id is not None:
        team_id = await db.fetchval("SELECT team_id FROM tasks WHERE id=%s", task_id)
    eid = await db.fetchval(
        "INSERT INTO events(task_id, step_id, team_id, type, actor, payload) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        task_id, step_id, team_id, type_, actor, db.J(payload))
    if step_id is not None:
        await db.execute("UPDATE task_steps SET last_event_at=now() WHERE id=%s", step_id)
    return eid


async def audit(actor, action: str, target: str = "", detail: dict | None = None):
    await db.execute("INSERT INTO audit_log(actor, action, target, detail) VALUES (%s,%s,%s,%s)",
                     actor, action, target, db.J(json.loads(json.dumps(detail or {}, default=str))))
