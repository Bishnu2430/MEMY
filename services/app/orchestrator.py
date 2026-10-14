"""Master node loop: routing, planning, dispatch, review, retries, guards, scheduler.
Only routing, planning, review and synthesis call an LLM; everything else is plain code."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from croniter import croniter

from . import db, knowledge
from .config import settings
from .events import audit, emit
from .gateway import gateway
from .gateway import UsageMeta
from .router import pick_alternative, route
from .recorder import record_step, record_task
from .supervisor import plan, review, step_spec, synthesize, verify
from .tasks import add_steps, create_task, files_note, start_single
from .tools import mcp_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("orchestrator")


def waiter(task_id, step_id=None):
    async def on_wait(seconds: float, reason: str):
        await emit("rate_limit_wait", task_id=task_id, step_id=step_id, actor="gateway",
                   payload={"seconds": round(seconds, 1), "reason": reason})
    return on_wait


class Orchestrator:
    def __init__(self):
        self.inflight: set[tuple[str, str]] = set()
        self.last_sched = 0.0
        self.last_mcp = 0.0

    def spawn(self, kind: str, key, coro):
        k = (kind, str(key))
        if k in self.inflight:
            coro.close()
            return
        self.inflight.add(k)

        async def runner():
            try:
                await coro
            except Exception:
                log.exception("%s %s failed", kind, key)
            finally:
                self.inflight.discard(k)
        asyncio.create_task(runner())

    # ---------------- routing & planning ----------------
    async def op_start(self, kind: str, alias: str, task_id, step_id=None, **extra):
        target = (await gateway().resolve_chain(alias))[0]
        await emit(f"{kind}_start", task_id=task_id, step_id=step_id, actor=alias,
                   payload={"model": target["alias"], "model_name": target.get("model"), **extra})

    async def route_task(self, t: dict):
        await self.op_start("routing", "router", t["id"])
        d = await route(t["request"] + files_note(t), t["team_id"], UsageMeta(user_id=t["created_by"], team_id=t["team_id"],
                                                              agent_id="router", task_id=t["id"]), on_wait=waiter(t["id"]))
        await db.execute("INSERT INTO routing_log(task_id, team_id, request, shortlist, chosen, mode, confidence, reason)"
                         " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", t["id"], t["team_id"], t["request"],
                         db.J([{"id": a["id"], "similarity": a["similarity"]} for a in d["shortlist"]]),
                         d["agent_id"], d["mode"], d["confidence"], d["reason"])
        await emit("routing_decision", task_id=t["id"], actor="router", payload={k: d[k] for k in (
            "mode", "agent_id", "confidence", "reason")} | {"shortlist": [a["id"] for a in d["shortlist"]]})
        if d["mode"] == "none":
            await db.execute("UPDATE tasks SET status='failed', error=%s WHERE id=%s", d["reason"], t["id"])
        elif d["mode"] == "ask":
            await db.execute("UPDATE tasks SET status='awaiting_user', router=%s, updated_at=now() WHERE id=%s",
                             db.J(d), t["id"])
            await emit("routing_needs_user", task_id=t["id"], actor="router",
                       payload={"candidates": [{"id": a["id"], "name": a["name"]} for a in d["shortlist"][:2]]})
        elif d["mode"] == "split":
            await db.execute("UPDATE tasks SET status='planning', router=%s, updated_at=now() WHERE id=%s", db.J(d), t["id"])
        else:
            await db.execute("UPDATE tasks SET router=%s WHERE id=%s", db.J(d), t["id"])
            await start_single(t, d["agent_id"], how="auto")

    async def plan_task(self, t: dict):
        await self.op_start("planning", "supervisor", t["id"])
        p = await plan(t, hint={k: (t.get("router") or {}).get(k) for k in ("agent_id", "reason")}, on_wait=waiter(t["id"]))
        await db.execute("UPDATE tasks SET goal=%s, acceptance=%s, updated_at=now() WHERE id=%s",
                         p["goal"], db.J(p["acceptance"]), t["id"])
        await add_steps(t["id"], p["steps"])
        await db.execute("UPDATE tasks SET status='running' WHERE id=%s AND status='planning'", t["id"])
        await emit("plan_created", task_id=t["id"], actor="supervisor", payload=p)

    # ---------------- dispatch ----------------
    async def promote_ready(self):
        rows = await db.fetch(
            "UPDATE task_steps s SET status='ready' FROM tasks t WHERE s.task_id=t.id AND t.status='running'"
            " AND s.status='pending' AND NOT EXISTS (SELECT 1 FROM task_steps d WHERE d.task_id=s.task_id"
            " AND d.key = ANY(s.depends_on) AND d.status <> 'done') RETURNING s.id, s.task_id, s.key, s.agent_id")
        for r in rows:
            await emit("step_dispatched", task_id=r["task_id"], step_id=r["id"], payload={"key": r["key"], "agent": r["agent_id"]})

    # ---------------- review & correction ----------------
    async def review_step(self, s: dict):
        n = await db.execute("UPDATE task_steps SET status='reviewing' WHERE id=%s AND status='review'", s["id"])
        if not n:
            return
        t = await db.fetchrow("SELECT * FROM tasks WHERE id=%s", s["task_id"])
        s = await db.fetchrow("SELECT * FROM task_steps WHERE id=%s", s["id"])
        spec = await step_spec(s)
        if spec.get("verification") in ("code-default", "strict"):
            await self.op_start("review", "verifier", t["id"], s["id"], profile=spec.get("verification"), agent=s["agent_id"])
        else:
            await emit("review_start", task_id=t["id"], step_id=s["id"], actor="verifier",
                       payload={"model": "deterministic checks", "model_name": "-", "profile": spec.get("verification")})
        ver = await verify(t, s, spec)
        rev = {"verdict": "revise", "issues": [], "notes": "skipped: deterministic checks failed", "llm": False}
        if ver["passed"]:
            try:
                rev = await review(t, s, spec, ver, on_wait=waiter(t["id"], s["id"]))
            except Exception as e:
                rev = {"verdict": "accept", "issues": [], "notes": f"reviewer unavailable: {e}", "llm": False}
        high = [i for i in rev.get("issues", []) if i.get("severity") == "high"]
        accepted = ver["passed"] and (rev.get("verdict") == "accept" or not high)
        await db.execute("UPDATE task_steps SET verification=%s, review=%s WHERE id=%s", db.J(ver), db.J(rev), s["id"])
        await emit("verification", task_id=t["id"], step_id=s["id"], actor="verifier",
                   payload={"passed": ver["passed"], "checks": ver["checks"], "review": rev, "accepted": accepted,
                            "first_pass": accepted and s["attempts"] == 0 and s["reassignments"] == 0})
        if accepted:
            await db.execute("UPDATE task_steps SET status='done', finished_at=now() WHERE id=%s", s["id"])
            await emit("step_done", task_id=t["id"], step_id=s["id"], payload={"key": s["key"], "agent": s["agent_id"]})
            await record_step(t, dict(await db.fetchrow("SELECT * FROM task_steps WHERE id=%s", s["id"])))
            return
        failed = [f"{c['name']}: {c['detail']}" for c in ver["checks"] if not c["ok"]]
        issues = [f"[{i.get('severity')}] {i.get('description')} -> fix: {i.get('fix', '')}" for i in rev.get("issues", [])]
        correction = ("Your previous attempt was rejected. Fix these problems (keep what was correct, do not start over):\n"
                      + "\n".join(failed + issues)[:2500]
                      + "\n\nYour previous output:\n" + (s.get("output") or "")[:3000])
        await self.reject(s, correction, "verification")

    async def reject(self, s: dict, correction: str, reason: str, retry_limit: int | None = None):
        limit = settings.max_step_retries if retry_limit is None else retry_limit
        if s["attempts"] < limit:
            att = s["attempts"] + 1
            await db.execute("UPDATE task_steps SET status='ready', attempts=%s, correction=%s, thread_id=%s, output=NULL,"
                             " error=NULL, locked_by=NULL WHERE id=%s", att, correction, f"{s['id']}:{uuid.uuid4().hex[:10]}", s["id"])
            await emit("step_revision", task_id=s["task_id"], step_id=s["id"], actor="supervisor",
                       payload={"attempt": att, "reason": reason, "correction": correction})
            return
        if s["reassignments"] < settings.max_reassignments:
            t = await db.fetchrow("SELECT team_id, created_by FROM tasks WHERE id=%s", s["task_id"])
            alt = await pick_alternative(s["title"], s["instruction"], t["team_id"], list(s["agents_tried"] or []) + [s["agent_id"]],
                                         UsageMeta(user_id=t["created_by"], team_id=t["team_id"], agent_id="router",
                                                   task_id=s["task_id"]))
            new = alt.get("agent_id")
            if new:
                spec = await step_spec({"agent_id": new, "agent_version": None})
                await db.execute(
                    "UPDATE task_steps SET status='ready', agent_id=%s, agent_version=%s, attempts=0,"
                    " reassignments=reassignments+1, agents_tried=array_append(agents_tried, %s), correction=%s,"
                    " thread_id=%s, output=NULL, error=NULL, locked_by=NULL WHERE id=%s",
                    new, spec.get("version"), new, correction, f"{s['id']}:{uuid.uuid4().hex[:10]}", s["id"])
                await emit("step_reassigned", task_id=s["task_id"], step_id=s["id"], actor="supervisor",
                           payload={"from": s["agent_id"], "to": new, "reason": f"{reason}: {alt.get('reason', '')}"})
                return
        await db.execute("UPDATE task_steps SET status='escalated', locked_by=NULL WHERE id=%s", s["id"])
        await db.execute("UPDATE tasks SET status='needs_attention', error=%s, updated_at=now() WHERE id=%s",
                         f"step {s['key']} escalated: {correction[:300]}", s["task_id"])
        await emit("escalated", task_id=s["task_id"], step_id=s["id"], actor="supervisor",
                   payload={"reason": reason, "detail": correction[:1000]})

    async def handle_failed(self, s: dict):
        err = s.get("error") or ""
        if err.startswith("loop"):
            await self.reject(s, f"You repeated the same tool call ({err}). Change approach or answer directly.", "loop")
        elif err.startswith("timeout"):
            await self.reject(s, "The previous attempt exceeded the wall-time limit. Be more direct.", "timeout", retry_limit=1)
        else:
            await self.reject(s, f"The previous attempt failed with an error: {err[:500]}", "error")

    # ---------------- completion ----------------
    async def finalize(self, t: dict):
        n = await db.execute("UPDATE tasks SET status='finalizing' WHERE id=%s AND status='running'", t["id"])
        if not n:
            return
        steps = await db.fetch("SELECT * FROM task_steps WHERE task_id=%s ORDER BY ord", t["id"])
        if len(steps) > 1:
            await self.op_start("synthesis", "supervisor", t["id"])
        result = await synthesize(t, steps, on_wait=waiter(t["id"]))
        await db.execute("UPDATE tasks SET result=%s, updated_at=now() WHERE id=%s", result, t["id"])
        await emit("recording_start", task_id=t["id"], actor="recorder", payload={"model": "bulk"})
        await record_task(t, steps, result, on_wait=waiter(t["id"]))
        await db.execute("UPDATE tasks SET status='completed', completed_at=now(), updated_at=now() WHERE id=%s", t["id"])
        from .tasks import task_cost
        await emit("task_completed", task_id=t["id"], actor="supervisor", payload={"cost": await task_cost(t["id"])})

    # ---------------- guards ----------------
    async def guards(self):
        stalled = await db.fetch(
            "UPDATE task_steps SET lease_until=now() - interval '1 second' WHERE status='running' AND lease_until > now()"
            " AND last_event_at < now() - make_interval(secs => %s) RETURNING id, task_id, locked_by",
            settings.stall_after_s)
        for s in stalled:
            await emit("stalled", task_id=s["task_id"], step_id=s["id"], payload={"worker": s["locked_by"],
                       "action": "lease released; another worker resumes from checkpoint"})
        over = await db.fetch(
            "SELECT t.id FROM tasks t WHERE t.status='running' AND (SELECT count(*) + coalesce(sum(attempts + reassignments),0)"
            " FROM task_steps WHERE task_id=t.id) > %s", settings.max_steps_per_task)
        for t in over:
            await db.execute("UPDATE tasks SET status='needs_attention', error='step limit per task reached' WHERE id=%s", t["id"])
            await emit("halted", task_id=t["id"], payload={"guard": "steps_per_task", "limit": settings.max_steps_per_task})
        # Orphans: a step can never outlive its task. Anything unfinished under a finished task is closed.
        orphans = await db.fetch(
            "UPDATE task_steps s SET status='cancelled', locked_by=NULL, lease_until=NULL FROM tasks t"
            " WHERE s.task_id=t.id AND t.status IN ('cancelled','completed','failed')"
            " AND s.status NOT IN ('done','cancelled','escalated') RETURNING s.id, s.task_id")
        for o in orphans:
            await emit("step_cancelled", task_id=o["task_id"], step_id=o["id"], payload={"reason": "task already finished"})
        # Steps paused by the kill switch resume when the switch is lifted.
        await db.execute(
            "UPDATE task_steps s SET status='ready' FROM tasks t WHERE s.task_id=t.id AND s.status='paused'"
            " AND t.status='running' AND NOT EXISTS (SELECT 1 FROM controls c WHERE c.paused AND"
            " c.scope IN ('all', 'team:' || t.team_id, 'agent:' || s.agent_id))")

    # ---------------- scheduler ----------------
    async def scheduler(self):
        due = await db.fetch("SELECT * FROM schedules WHERE enabled AND next_run_at <= now()")
        for sc in due:
            role = await db.fetchval("SELECT role FROM memberships WHERE user_id=%s AND team_id=%s",
                                     sc["created_by"], sc["team_id"])
            active = await db.fetchval("SELECT active FROM users WHERE id=%s", sc["created_by"])
            nxt = croniter(sc["cron"], datetime.now(timezone.utc)).get_next(datetime)
            if role not in ("owner", "admin", "member") or not active:
                await db.execute("UPDATE schedules SET enabled=false WHERE id=%s", sc["id"])
                await audit(None, "schedule.disabled", str(sc["id"]), {"reason": "creator lost access"})
                continue
            try:
                t = await create_task(team_id=sc["team_id"], user_id=sc["created_by"], text=sc["request"],
                                      agent_id=sc["agent_id"], schedule_id=sc["id"])
                await db.execute("UPDATE schedules SET last_run_at=now(), next_run_at=%s, last_task_id=%s WHERE id=%s",
                                 nxt, t["id"], sc["id"])
            except Exception as e:
                log.warning("schedule %s failed: %s", sc["id"], e)
                await db.execute("UPDATE schedules SET next_run_at=%s WHERE id=%s", nxt, sc["id"])

    async def refresh_mcp(self):
        for s in await db.fetch("SELECT id FROM mcp_servers WHERE status='active'"):
            await mcp_client.refresh_server(s["id"])

    # ---------------- main loop ----------------
    async def tick(self):
        for t in await db.fetch("SELECT * FROM tasks WHERE status='routing' ORDER BY created_at LIMIT 10"):
            self.spawn("route", t["id"], self.route_task(dict(t)))
        for t in await db.fetch("SELECT * FROM tasks WHERE status='planning' ORDER BY created_at LIMIT 10"):
            self.spawn("plan", t["id"], self.plan_task(dict(t)))
        await self.promote_ready()
        for s in await db.fetch("SELECT * FROM task_steps WHERE status='review' ORDER BY created_at LIMIT 10"):
            self.spawn("review", s["id"], self.review_step(dict(s)))
        for s in await db.fetch("SELECT * FROM task_steps WHERE status='failed' LIMIT 10"):
            self.spawn("failed", s["id"], self.handle_failed(dict(s)))
        for t in await db.fetch(
                "SELECT t.* FROM tasks t WHERE t.status='running' AND EXISTS (SELECT 1 FROM task_steps s WHERE s.task_id=t.id)"
                " AND NOT EXISTS (SELECT 1 FROM task_steps s WHERE s.task_id=t.id AND s.status <> 'done')"):
            self.spawn("finalize", t["id"], self.finalize(dict(t)))
        await self.guards()
        now = asyncio.get_event_loop().time()
        if now - self.last_sched > 20:
            self.last_sched = now
            self.spawn("scheduler", "all", self.scheduler())
        if now - self.last_mcp > 300:
            self.last_mcp = now
            self.spawn("mcp", "all", self.refresh_mcp())

    async def run(self):
        await db.init_pool(max_size=12)
        gateway()
        async with db.pool().connection() as lock_conn:
            async def try_lock():
                return (await (await lock_conn.execute("SELECT pg_try_advisory_lock(4242) AS ok")).fetchone())["ok"]
            while not await try_lock():
                log.info("another orchestrator holds the leader lock; waiting")
                await asyncio.sleep(5)
            log.info("orchestrator is leader")
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("tick failed")
                await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
