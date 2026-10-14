"""API: file uploads, knowledge provenance, media studio jobs, MCP management, project workspaces, teams."""
import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import artifacts, db, inputs, media
from ..config import settings
from ..events import audit
from ..gateway import UsageMeta, gateway
from ..tools import mcp_client, render_mermaid
from .deps import RANK, current_user, need, need_admin_any

log = logging.getLogger("api.extra")
router = APIRouter(prefix="/api")


# ---------------- uploads (task attachments) ----------------
@router.post("/uploads")
async def upload(team_id: str = Form(...), note: str = Form(""), file: UploadFile = File(...), user=Depends(current_user)):
    need(user, team_id, "member")
    data = await file.read()
    try:
        return await inputs.process_upload(team_id=team_id, user_id=user["id"], filename=file.filename or "upload",
                                           data=data, mime=file.content_type, note=note)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- knowledge provenance ----------------
@router.get("/tasks/{task_id}/knowledge")
async def task_knowledge(task_id: str, user=Depends(current_user)):
    t = await db.fetchrow("SELECT team_id FROM tasks WHERE id=%s", task_id)
    if not t:
        raise HTTPException(404)
    need(user, t["team_id"])
    return await db.fetch(
        "SELECT s.id, s.kind, s.title, s.url, s.status, s.trust_score, s.excerpt, s.agent_id, s.step_id, s.created_at,"
        " (SELECT count(*) FROM chunks c WHERE c.source_id=s.id)::int AS chunks FROM sources s WHERE s.task_id=%s"
        " ORDER BY s.created_at", task_id)


@router.get("/knowledge/sources/{source_id}")
async def source_detail(source_id: str, user=Depends(current_user)):
    s = await db.fetchrow("SELECT s.*, t.title AS task_title FROM sources s LEFT JOIN tasks t ON t.id=s.task_id WHERE s.id=%s",
                          source_id)
    if not s:
        raise HTTPException(404)
    need(user, s["team_id"])
    s = dict(s)
    s["chunks"] = await db.fetch("SELECT ord, content, embedding IS NOT NULL AS embedded FROM chunks WHERE source_id=%s"
                                 " ORDER BY ord", source_id)
    return s


# ---------------- media studio ----------------
JOB_KINDS = {"speech", "sound", "music", "image", "video", "diagram", "transcribe"}
_running_jobs: set[asyncio.Task] = set()


async def _job_progress(job_id, msg: str):
    await db.execute("UPDATE media_jobs SET progress=%s, updated_at=now() WHERE id=%s", msg[:300], job_id)


async def _run_job(job_id):
    job = await db.fetchrow("SELECT * FROM media_jobs WHERE id=%s", job_id)
    p, team, uid = job["params"], job["team_id"], job["user_id"]
    meta = UsageMeta(user_id=uid, team_id=team, agent_id="studio")
    await db.execute("UPDATE media_jobs SET status='running', updated_at=now() WHERE id=%s", job_id)

    async def progress(msg):
        await _job_progress(job_id, msg)

    def store(name, data, kind, mime, info):
        return media.store(team_id=team, task_id=None, step_id=None, user_id=uid, name=name, data=data, kind=kind,
                           mime=mime, info={**info, "studio_job": str(job_id)})
    try:
        k = job["kind"]
        if k == "speech":
            data, info = await media.speech(p["text"], p.get("voice"), p.get("model_id"), meta=meta)
            res = await store(p.get("name") or "speech", data, "audio", "audio/mpeg", {**info, "prompt": p["text"][:500]})
        elif k == "sound":
            data, info = await media.sound_effect(p["prompt"], p.get("duration_s"), meta=meta)
            res = await store(p.get("name") or "sound", data, "audio", "audio/mpeg", info)
        elif k == "music":
            data, info = await media.music(p["prompt"], int(p.get("length_s", 30)), meta=meta)
            res = await store(p.get("name") or "music", data, "audio", "audio/mpeg", info)
        elif k == "image":
            data, mime, info = await media.image(p["prompt"], p.get("model_id"), p.get("aspect_ratio", "1:1"), progress, meta)
            res = await store(p.get("name") or "image", data, "image", mime, info)
        elif k == "video":
            data, mime, info = await media.video(p["prompt"], p.get("model_id"), int(p.get("duration_secs", 4)),
                                                 progress=progress, meta=meta)
            res = await store(p.get("name") or "video", data, "video", mime, info)
        elif k == "diagram":
            r = await render_mermaid(p["source"])
            if not r.get("ok"):
                raise media.MediaError(f"Mermaid parse error: {r.get('error', '')[:600]}")
            res = await store((p.get("name") or "diagram") + ".svg", r["svg"].encode(), "diagram", "image/svg+xml",
                              {"source": p["source"][:4000]})
        elif k == "transcribe":
            row = await db.fetchrow("SELECT * FROM artifacts WHERE id=%s AND team_id=%s", p["artifact_id"], team)
            if not row:
                raise media.MediaError("audio file not found")
            data = Path(row["storage_path"]).read_bytes()
            if p.get("engine") == "elevenlabs":
                text = (await media.transcribe(data, row["name"], meta=meta))["text"]
            else:
                text = await gateway().transcribe(data, row["name"], meta=meta)
            res = await artifacts.save(team_id=team, task_id=None, step_id=None, name=f"{row['name']}.transcript.md",
                                       data=text, kind="markdown", meta={"source_artifact": str(row["id"]),
                                                                         "engine": p.get("engine", "whisper")},
                                       created_by=uid)
        else:
            raise media.MediaError(f"unknown job kind {k}")
        await db.execute("UPDATE media_jobs SET status='completed', artifact_id=%s, progress='done', updated_at=now()"
                         " WHERE id=%s", res["artifact_id"], job_id)
    except Exception as e:
        log.warning("media job %s failed: %s", job_id, e)
        await db.execute("UPDATE media_jobs SET status='failed', error=%s, updated_at=now() WHERE id=%s",
                         str(e).replace("PAID_PLAN_REQUIRED: ", "")[:600], job_id)


