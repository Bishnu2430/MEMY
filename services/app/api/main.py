"""Master node API: REST + SSE for the web dashboard and the terminal client."""
import asyncio
import uuid
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import jsonschema
import yaml
from croniter import croniter
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import agents as agent_reg
from .. import artifacts, db, knowledge, scraping
from ..events import audit, emit
from ..gateway import UsageMeta, gateway
from ..router import route
from ..runtime import checkpointer
from ..security import decode_token, encrypt, make_token, verify_password, hash_password
from ..seed import seed
from ..tasks import TaskError, create_task, start_single, task_cost
from ..tools import BUILTINS, mcp_client
from . import extra
from .deps import RANK, current_user, need, need_admin_any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("api")


async def _card_refresher():
    while True:
        await asyncio.sleep(45)
        try:
            missing = await db.fetchval("SELECT count(*) FROM agents a LEFT JOIN agent_cards c ON c.agent_id=a.id"
                                        " WHERE c.embedding IS NULL OR c.version <> a.current_version")
            if missing:
                await agent_reg.refresh_missing_cards()
            await agent_reg.index_skills(auto_approve_new=False)
        except Exception as e:
            log.info("card refresh pending: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool(max_size=15)
    await db.migrate()
    await checkpointer(setup=True)
    gateway()
    await seed()
    await extra.fail_interrupted_jobs()
    task = asyncio.create_task(_card_refresher())
    yield
    task.cancel()


app = FastAPI(title="Memy Orchestration Platform", lifespan=lifespan)
app.include_router(extra.router)


@app.exception_handler(jsonschema.ValidationError)
async def _schema_error(request: Request, exc: jsonschema.ValidationError):
    return JSONResponse(status_code=400, content={"detail": _spec_error(exc)})


@app.exception_handler(yaml.YAMLError)
async def _yaml_error(request: Request, exc: yaml.YAMLError):
    return JSONResponse(status_code=400, content={"detail": _spec_error(exc)})


@app.exception_handler(TaskError)
async def _task_error(request: Request, exc: TaskError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------- auth ----------------
class Login(BaseModel):
    email: str
    password: str


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/auth/login")
async def login(body: Login):
    u = await db.fetchrow("SELECT id, password_hash, active FROM users WHERE email=%s", body.email.strip().lower())
    if not u or not u["active"] or not verify_password(body.password, u["password_hash"]):
        await audit(None, "auth.login_failed", body.email)
        raise HTTPException(401, "invalid credentials")
    await audit(u["id"], "auth.login", body.email)
    return {"token": make_token(u["id"])}


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return user


# ---------------- tasks ----------------
class NewTask(BaseModel):
    team_id: str
    text: str
    project: str | None = None
    agent_id: str | None = None
    private: bool = False
    budget_usd: float | None = None
    attachment_ids: list[str] = []


async def _get_task(task_id: str, user: dict) -> dict:
    t = await db.fetchrow("SELECT * FROM tasks WHERE id=%s", task_id)
    if not t:
        raise HTTPException(404, "task not found")
    role = need(user, t["team_id"])
    if t["private"] and t["created_by"] != user["id"] and RANK[role] < RANK["admin"]:
        raise HTTPException(404, "task not found")
    return dict(t)


@app.post("/api/tasks")
async def new_task(body: NewTask, user=Depends(current_user)):
    need(user, body.team_id, "member")
    try:
        return await create_task(team_id=body.team_id, user_id=user["id"], text=body.text, project=body.project,
                                 agent_id=body.agent_id, private=body.private, budget_usd=body.budget_usd,
                                 attachment_ids=body.attachment_ids)
    except TaskError as e:
        raise HTTPException(400, str(e))


@app.get("/api/tasks")
async def list_tasks(team_id: str | None = None, limit: int = 50, user=Depends(current_user)):
    teams = [team_id] if team_id else list(user["roles"])
    for t in teams:
        need(user, t)
    return await db.fetch(
        "SELECT t.id, t.team_id, t.title, t.status, t.mode, t.requested_agent, t.created_at, t.completed_at, t.created_by,"
        " u.name AS created_by_name, (SELECT coalesce(sum(cost_usd),0)::float FROM usage_records WHERE task_id=t.id) AS cost_usd,"
        " (SELECT count(*) FROM task_steps WHERE task_id=t.id)::int AS steps,"
        " (SELECT count(*) FROM task_steps WHERE task_id=t.id AND status='done')::int AS steps_done"
        " FROM tasks t JOIN users u ON u.id=t.created_by WHERE t.team_id::text = ANY(%s) AND (NOT t.private OR t.created_by=%s)"
        " ORDER BY t.created_at DESC LIMIT %s", teams, user["id"], min(limit, 200))


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, user=Depends(current_user)):
    t = await _get_task(task_id, user)
    t["steps"] = await db.fetch("SELECT * FROM task_steps WHERE task_id=%s ORDER BY ord", task_id)
    t["approvals"] = await db.fetch("SELECT * FROM approvals WHERE task_id=%s ORDER BY requested_at", task_id)
    t["artifacts"] = await db.fetch("SELECT id, step_id, name, kind, mime, size, meta, role, created_at FROM artifacts"
                                    " WHERE task_id=%s ORDER BY created_at", task_id)
    t["knowledge_count"] = await db.fetchval("SELECT count(*) FROM sources WHERE task_id=%s", task_id)
    t["cost"] = await task_cost(task_id)
    t["step_costs"] = {str(r["step_id"]): r for r in await db.fetch(
        "SELECT step_id, coalesce(sum(cost_usd),0)::float AS cost_usd, coalesce(sum(tokens_in+tokens_out),0)::int AS tokens"
        " FROM usage_records WHERE task_id=%s GROUP BY step_id", task_id)}
    return t


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, after: int = 0, user=Depends(current_user)):
    await _get_task(task_id, user)
    return await db.fetch("SELECT * FROM events WHERE task_id=%s AND id > %s ORDER BY id LIMIT 1000", task_id, after)


