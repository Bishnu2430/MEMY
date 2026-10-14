"""Agent runtime worker: claims ready steps with a lease, runs them, heartbeats. A step whose
lease expires (crashed worker) is reclaimed by any other worker and resumed from its checkpoint."""
import asyncio
import logging
import os
import socket

from . import db
from .agents import get_spec
from .config import settings
from .events import emit
from .gateway import gateway
from .runtime import StepHalted, checkpointer, run_step

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("worker")
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

CLAIM_SQL = """
WITH c AS (
  SELECT st.id, st.locked_by AS prev_owner, st.status AS prev_status
  FROM task_steps st JOIN tasks t ON t.id = st.task_id
  WHERE (st.status = 'ready' OR (st.status = 'running' AND st.lease_until < now()))
    AND t.status = 'running'
    AND NOT EXISTS (SELECT 1 FROM controls c WHERE c.paused
                    AND c.scope IN ('all', 'team:' || t.team_id, 'agent:' || st.agent_id))
  ORDER BY st.created_at
  FOR UPDATE OF st SKIP LOCKED
  LIMIT 1)
UPDATE task_steps s SET status = 'running', locked_by = %s,
  lease_until = now() + make_interval(secs => %s), started_at = coalesce(s.started_at, now()), last_event_at = now()
FROM c WHERE s.id = c.id
RETURNING s.*, c.prev_owner, c.prev_status
"""


class LeaseLost(Exception):
    pass


async def heartbeat(step_id, task_id, runner: asyncio.Task):
    """Renew the lease every few seconds; stop the step if the lease was lost or the task was cancelled."""
    tick = 0
    while True:
        await asyncio.sleep(3)
        tick += 1
        status = await db.fetchval("SELECT status FROM tasks WHERE id=%s", task_id)
        if status == "cancelled":
            log.info("task %s cancelled; stopping step %s", task_id, step_id)
            runner.cancel()
            return
        if tick % 3 == 0:
            n = await db.execute("UPDATE task_steps SET lease_until=now() + make_interval(secs => %s)"
                                 " WHERE id=%s AND locked_by=%s AND status='running'", settings.lease_s, step_id, WORKER_ID)
            if n == 0:
                log.warning("lost lease on step %s; cancelling", step_id)
                runner.cancel()
                return


async def finish(step_id, status: str, **fields):
    sets = ", ".join([f"{k}=%s" for k in fields] + ["status=%s", "locked_by=NULL", "lease_until=NULL"])
    return await db.execute(f"UPDATE task_steps SET {sets} WHERE id=%s AND locked_by=%s",
                            *fields.values(), status, step_id, WORKER_ID)


async def process(step: dict):
    resumed_from = step["prev_owner"] if step["prev_status"] == "running" and step["prev_owner"] != WORKER_ID else None
    await emit("step_claimed", task_id=step["task_id"], step_id=step["id"], actor=WORKER_ID,
               payload={"worker": WORKER_ID, "agent": step["agent_id"], "attempt": step["attempts"],
                        "resumed_from": resumed_from})
    spec = await get_spec(step["agent_id"], step["agent_version"]) or {}
    timeout = int((spec.get("limits") or {}).get("timeout_s", settings.step_wall_time_s))
    runner = asyncio.create_task(asyncio.wait_for(run_step(step, WORKER_ID, resumed_from), timeout=timeout))
    hb = asyncio.create_task(heartbeat(step["id"], step["task_id"], runner))
    try:
        result = await runner
        if result["status"] == "waiting_approval":
            await finish(step["id"], "waiting_approval")
            await emit("step_waiting_approval", task_id=step["task_id"], step_id=step["id"], actor=WORKER_ID)
        else:
            await finish(step["id"], "review", output=result["output"])
            await emit("step_output", task_id=step["task_id"], step_id=step["id"], actor=step["agent_id"],
                       payload={"output": result["output"][:3000]})
    except asyncio.CancelledError:
        if await db.fetchval("SELECT status FROM tasks WHERE id=%s", step["task_id"]) == "cancelled":
            await finish(step["id"], "cancelled")
            await emit("step_cancelled", task_id=step["task_id"], step_id=step["id"], actor=WORKER_ID)
        else:
            log.info("step %s cancelled (lease lost)", step["id"])
    except asyncio.TimeoutError:
        await finish(step["id"], "failed", error=f"timeout: exceeded {timeout}s wall time")
        await emit("step_failed", task_id=step["task_id"], step_id=step["id"], payload={"reason": "timeout"})
    except StepHalted as h:
        if h.reason in ("paused", "budget"):
            await finish(step["id"], "paused")
            if h.reason == "budget":
                await db.execute("UPDATE tasks SET status='paused_budget', error=%s, updated_at=now() WHERE id=%s",
                                 h.detail, step["task_id"])
            await emit("budget_paused" if h.reason == "budget" else "step_paused", task_id=step["task_id"],
                       step_id=step["id"], payload={"detail": h.detail})
        elif h.reason == "cancelled":
            await finish(step["id"], "cancelled")
        else:
            await finish(step["id"], "failed", error=f"{h.reason}: {h.detail}")
            await emit("halted", task_id=step["task_id"], step_id=step["id"], payload={"guard": h.reason, "detail": h.detail})
    except Exception as e:
        log.exception("step %s failed", step["id"])
        await finish(step["id"], "failed", error=f"{type(e).__name__}: {str(e)[:500]}")
        await emit("step_failed", task_id=step["task_id"], step_id=step["id"], payload={"error": str(e)[:500]})
    finally:
        hb.cancel()


async def main():
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "3"))
    await db.init_pool(max_size=4 + concurrency * 2)
    gateway()
    await checkpointer()
    log.info("worker %s ready (up to %d concurrent steps)", WORKER_ID, concurrency)
    running: set[asyncio.Task] = set()
    while True:
        running = {t for t in running if not t.done()}
        if len(running) >= concurrency:
            await asyncio.sleep(0.5)
            continue
        try:
            step = await db.fetchrow(CLAIM_SQL, WORKER_ID, settings.lease_s)
        except Exception:
            log.exception("claim failed")
            step = None
        if not step:
            await asyncio.sleep(1)
            continue
        running.add(asyncio.create_task(process(dict(step))))


if __name__ == "__main__":
    asyncio.run(main())