class JobBody(BaseModel):
    team_id: str
    kind: str
    params: dict


@router.post("/media/jobs")
async def create_job(body: JobBody, user=Depends(current_user)):
    need(user, body.team_id, "member")
    if body.kind not in JOB_KINDS:
        raise HTTPException(400, "unknown kind")
    jid = await db.fetchval("INSERT INTO media_jobs(team_id, user_id, kind, params) VALUES (%s,%s,%s,%s) RETURNING id",
                            body.team_id, user["id"], body.kind, db.J(body.params))
    t = asyncio.create_task(_run_job(jid))
    _running_jobs.add(t)
    t.add_done_callback(_running_jobs.discard)
    await audit(user["id"], f"media.{body.kind}", str(jid))
    return {"id": jid}


@router.get("/media/jobs")
async def list_jobs(team_id: str, user=Depends(current_user)):
    need(user, team_id)
    return await db.fetch(
        "SELECT j.*, u.name AS user_name, a.name AS artifact_name, a.mime AS artifact_mime, a.kind AS artifact_kind"
        " FROM media_jobs j JOIN users u ON u.id=j.user_id LEFT JOIN artifacts a ON a.id=j.artifact_id"
        " WHERE j.team_id=%s ORDER BY j.created_at DESC LIMIT 50", team_id)


@router.get("/media/gallery")
async def gallery(team_id: str, kind: str | None = None, user=Depends(current_user)):
    need(user, team_id)
    kinds = [kind] if kind else ["audio", "image", "video", "diagram"]
    return await db.fetch(
        "SELECT a.id, a.name, a.kind, a.mime, a.size, a.meta, a.role, a.task_id, a.created_at, t.title AS task_title,"
        " u.name AS created_by_name FROM artifacts a LEFT JOIN tasks t ON t.id=a.task_id LEFT JOIN users u ON u.id=a.created_by"
        " WHERE a.team_id=%s AND a.kind = ANY(%s) ORDER BY a.created_at DESC LIMIT 120", team_id, kinds)


@router.get("/media/status")
async def media_status(user=Depends(current_user)):
    try:
        sub = await media.subscription(force=True)
        return {"configured": True, **sub, "image_models": media.IMAGE_MODELS, "video_models": media.VIDEO_MODELS,
                "paid_features": ["image", "video", "music"], "free_features": ["speech", "sound", "transcribe"]}
    except media.MediaError as e:
        return {"configured": False, "error": str(e)}


@router.get("/media/voices")
async def media_voices(user=Depends(current_user)):
    try:
        return await media.voices()
    except media.MediaError as e:
        raise HTTPException(400, str(e))


async def fail_interrupted_jobs():
    await db.execute("UPDATE media_jobs SET status='failed', error='interrupted by a server restart'"
                     " WHERE status IN ('queued','running')")


# ---------------- MCP management ----------------
class McpProbe(BaseModel):
    transport: str
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    secret_env: dict = {}
    headers: dict = {}


@router.post("/mcp/probe")
async def mcp_probe(body: McpProbe, user=Depends(current_user)):
    need_admin_any(user)
    return await mcp_client.probe({**body.model_dump(), "context_env": False})


class McpSecrets(BaseModel):
    secret_env: dict = {}
    headers: dict = {}
    description: str | None = None


@router.patch("/mcp/servers/{server_id}")
async def mcp_update(server_id: str, body: McpSecrets, user=Depends(current_user)):
    need_admin_any(user)
    await db.execute("UPDATE mcp_servers SET secret_env=%s, headers=%s, description=coalesce(%s, description) WHERE id=%s",
                     db.J(body.secret_env), db.J(body.headers), body.description, server_id)
    await audit(user["id"], "mcp.server_updated", server_id, {"secret_env": list(body.secret_env), "headers": list(body.headers)})
    return await mcp_client.refresh_server(server_id)