def _sse(rows) -> str:
    return "".join(f"id: {r['id']}\nevent: message\ndata: {json.dumps(r, default=str)}\n\n" for r in rows)


async def _stream(request: Request, query: str, params: list, after: int):
    last, idle = after, 0
    while not await request.is_disconnected():
        rows = await db.fetch(query, *params, last)
        if rows:
            last = rows[-1]["id"]
            idle = 0
            yield _sse(rows)
        else:
            idle += 1
            if idle % 20 == 0:
                yield ": ping\n\n"
        await asyncio.sleep(0.7)


@app.get("/api/tasks/{task_id}/stream")
async def task_stream(task_id: str, request: Request, after: int = 0, user=Depends(current_user)):
    await _get_task(task_id, user)
    q = "SELECT * FROM events WHERE task_id=%s AND id > %s ORDER BY id LIMIT 200"
    return StreamingResponse(_stream(request, q, [task_id], after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stream")
async def team_stream(request: Request, after: int = -1, user=Depends(current_user)):
    teams = list(user["roles"])
    if after < 0:
        after = await db.fetchval("SELECT coalesce(max(id),0) FROM events") or 0
    q = "SELECT * FROM events WHERE team_id::text = ANY(%s) AND id > %s ORDER BY id LIMIT 200"
    return StreamingResponse(_stream(request, q, [teams], after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Choose(BaseModel):
    agent_id: str


@app.post("/api/tasks/{task_id}/choose")
async def choose_agent(task_id: str, body: Choose, user=Depends(current_user)):
    t = await _get_task(task_id, user)
    need(user, t["team_id"], "member")
    if t["status"] != "awaiting_user":
        raise HTTPException(400, "task is not waiting for a routing choice")
    picked = (t.get("router") or {}).get("agent_id")
    await db.execute("UPDATE routing_log SET overridden_to=%s WHERE id=(SELECT max(id) FROM routing_log WHERE task_id=%s)",
                     body.agent_id if body.agent_id != picked else None, task_id)
    await emit("routing_user_choice", task_id=task_id, actor=str(user["id"]),
               payload={"chosen": body.agent_id, "router_pick": picked})
    await start_single(t, body.agent_id, how="user_choice")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user=Depends(current_user)):
    t = await _get_task(task_id, user)
    if t["created_by"] != user["id"]:
        need(user, t["team_id"], "admin")
    if t["status"] in TERMINAL:
        raise HTTPException(400, f"task is already {t['status']}")
    await db.execute("UPDATE tasks SET status='cancelled', error=%s, updated_at=now() WHERE id=%s",
                     f"cancelled by {user['name']}", task_id)
    # Running steps are flagged too; their worker sees the cancellation within ~3 s and stops.
    await db.execute("UPDATE task_steps SET status='cancelled' WHERE task_id=%s AND status NOT IN ('done','escalated')", task_id)
    await db.execute("UPDATE approvals SET status='rejected', decided_by=%s, decided_at=now() WHERE task_id=%s"
                     " AND status='pending'", user["id"], task_id)
    await emit("task_cancelled", task_id=task_id, actor=str(user["id"]))
    return {"ok": True}


TERMINAL = ("completed", "cancelled", "failed")


@app.post("/api/tasks/{task_id}/rerun")
async def rerun_task(task_id: str, user=Depends(current_user)):
    """Start a fresh task with the same request, project and attached files."""
    t = await _get_task(task_id, user)
    need(user, t["team_id"], "member")
    ids = []
    for a in await db.fetch("SELECT * FROM artifacts WHERE task_id=%s AND role='input' ORDER BY created_at", task_id):
        nid = await db.fetchval(
            "INSERT INTO artifacts(task_id, step_id, team_id, name, kind, mime, storage_path, content_hash, size, meta,"
            " created_by, role, extracted) VALUES (NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,'input',%s) RETURNING id",
            a["team_id"], a["name"], a["kind"], a["mime"], a["storage_path"], a["content_hash"], a["size"],
            db.J(a["meta"] or {}), user["id"], a["extracted"])
        ids.append(str(nid))
    try:
        return await create_task(team_id=t["team_id"], user_id=user["id"], text=t["request"], project=t["project"],
                                 agent_id=t["requested_agent"], private=t["private"], attachment_ids=ids)
    except TaskError as e:
        raise HTTPException(400, str(e))


class Budget(BaseModel):
    budget_usd: float | None = None
    budget_tokens: int | None = None


@app.post("/api/tasks/{task_id}/budget")
async def raise_budget(task_id: str, body: Budget, user=Depends(current_user)):
    t = await _get_task(task_id, user)
    if t["created_by"] != user["id"]:
        need(user, t["team_id"], "admin")
    await db.execute("UPDATE tasks SET budget_usd=coalesce(%s, budget_usd), budget_tokens=coalesce(%s, budget_tokens),"
                     " status=CASE WHEN status='paused_budget' THEN 'running' ELSE status END, updated_at=now() WHERE id=%s",
                     body.budget_usd, body.budget_tokens, task_id)
    await emit("budget_changed", task_id=task_id, actor=str(user["id"]), payload=body.model_dump())
    return {"ok": True}


class StepAction(BaseModel):
    action: str  # retry | reassign | complete
    agent_id: str | None = None
    output: str | None = None


@app.post("/api/steps/{step_id}/action")
async def step_action(step_id: str, body: StepAction, user=Depends(current_user)):
    s = await db.fetchrow("SELECT * FROM task_steps WHERE id=%s", step_id)
    if not s:
        raise HTTPException(404)
    t = await _get_task(str(s["task_id"]), user)
    need(user, t["team_id"], "member")
    if body.action == "retry":
        await db.execute("UPDATE task_steps SET status='ready', attempts=attempts+1, thread_id=%s, locked_by=NULL,"
                         " correction=coalesce(%s, correction) WHERE id=%s", f"{step_id}:{uuid.uuid4().hex[:10]}", body.output, step_id)
    elif body.action == "reassign":
        spec = await agent_reg.get_spec(body.agent_id or "")
        if not spec:
            raise HTTPException(400, "unknown agent")
        await db.execute("UPDATE task_steps SET status='ready', agent_id=%s, agent_version=%s, attempts=0, thread_id=%s,"
                         " agents_tried=array_append(agents_tried, %s), locked_by=NULL WHERE id=%s",
                         body.agent_id, spec["version"], f"{step_id}:{uuid.uuid4().hex[:10]}",
                         body.agent_id, step_id)
    elif body.action == "complete":
        await db.execute("UPDATE task_steps SET status='done', output=%s, finished_at=now(), locked_by=NULL WHERE id=%s",
                         body.output or "(completed by a person)", step_id)
    else:
        raise HTTPException(400, "unknown action")
    await db.execute("UPDATE tasks SET status='running', error=NULL WHERE id=%s AND status IN ('needs_attention','failed')",
                     t["id"])
    await emit(f"human_{body.action}", task_id=t["id"], step_id=step_id, actor=str(user["id"]), payload=body.model_dump())
    await audit(user["id"], f"step.{body.action}", step_id)
    return {"ok": True}


# ---------------- approvals ----------------
@app.get("/api/approvals")
async def list_approvals(status: str = "pending", user=Depends(current_user)):
    return await db.fetch(
        "SELECT a.*, t.title AS task_title, s.agent_id, s.title AS step_title FROM approvals a JOIN tasks t ON t.id=a.task_id"
        " JOIN task_steps s ON s.id=a.step_id WHERE a.team_id::text = ANY(%s) AND a.status=%s ORDER BY a.requested_at DESC"
        " LIMIT 100", list(user["roles"]), status)


class Decision(BaseModel):
    decision: str  # approve | reject


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: Decision, user=Depends(current_user)):
    a = await db.fetchrow("SELECT a.*, t.created_by FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=%s", approval_id)
    if not a or a["status"] != "pending":
        raise HTTPException(404, "no pending approval")
    need(user, a["team_id"], "member" if a["created_by"] == user["id"] else "admin")
    status = "approved" if body.decision == "approve" else "rejected"
    await db.execute("UPDATE approvals SET status=%s, decided_by=%s, decided_at=now() WHERE id=%s", status, user["id"], approval_id)
    await emit("approval_decided", task_id=a["task_id"], step_id=a["step_id"], actor=str(user["id"]),
               payload={"approval_id": approval_id, "tool": a["tool"], "decision": status})
    await audit(user["id"], f"approval.{status}", approval_id, {"tool": a["tool"], "args": a["args"]})
    pending = await db.fetchval("SELECT count(*) FROM approvals WHERE step_id=%s AND status='pending'", a["step_id"])
    if not pending:
        await db.execute("UPDATE task_steps SET status='ready' WHERE id=%s AND status='waiting_approval'", a["step_id"])
    return {"ok": True, "status": status}


# ---------------- agents ----------------
@app.get("/api/agents")
async def get_agents(user=Depends(current_user)):
    rows = await agent_reg.list_agents(list(user["roles"]), include_disabled=True)
    paused = {r["scope"][6:] for r in await db.fetch("SELECT scope FROM controls WHERE paused AND scope LIKE 'agent:%%'")}
    stats = {r["agent_id"]: r for r in await db.fetch(
        "SELECT agent_id, count(*)::int AS steps, count(*) FILTER (WHERE status='done')::int AS done,"
        " count(*) FILTER (WHERE attempts > 0 OR reassignments > 0)::int AS retried FROM task_steps GROUP BY agent_id")}
    for r in rows:
        r["paused"] = r["id"] in paused
        r["stats"] = stats.get(r["id"], {})
    return rows


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str, user=Depends(current_user)):
    a = await db.fetchrow("SELECT * FROM agents WHERE id=%s", agent_id)
    if not a:
        raise HTTPException(404)
    versions = await db.fetch("SELECT version, yaml, created_by, created_at FROM agent_versions WHERE agent_id=%s"
                              " ORDER BY version DESC", agent_id)
    spec = await agent_reg.get_spec(agent_id)
    return {**a, "spec": spec, "yaml": versions[0]["yaml"] if versions else "", "versions": versions}


class SpecBody(BaseModel):
    yaml: str
    team_id: str | None = None


def _spec_error(e: Exception) -> str:
    if isinstance(e, jsonschema.ValidationError):
        where = ".".join(str(p) for p in e.absolute_path)
        return f"Invalid agent configuration{f' at {where}' if where else ''}: {e.message}"
    if isinstance(e, yaml.YAMLError):
        return f"The YAML could not be parsed: {str(e).splitlines()[0]}"
    return str(e)


async def _check_grants(user: dict, yaml_text: str):
    """Editors can only grant models, tools and scopes within what their own role allows."""
    is_admin = user["is_owner"] or any(RANK[r] >= RANK["admin"] for r in user["roles"].values())
    if not is_admin:  # permission first: never reveal validation details to someone who may not edit
        raise HTTPException(403, "only team admins can change agents")
    try:
        spec = agent_reg.parse_spec(yaml_text)
    except Exception as e:
        raise HTTPException(400, _spec_error(e))
    for alias in (spec["model"]["primary"], spec["model"].get("fallback")):
        if alias and alias not in gateway().models:
            raise HTTPException(400, f"unknown model alias {alias}")
    for name in spec["tools"]["builtin"]:
        if name not in BUILTINS:
            raise HTTPException(400, f"unknown builtin tool {name}")
    return spec


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, body: SpecBody, user=Depends(current_user)):
    await _check_grants(user, body.yaml)
    try:
        return await agent_reg.save_version(body.yaml, user["id"], expected_id=agent_id)
    except Exception as e:
        raise HTTPException(400, _spec_error(e))


