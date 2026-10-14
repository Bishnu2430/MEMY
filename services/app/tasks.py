"""Task ledger operations shared by the API, orchestrator and scheduler."""
import uuid

from . import db
from .agents import get_spec
from .events import emit
from .router import invocable_agents, parse_mention


class TaskError(Exception):
    pass


async def add_steps(task_id, steps: list[dict]):
    for i, s in enumerate(steps):
        sid = uuid.uuid4()
        spec = await get_spec(s["agent_id"])
        await db.execute(
            "INSERT INTO task_steps(id, task_id, ord, key, title, instruction, agent_id, agent_version, depends_on,"
            " agents_tried, thread_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            sid, task_id, i, s["key"], s["title"], s["instruction"], s["agent_id"], spec["version"] if spec else None,
            s.get("depends_on", []), [s["agent_id"]], f"{sid}:a0")


async def start_single(task: dict, agent_id: str, *, how: str):
    title = task["title"]
    await add_steps(task["id"], [{"key": "s1", "title": title, "instruction": task["request"], "agent_id": agent_id}])
    await db.execute("UPDATE tasks SET status='running', goal=coalesce(goal, request), acceptance=%s, updated_at=now()"
                     " WHERE id=%s", db.J(["The request is fully and correctly answered"]), task["id"])
    await emit("step_planned", task_id=task["id"], payload={"how": how, "agent_id": agent_id})


async def create_task(*, team_id, user_id, text: str, project: str | None = None, agent_id: str | None = None,
                      private: bool = False, schedule_id=None, budget_usd: float | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise TaskError("empty request")
    team = await db.fetchrow("SELECT * FROM teams WHERE id=%s", team_id)
    if not team:
        raise TaskError("unknown team")
    explicit = agent_id or parse_mention(text)
    if explicit:
        allowed = {a["id"] for a in await invocable_agents(team_id)}
        if explicit not in allowed:
            raise TaskError(f"agent @{explicit} is unknown, paused or not available to this team")
    title = text.replace("\n", " ")[:90]
    task = await db.fetchrow(
        "INSERT INTO tasks(team_id, project, created_by, title, request, status, mode, requested_agent, budget_usd,"
        " budget_tokens, private, schedule_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        team_id, project, user_id, title, text, "running" if explicit else "routing", "manual" if explicit else "auto",
        explicit, budget_usd or team["budget_usd_per_task"], team["budget_tokens_per_task"], private, schedule_id)
    await emit("task_created", task_id=task["id"], team_id=team_id, actor=str(user_id),
               payload={"request": text, "mode": task["mode"], "agent": explicit})
    if explicit:
        await db.execute("INSERT INTO routing_log(task_id, team_id, request, chosen, mode, confidence, reason)"
                         " VALUES (%s,%s,%s,%s,'manual',1,'explicit mention')", task["id"], team_id, text, explicit)
        await start_single(task, explicit, how="manual")
    return dict(await db.fetchrow("SELECT * FROM tasks WHERE id=%s", task["id"]))


async def task_cost(task_id) -> dict:
    return await db.fetchrow(
        "SELECT coalesce(sum(cost_usd),0)::float AS cost_usd, coalesce(sum(tokens_in),0)::int AS tokens_in,"
        " coalesce(sum(tokens_out),0)::int AS tokens_out, count(*) FILTER (WHERE kind='llm')::int AS llm_calls,"
        " count(*) FILTER (WHERE kind='tool')::int AS tool_calls FROM usage_records WHERE task_id=%s", task_id)