@router.post("/mcp/servers/{server_id}/status")
async def mcp_status(server_id: str, active: bool, user=Depends(current_user)):
    need_admin_any(user)
    await db.execute("UPDATE mcp_servers SET status=%s WHERE id=%s", "active" if active else "disabled", server_id)
    await audit(user["id"], "mcp.server_enabled" if active else "mcp.server_disabled", server_id)
    return {"ok": True}


@router.delete("/mcp/servers/{server_id}")
async def mcp_delete(server_id: str, user=Depends(current_user)):
    need_admin_any(user)
    await db.execute("DELETE FROM mcp_servers WHERE id=%s", server_id)
    await db.execute("DELETE FROM tool_grants WHERE tool LIKE %s", f"mcp.{server_id}.%")
    await audit(user["id"], "mcp.server_removed", server_id)
    return {"ok": True}


# ---------------- project workspaces ----------------
def _ws_root(team_id: str, project: str | None = None) -> Path:
    base = Path(settings.artifact_dir) / "workspace" / str(team_id)
    if project is None:
        return base
    clean = "".join(c for c in project if c.isalnum() or c in "-_") or "default"
    return base / clean


@router.get("/workspace/projects")
async def ws_projects(team_id: str, user=Depends(current_user)):
    need(user, team_id)
    base = _ws_root(team_id)
    if not base.exists():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        files = [f for f in d.rglob("*") if f.is_file()]
        out.append({"project": d.name, "files": len(files), "bytes": sum(f.stat().st_size for f in files),
                    "updated": max((f.stat().st_mtime for f in files), default=d.stat().st_mtime)})
    return out


@router.get("/workspace/files")
async def ws_files(team_id: str, project: str, user=Depends(current_user)):
    need(user, team_id)
    root = _ws_root(team_id, project)
    if not root.exists():
        return []
    return [{"path": str(f.relative_to(root)).replace("\\", "/"), "size": f.stat().st_size, "modified": f.stat().st_mtime}
            for f in sorted(root.rglob("*")) if f.is_file()]


@router.get("/workspace/file")
async def ws_file(team_id: str, project: str, path: str, user=Depends(current_user)):
    need(user, team_id)
    root = _ws_root(team_id, project).resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404)
    return {"path": path, "content": p.read_text(errors="replace")[:200000]}


# ---------------- teams ----------------
class TeamBody(BaseModel):
    name: str
    slug: str
    budget_usd_per_task: float = 2.0
    budget_tokens_per_task: int = 400000


@router.post("/teams")
async def create_team(body: TeamBody, user=Depends(current_user)):
    if not user["is_owner"]:
        raise HTTPException(403, "only the organisation owner creates teams")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", body.slug):
        raise HTTPException(400, "slug must be lowercase letters, digits and dashes")
    org = await db.fetchval("SELECT id FROM orgs LIMIT 1")
    tid = await db.fetchval("INSERT INTO teams(org_id, name, slug, budget_usd_per_task, budget_tokens_per_task)"
                            " VALUES (%s,%s,%s,%s,%s) RETURNING id", org, body.name, body.slug, body.budget_usd_per_task,
                            body.budget_tokens_per_task)
    from ..seed import default_grants
    await default_grants(tid)
    await audit(user["id"], "team.created", str(tid), body.model_dump())
    return {"id": tid}


@router.get("/teams/{team_id}/members")
async def team_members(team_id: str, user=Depends(current_user)):
    need(user, team_id)
    return await db.fetch("SELECT u.id, u.name, u.email, m.role, u.is_owner FROM memberships m JOIN users u ON u.id=m.user_id"
                          " WHERE m.team_id=%s ORDER BY array_position(ARRAY['owner','admin','member','viewer'], m.role), u.name",
                          team_id)


@router.get("/access")
async def access_matrix(user=Depends(current_user)):
    """Who can do what, per team: the RBAC matrix rendered on the Admin page."""
    teams = await db.fetch("SELECT id, name, slug FROM teams WHERE id::text = ANY(%s) ORDER BY name", list(user["roles"]))
    rows = await db.fetch("SELECT u.id, u.name, u.email, u.is_owner, m.team_id, m.role FROM users u"
                          " LEFT JOIN memberships m ON m.user_id=u.id ORDER BY u.created_at")
    users: dict = {}
    for r in rows:
        u = users.setdefault(str(r["id"]), {"id": str(r["id"]), "name": r["name"], "email": r["email"],
                                            "is_owner": r["is_owner"], "roles": {}})
        if r["team_id"]:
            u["roles"][str(r["team_id"])] = r["role"]
    capabilities = {
        "viewer": ["read tasks, results, knowledge and dashboards"],
        "member": ["create tasks and schedules", "attach files", "add team knowledge", "approve actions on own tasks",
                   "use the media studio"],
        "admin": ["edit agents, skills and MCP servers", "grant tools", "approve any team action", "manage members",
                  "kill switch for the team"],
        "owner": ["provider keys", "create teams", "everything"],
    }
    return {"teams": teams, "users": list(users.values()), "capabilities": capabilities, "rank": RANK}