@app.post("/api/agents")
async def create_agent(body: SpecBody, user=Depends(current_user)):
    spec = await _check_grants(user, body.yaml)
    if await db.fetchval("SELECT 1 FROM agents WHERE id=%s", spec["id"]):
        raise HTTPException(409, "agent id exists")
    if body.team_id:
        need(user, body.team_id, "admin")
    return await agent_reg.save_version(body.yaml, user["id"], team_id=body.team_id)


class Rollback(BaseModel):
    version: int


@app.post("/api/agents/{agent_id}/rollback")
async def rollback_agent(agent_id: str, body: Rollback, user=Depends(current_user)):
    need_admin_any(user)
    return await agent_reg.rollback(agent_id, body.version, user["id"])


class Clone(BaseModel):
    new_id: str
    team_id: str


@app.post("/api/agents/{agent_id}/clone")
async def clone_agent(agent_id: str, body: Clone, user=Depends(current_user)):
    need(user, body.team_id, "admin")
    row = await db.fetchrow("SELECT v.yaml FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version"
                            " WHERE a.id=%s", agent_id)
    if not row:
        raise HTTPException(404)
    import yaml as _y
    spec = _y.safe_load(row["yaml"])
    spec["id"] = body.new_id
    spec["name"] = f"{spec['name']} ({body.new_id})"
    return await agent_reg.save_version(_y.safe_dump(spec, sort_keys=False), user["id"], team_id=body.team_id,
                                        cloned_from=agent_id)


@app.post("/api/agents/{agent_id}/enabled")
async def toggle_agent(agent_id: str, enabled: bool, user=Depends(current_user)):
    need_admin_any(user)
    await db.execute("UPDATE agents SET enabled=%s WHERE id=%s", enabled, agent_id)
    await audit(user["id"], "agent.enabled" if enabled else "agent.disabled", agent_id)
    return {"ok": True}


class RouteReq(BaseModel):
    team_id: str
    text: str


@app.post("/api/route")
async def route_dry_run(body: RouteReq, user=Depends(current_user)):
    need(user, body.team_id, "member")
    return await route(body.text, body.team_id, UsageMeta(user_id=user["id"], team_id=body.team_id, agent_id="router"))


# ---------------- knowledge ----------------
@app.post("/api/knowledge/upload")
async def upload(team_id: str = Form(...), project: str | None = Form(None), file: UploadFile = File(...),
                 user=Depends(current_user)):
    need(user, team_id, "member")
    data = await file.read()
    text = knowledge.parse_file(file.filename or "upload.txt", data)
    if not text.strip():
        raise HTTPException(400, "no text could be extracted")
    res = await knowledge.ingest_text(team_id=team_id, title=file.filename or "upload", text=text, kind="upload",
                                      project=project, created_by=user["id"], trust=0.9,
                                      meta=UsageMeta(user_id=user["id"], team_id=team_id))
    await audit(user["id"], "knowledge.upload", res["source_id"], {"file": file.filename})
    return res


class TextDoc(BaseModel):
    team_id: str
    title: str
    text: str
    project: str | None = None


@app.post("/api/knowledge/text")
async def add_text(body: TextDoc, user=Depends(current_user)):
    need(user, body.team_id, "member")
    return await knowledge.ingest_text(team_id=body.team_id, title=body.title, text=body.text, kind="text",
                                       project=body.project, created_by=user["id"], trust=0.9,
                                       meta=UsageMeta(user_id=user["id"], team_id=body.team_id))


class UrlDoc(BaseModel):
    team_id: str
    url: str
    purpose: str = ""
    project: str | None = None


@app.post("/api/knowledge/url")
async def add_url(body: UrlDoc, user=Depends(current_user)):
    need(user, body.team_id, "member")
    res = await scraping.fetch_and_store(body.url, team_id=body.team_id, purpose=body.purpose, project=body.project,
                                         user_id=user["id"], meta=UsageMeta(user_id=user["id"], team_id=body.team_id))
    res.pop("content", None)
    return res


@app.get("/api/knowledge/sources")
async def sources(team_id: str, status: str | None = None, user=Depends(current_user)):
    need(user, team_id)
    return await db.fetch(
        "SELECT s.id, s.kind, s.url, s.domain, s.title, s.trust_score, s.status, s.scores, s.created_at, s.excerpt,"
        " (SELECT count(*) FROM chunks c WHERE c.source_id=s.id)::int AS chunks FROM sources s WHERE s.team_id=%s"
        " AND (%s::text IS NULL OR s.status=%s) ORDER BY s.created_at DESC LIMIT 200", team_id, status, status)


@app.get("/api/knowledge/search")
async def ksearch(team_id: str, q: str, user=Depends(current_user)):
    need(user, team_id)
    return await knowledge.search(q, [team_id], k=8, meta=UsageMeta(user_id=user["id"], team_id=team_id))


class SourceStatus(BaseModel):
    status: str


@app.post("/api/knowledge/sources/{source_id}/status")
async def source_status(source_id: str, body: SourceStatus, user=Depends(current_user)):
    s = await db.fetchrow("SELECT team_id FROM sources WHERE id=%s", source_id)
    if not s:
        raise HTTPException(404)
    need(user, s["team_id"], "admin")
    if body.status not in ("live", "quarantined", "superseded"):
        raise HTTPException(400, "bad status")
    await db.execute("UPDATE sources SET status=%s WHERE id=%s", body.status, source_id)
    await audit(user["id"], "knowledge.status", source_id, {"status": body.status})
    return {"ok": True}


@app.delete("/api/knowledge/sources/{source_id}")
async def delete_source(source_id: str, user=Depends(current_user)):
    s = await db.fetchrow("SELECT team_id FROM sources WHERE id=%s", source_id)
    if not s:
        raise HTTPException(404)
    need(user, s["team_id"], "admin")
    await db.execute("DELETE FROM sources WHERE id=%s", source_id)
    await audit(user["id"], "knowledge.rollback_source", source_id)
    return {"ok": True}


@app.get("/api/memory")
async def memory(status: str = "pending", user=Depends(current_user)):
    return await db.fetch("SELECT * FROM memory_proposals WHERE team_id::text = ANY(%s) AND status=%s ORDER BY created_at DESC",
                          list(user["roles"]), status)


@app.post("/api/memory/{proposal_id}")
async def decide_memory(proposal_id: str, body: Decision, user=Depends(current_user)):
    p = await db.fetchrow("SELECT team_id FROM memory_proposals WHERE id=%s", proposal_id)
    if not p:
        raise HTTPException(404)
    need(user, p["team_id"], "member")
    return await knowledge.decide_memory(proposal_id, body.decision, user["id"])


# ---------------- tools, MCP, skills ----------------
@app.get("/api/tools")
async def tools(user=Depends(current_user)):
    return {
        "builtin": [{"name": t.name, "description": t.description, "side_effect": t.side_effect} for t in BUILTINS.values()],
        "servers": await db.fetch("SELECT * FROM mcp_servers ORDER BY id"),
        "mcp_tools": await db.fetch("SELECT server_id, name, description, current_hash, pinned_hash, status, side_effect,"
                                    " updated_at FROM mcp_tools ORDER BY server_id, name"),
        "grants": await db.fetch("SELECT g.*, t.name AS team_name FROM tool_grants g JOIN teams t ON t.id=g.team_id"
                                 " WHERE g.team_id::text = ANY(%s) ORDER BY t.name, g.tool", list(user["roles"])),
    }


class McpServer(BaseModel):
    id: str
    name: str
    transport: str
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    secret_env: dict = {}
    headers: dict = {}
    description: str | None = None


@app.post("/api/mcp/servers")
async def add_mcp(body: McpServer, user=Depends(current_user)):
    need_admin_any(user)
    await db.execute("INSERT INTO mcp_servers(id, name, transport, command, args, url, secret_env, headers, description)"
                     " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", body.id, body.name, body.transport, body.command,
                     db.J(body.args), body.url, db.J(body.secret_env), db.J(body.headers), body.description)
    await audit(user["id"], "mcp.server_added", body.id, body.model_dump())
    return await mcp_client.refresh_server(body.id)


@app.post("/api/mcp/servers/{server_id}/refresh")
async def refresh_mcp(server_id: str, user=Depends(current_user)):
    need_admin_any(user)
    return await mcp_client.refresh_server(server_id)


@app.post("/api/mcp/tools/{server_id}/{name}/approve")
async def approve_mcp_tool(server_id: str, name: str, user=Depends(current_user)):
    need_admin_any(user)
    return {"ok": await mcp_client.approve_tool(server_id, name, user["id"])}


class Grant(BaseModel):
    team_id: str
    tool: str
    needs_approval: bool = False


@app.post("/api/grants")
async def add_grant(body: Grant, user=Depends(current_user)):
    need(user, body.team_id, "admin")
    await db.execute("INSERT INTO tool_grants(team_id, tool, needs_approval) VALUES (%s,%s,%s) ON CONFLICT (team_id, tool)"
                     " DO UPDATE SET needs_approval=EXCLUDED.needs_approval", body.team_id, body.tool, body.needs_approval)
    await audit(user["id"], "grant.set", body.tool, body.model_dump())
    return {"ok": True}


@app.delete("/api/grants/{grant_id}")
async def del_grant(grant_id: str, user=Depends(current_user)):
    g = await db.fetchrow("SELECT team_id, tool FROM tool_grants WHERE id=%s", grant_id)
    if not g:
        raise HTTPException(404)
    need(user, g["team_id"], "admin")
    await db.execute("DELETE FROM tool_grants WHERE id=%s", grant_id)
    await audit(user["id"], "grant.revoked", g["tool"], {"team_id": str(g["team_id"])})
    return {"ok": True}


@app.get("/api/skills")
async def skills(user=Depends(current_user)):
    await agent_reg.index_skills(auto_approve_new=False)
    return await db.fetch("SELECT name, description, content_hash, approved_hash, enabled, updated_at FROM skills ORDER BY name")


@app.post("/api/skills/{name}/approve")
async def approve_skill(name: str, user=Depends(current_user)):
    need_admin_any(user)
    await db.execute("UPDATE skills SET approved_hash=content_hash, enabled=true WHERE name=%s", name)
    await audit(user["id"], "skill.approved", name)
    return {"ok": True}


# ---------------- schedules ----------------
class ScheduleBody(BaseModel):
    team_id: str
    name: str
    cron: str
    request: str
    agent_id: str | None = None


@app.get("/api/schedules")
async def list_schedules(user=Depends(current_user)):
    return await db.fetch("SELECT s.*, u.name AS created_by_name FROM schedules s JOIN users u ON u.id=s.created_by"
                          " WHERE s.team_id::text = ANY(%s) ORDER BY s.created_at DESC", list(user["roles"]))


@app.post("/api/schedules")
async def add_schedule(body: ScheduleBody, user=Depends(current_user)):
    need(user, body.team_id, "member")
    if not croniter.is_valid(body.cron):
        raise HTTPException(400, "invalid cron expression")
    nxt = croniter(body.cron, datetime.now(timezone.utc)).get_next(datetime)
    sid = await db.fetchval("INSERT INTO schedules(team_id, created_by, name, cron, request, agent_id, next_run_at)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id", body.team_id, user["id"], body.name, body.cron,
                            body.request, body.agent_id, nxt)
    await audit(user["id"], "schedule.created", str(sid), body.model_dump())
    return {"id": sid, "next_run_at": nxt}


@app.post("/api/schedules/{schedule_id}/enabled")
async def toggle_schedule(schedule_id: str, enabled: bool, user=Depends(current_user)):
    s = await db.fetchrow("SELECT team_id FROM schedules WHERE id=%s", schedule_id)
    if not s:
        raise HTTPException(404)
    need(user, s["team_id"], "member")
    await db.execute("UPDATE schedules SET enabled=%s WHERE id=%s", enabled, schedule_id)
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, user=Depends(current_user)):
    s = await db.fetchrow("SELECT team_id FROM schedules WHERE id=%s", schedule_id)
    if not s:
        raise HTTPException(404)
    need(user, s["team_id"], "member")
    await db.execute("UPDATE schedules SET next_run_at=now() WHERE id=%s", schedule_id)
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def del_schedule(schedule_id: str, user=Depends(current_user)):
    s = await db.fetchrow("SELECT team_id FROM schedules WHERE id=%s", schedule_id)
    if not s:
        raise HTTPException(404)
    need(user, s["team_id"], "member")
    await db.execute("DELETE FROM schedules WHERE id=%s", schedule_id)
    return {"ok": True}


# ---------------- usage ----------------
@app.get("/api/usage")
async def usage(days: int = 7, user=Depends(current_user)):
    teams = list(user["roles"])
    w = "u.team_id::text = ANY(%s) AND u.created_at > now() - make_interval(days => %s)"
    p = (teams, days)
    q = lambda sql: db.fetch(sql, *p)  # noqa: E731
    return {
        "totals": (await q(f"SELECT coalesce(sum(cost_usd),0)::float AS cost_usd, coalesce(sum(tokens_in),0)::int AS tokens_in,"
                           f" coalesce(sum(tokens_out),0)::int AS tokens_out, count(*) FILTER (WHERE kind='llm')::int AS llm_calls,"
                           f" count(*) FILTER (WHERE kind='tool')::int AS tool_calls, count(*) FILTER (WHERE kind='media')::int AS media_calls,"
                           f" count(*) FILTER (WHERE status='rate_limited')::int AS rate_limited FROM usage_records u WHERE {w}"))[0],
        "by_day": await q(f"SELECT date_trunc('day', created_at)::date AS day, coalesce(sum(cost_usd),0)::float AS cost_usd,"
                          f" coalesce(sum(tokens_in+tokens_out),0)::int AS tokens FROM usage_records u WHERE {w} GROUP BY 1 ORDER BY 1"),
        "daily_quota": await gateway().daily_status(),
        "by_day_model": await q(f"SELECT date_trunc('day', created_at)::date AS day, model AS key,"
                                f" coalesce(sum(tokens_in+tokens_out),0)::int AS tokens, coalesce(sum(cost_usd),0)::float AS cost_usd"
                                f" FROM usage_records u WHERE {w} AND kind='llm' AND status='ok' GROUP BY 1,2 ORDER BY 1"),
        "by_kind": await q(f"SELECT kind AS key, count(*)::int AS calls, coalesce(sum(cost_usd),0)::float AS cost_usd"
                           f" FROM usage_records u WHERE {w} GROUP BY 1 ORDER BY 2 DESC"),
        "by_agent": await q(f"SELECT coalesce(agent_id,'-') AS key, coalesce(sum(cost_usd),0)::float AS cost_usd,"
                            f" coalesce(sum(tokens_in+tokens_out),0)::int AS tokens, count(*)::int AS calls FROM usage_records u"
                            f" WHERE {w} GROUP BY 1 ORDER BY 2 DESC, 3 DESC"),
        "by_model": await q(f"SELECT model AS key, provider, coalesce(sum(cost_usd),0)::float AS cost_usd,"
                            f" coalesce(sum(tokens_in+tokens_out),0)::int AS tokens, count(*)::int AS calls,"
                            f" count(*) FILTER (WHERE status <> 'ok')::int AS errors FROM usage_records u WHERE {w}"
                            f" AND kind IN ('llm','embed') GROUP BY 1,2 ORDER BY 3 DESC, 4 DESC"),
        "by_team": await q(f"SELECT t.name AS key, coalesce(sum(cost_usd),0)::float AS cost_usd,"
                           f" coalesce(sum(tokens_in+tokens_out),0)::int AS tokens FROM usage_records u JOIN teams t"
                           f" ON t.id=u.team_id WHERE {w} GROUP BY 1 ORDER BY 2 DESC"),
        "latency": await q(f"SELECT kind, model AS key, count(*)::int AS n,"
                           f" percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::int AS p50,"
                           f" percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95"
                           f" FROM usage_records u WHERE {w} AND status='ok' GROUP BY 1,2 ORDER BY 1,2"),
        "top_tasks": await q(f"SELECT t.id, t.title, coalesce(sum(u.cost_usd),0)::float AS cost_usd,"
                             f" coalesce(sum(u.tokens_in+u.tokens_out),0)::int AS tokens FROM usage_records u JOIN tasks t"
                             f" ON t.id=u.task_id WHERE {w} GROUP BY 1,2 ORDER BY 3 DESC, 4 DESC LIMIT 10"),
        "quality": await db.fetch(
            "SELECT e.actor_agent AS agent, count(*)::int AS reviews,"
            " round(avg(CASE WHEN (e.payload->>'first_pass')::boolean THEN 1 ELSE 0 END)::numeric, 3)::float AS first_pass_rate,"
            " round(avg(CASE WHEN (e.payload->>'accepted')::boolean THEN 1 ELSE 0 END)::numeric, 3)::float AS accept_rate"
            " FROM (SELECT ev.payload, s.agent_id AS actor_agent FROM events ev JOIN task_steps s ON s.id=ev.step_id"
            " WHERE ev.type='verification' AND ev.team_id::text = ANY(%s)) e GROUP BY 1 ORDER BY 1", teams),
        "steps": await db.fetch(
            "SELECT s.agent_id AS agent, count(*)::int AS steps, count(*) FILTER (WHERE s.status='done')::int AS done,"
            " round(avg(CASE WHEN s.attempts > 0 OR s.reassignments > 0 THEN 1 ELSE 0 END)::numeric, 3)::float AS retry_rate"
            " FROM task_steps s JOIN tasks t ON t.id=s.task_id WHERE t.team_id::text = ANY(%s) GROUP BY 1 ORDER BY 1", teams),
        "routing": (await db.fetch(
            "SELECT count(*) FILTER (WHERE mode <> 'manual')::int AS auto, count(*) FILTER (WHERE mode='manual')::int AS manual,"
            " count(*) FILTER (WHERE overridden_to IS NOT NULL)::int AS overrides, count(*) FILTER (WHERE mode='ask')::int AS asked"
            " FROM routing_log WHERE team_id::text = ANY(%s)", teams))[0],
        "budgets": await db.fetch(
            "SELECT t.id, t.title, t.status, t.budget_usd::float, t.budget_tokens, coalesce(sum(u.cost_usd),0)::float AS cost_usd,"
            " coalesce(sum(u.tokens_in+u.tokens_out),0)::int AS tokens FROM tasks t LEFT JOIN usage_records u ON u.task_id=t.id"
            " WHERE t.team_id::text = ANY(%s) AND t.status IN ('running','paused_budget','routing','planning')"
            " GROUP BY t.id ORDER BY t.created_at DESC LIMIT 20", teams),
    }


# ---------------- live trace ----------------
@app.get("/api/live")
async def live(task_id: str | None = None, user=Depends(current_user)):
    """Who is working right now: agent, model, current operation and live usage per active step."""
    teams = list(user["roles"])
    steps = await db.fetch(
        "SELECT s.id, s.task_id, s.key, s.title, s.agent_id, s.agent_version, s.status, s.locked_by, s.started_at,"
        " s.attempts, s.reassignments, t.title AS task_title, t.status AS task_status,"
        " (SELECT row_to_json(e) FROM (SELECT id, type, actor, payload, created_at FROM events WHERE step_id=s.id"
        "   ORDER BY id DESC LIMIT 1) e) AS last_event,"
        " (SELECT payload FROM events WHERE step_id=s.id AND type='llm_start' ORDER BY id DESC LIMIT 1) AS last_llm,"
        " (SELECT row_to_json(u) FROM (SELECT coalesce(sum(tokens_in),0)::int AS tokens_in,"
        "   coalesce(sum(tokens_out),0)::int AS tokens_out, coalesce(sum(cost_usd),0)::float AS cost_usd,"
        "   count(*) FILTER (WHERE kind='llm')::int AS llm_calls, count(*) FILTER (WHERE kind='tool')::int AS tool_calls"
        "   FROM usage_records WHERE step_id=s.id) u) AS usage"
        " FROM task_steps s JOIN tasks t ON t.id=s.task_id WHERE t.team_id::text = ANY(%s)"
        " AND (%s::uuid IS NULL OR s.task_id=%s::uuid)"
        " AND (s.status IN ('running','reviewing','waiting_approval','ready') OR %s::uuid IS NOT NULL)"
        " AND t.status NOT IN ('cancelled','completed','failed')"
        " ORDER BY s.started_at NULLS LAST, s.ord", teams, task_id, task_id, task_id)
    tasks = await db.fetch(
        "SELECT t.id, t.title, t.status, t.created_at,"
        " (SELECT row_to_json(e) FROM (SELECT id, type, actor, payload, created_at FROM events WHERE task_id=t.id"
        "   ORDER BY id DESC LIMIT 1) e) AS last_event,"
        " (SELECT row_to_json(u) FROM (SELECT coalesce(sum(tokens_in),0)::int AS tokens_in,"
        "   coalesce(sum(tokens_out),0)::int AS tokens_out, coalesce(sum(cost_usd),0)::float AS cost_usd"
        "   FROM usage_records WHERE task_id=t.id) u) AS usage"
        " FROM tasks t WHERE t.team_id::text = ANY(%s) AND (%s::uuid IS NULL OR t.id=%s::uuid)"
        " AND (t.status IN ('routing','planning','running','finalizing','awaiting_user','paused_budget','needs_attention')"
        "  OR %s::uuid IS NOT NULL) ORDER BY t.created_at DESC LIMIT 30", teams, task_id, task_id, task_id)
    workers = await db.fetch("SELECT locked_by AS worker, count(*)::int AS steps FROM task_steps WHERE status='running'"
                             " AND lease_until > now() GROUP BY 1")
    return {"steps": steps, "tasks": tasks, "workers": workers, "models": await gateway().describe()}


# ---------------- artifacts ----------------
@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, user=Depends(current_user)):
    res = await artifacts.read(artifact_id)
    if not res:
        raise HTTPException(404)
    row, data = res
    need(user, row["team_id"])
    return Response(content=data, media_type=row["mime"], headers={"Content-Disposition": f'inline; filename="{row["name"]}"'})


# ---------------- admin ----------------
@app.get("/api/admin/users")
async def users(user=Depends(current_user)):
    need_admin_any(user)
    return await db.fetch("SELECT u.id, u.email, u.name, u.is_owner, u.active, u.created_at, coalesce(json_agg(json_build_object("
                          "'team', t.name, 'team_id', t.id, 'role', m.role)) FILTER (WHERE t.id IS NOT NULL), '[]') AS teams"
                          " FROM users u LEFT JOIN memberships m ON m.user_id=u.id LEFT JOIN teams t ON t.id=m.team_id"
                          " GROUP BY u.id ORDER BY u.created_at")


class NewUser(BaseModel):
    email: str
    name: str
    password: str
    team_id: str
    role: str = "member"


@app.post("/api/admin/users")
async def add_user(body: NewUser, user=Depends(current_user)):
    need(user, body.team_id, "admin")
    if body.role not in RANK or RANK[body.role] > RANK[user["roles"][body.team_id]]:
        raise HTTPException(403, "cannot grant a role above your own")
    org = await db.fetchval("SELECT id FROM orgs LIMIT 1")
    uid = await db.fetchval("SELECT id FROM users WHERE email=%s", body.email.lower())
    if not uid:
        uid = await db.fetchval("INSERT INTO users(org_id, email, name, password_hash) VALUES (%s,%s,%s,%s) RETURNING id",
                                org, body.email.lower(), body.name, hash_password(body.password))
    await db.execute("INSERT INTO memberships(user_id, team_id, role) VALUES (%s,%s,%s) ON CONFLICT (user_id, team_id)"
                     " DO UPDATE SET role=EXCLUDED.role", uid, body.team_id, body.role)
    await audit(user["id"], "user.membership", body.email, {"team": body.team_id, "role": body.role})
    return {"id": uid}


class Control(BaseModel):
    scope: str  # all | team:<id> | agent:<id>
    paused: bool


@app.get("/api/admin/controls")
async def controls(user=Depends(current_user)):
    return await db.fetch("SELECT * FROM controls ORDER BY scope")


@app.post("/api/admin/controls")
async def set_control(body: Control, user=Depends(current_user)):
    if body.scope.startswith("team:"):
        need(user, body.scope[5:], "admin")
    else:
        need_admin_any(user)
    await db.execute("INSERT INTO controls(scope, paused, updated_by) VALUES (%s,%s,%s) ON CONFLICT (scope) DO UPDATE"
                     " SET paused=EXCLUDED.paused, updated_by=EXCLUDED.updated_by, updated_at=now()",
                     body.scope, body.paused, user["id"])
    await audit(user["id"], "kill_switch", body.scope, {"paused": body.paused})
    await emit("kill_switch", team_id=body.scope[5:] if body.scope.startswith("team:") else None,
               actor=str(user["id"]), payload=body.model_dump())
    return {"ok": True}


@app.get("/api/admin/audit")
async def audit_log(user=Depends(current_user)):
    need_admin_any(user)
    return await db.fetch("SELECT a.*, u.email FROM audit_log a LEFT JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 300")


class Credential(BaseModel):
    provider: str
    secret: str


@app.get("/api/admin/credentials")
async def credentials(user=Depends(current_user)):
    need_admin_any(user)
    return await db.fetch("SELECT provider, key_version, created_at FROM credentials ORDER BY provider")


@app.post("/api/admin/credentials")
async def set_credential(body: Credential, user=Depends(current_user)):
    if not user["is_owner"]:
        raise HTTPException(403, "only the owner manages provider keys")
    ct, nonce = encrypt(body.secret)
    await db.execute("INSERT INTO credentials(provider, ciphertext, nonce, created_by) VALUES (%s,%s,%s,%s)"
                     " ON CONFLICT (provider) DO UPDATE SET ciphertext=EXCLUDED.ciphertext, nonce=EXCLUDED.nonce,"
                     " key_version=credentials.key_version+1", body.provider, ct, nonce, user["id"])
    await audit(user["id"], "credential.set", body.provider)
    return {"ok": True}


@app.get("/api/models")
async def models(user=Depends(current_user)):
    return await gateway().describe()


class InstructionBody(BaseModel):
    scope_type: str
    scope_id: str
    body: str


@app.get("/api/instructions")
async def instructions(user=Depends(current_user)):
    return await db.fetch("SELECT DISTINCT ON (scope_type, scope_id) * FROM instructions ORDER BY scope_type, scope_id,"
                          " version DESC")


@app.post("/api/instructions")
async def set_instruction(body: InstructionBody, user=Depends(current_user)):
    if body.scope_type == "team":
        need(user, body.scope_id, "admin")
    else:
        need_admin_any(user)
    v = await db.fetchval("SELECT coalesce(max(version),0)+1 FROM instructions WHERE scope_type=%s AND scope_id=%s",
                          body.scope_type, body.scope_id)
    await db.execute("INSERT INTO instructions(scope_type, scope_id, version, body, created_by) VALUES (%s,%s,%s,%s,%s)",
                     body.scope_type, body.scope_id, v, body.body, user["id"])
    await audit(user["id"], "instructions.set", f"{body.scope_type}:{body.scope_id}", {"version": v})
    return {"version": v}


@app.get("/api/teams")
async def teams(user=Depends(current_user)):
    return await db.fetch("SELECT id, name, slug, budget_usd_per_task::float, budget_tokens_per_task, plan_approval_usd::float"
                          " FROM teams WHERE id::text = ANY(%s) ORDER BY name", list(user["roles"]))


class TeamBudget(BaseModel):
    budget_usd_per_task: float
    budget_tokens_per_task: int


@app.post("/api/teams/{team_id}/budget")
async def team_budget(team_id: str, body: TeamBudget, user=Depends(current_user)):
    need(user, team_id, "admin")
    await db.execute("UPDATE teams SET budget_usd_per_task=%s, budget_tokens_per_task=%s WHERE id=%s",
                     body.budget_usd_per_task, body.budget_tokens_per_task, team_id)
    await audit(user["id"], "team.budget", team_id, body.model_dump())
    return {"ok": True}
