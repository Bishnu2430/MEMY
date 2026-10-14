"""AgentRuntime: one generic executor runs every AgentSpec as a LangGraph graph with Postgres
checkpoints, so any worker can resume any step from its last checkpoint."""
import hashlib
import json
import logging
import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import db
from .agents import get_spec
from .config import settings
from .context import compile_system, trim_turns
from .events import emit
from .gateway import UsageMeta, gateway
from .tools import ToolContext, execute, openai_tools, resolve_tools, validate_args

log = logging.getLogger("runtime")


class StepHalted(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


class StepState(TypedDict, total=False):
    messages: Annotated[list, operator.add]
    turns: int
    final: str | None
    call_counts: dict


_saver: AsyncPostgresSaver | None = None


async def checkpointer(setup: bool = False) -> AsyncPostgresSaver:
    global _saver
    if _saver is None:
        _saver = AsyncPostgresSaver(db.pool())
        if setup:
            await _saver.setup()
    return _saver


async def check_guards(task_id, step_id, team_id, agent_id, max_step_cost: float):
    """Budgets, kill switch and cancellation are enforced in code before every model turn."""
    paused = await db.fetchval("SELECT count(*) FROM controls WHERE paused AND scope = ANY(%s)",
                               ["all", f"team:{team_id}", f"agent:{agent_id}"])
    if paused:
        raise StepHalted("paused", "kill switch active")
    t = await db.fetchrow(
        "SELECT t.status, t.budget_usd, t.budget_tokens, coalesce(sum(u.cost_usd),0) AS cost,"
        " coalesce(sum(u.tokens_in + u.tokens_out),0) AS tokens,"
        " coalesce(sum(u.cost_usd) FILTER (WHERE u.step_id=%s),0) AS step_cost"
        " FROM tasks t LEFT JOIN usage_records u ON u.task_id=t.id WHERE t.id=%s GROUP BY t.id", step_id, task_id)
    if t["status"] == "cancelled":
        raise StepHalted("cancelled", "task cancelled")
    if t["cost"] >= t["budget_usd"] or t["tokens"] >= t["budget_tokens"]:
        raise StepHalted("budget", f"task budget reached (${float(t['cost']):.4f}, {t['tokens']} tokens)")
    if float(t["step_cost"]) >= max_step_cost:
        raise StepHalted("budget", f"agent step cost limit ${max_step_cost} reached")


def _assistant_msg(content: str, calls: list) -> dict:
    msg = {"role": "assistant", "content": content or ""}
    if calls:
        msg["tool_calls"] = [{"id": c["id"], "type": "function",
                              "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]
    return msg


async def _cached_result(step_id, call_id) -> str | None:
    row = await db.fetchrow("SELECT payload FROM events WHERE step_id=%s AND type='tool_result'"
                            " AND payload->>'call_id'=%s ORDER BY id DESC LIMIT 1", step_id, call_id)
    return row["payload"].get("result") if row else None


def build_graph(ctx: ToolContext, resolved: dict, system: str, budget_chars: int, saver):
    spec, task, step = ctx.spec, ctx.task, ctx.step
    gw = gateway()
    tools = openai_tools(resolved)
    max_turns = int(spec["limits"].get("max_steps", 10))
    step_prompt = f"Your step: {step['title']}\n\n{step['instruction']}"
    primary = spec["model"]["primary"]
    fallback = spec["model"].get("fallback")
    temperature = float(spec["model"].get("temperature", 0.2))

    def invalid_calls(calls):
        errs = []
        for c in calls:
            t = resolved.get(c["name"])
            if not t:
                errs.append(f"unknown tool '{c['name']}'")
                continue
            c["arguments"], err = validate_args(t, c["arguments"])
            if err:
                errs.append(f"{c['name']}: {err}")
        return errs

    async def model_node(state: StepState):
        await check_guards(task["id"], step["id"], task["team_id"], spec["id"], float(spec["limits"].get("max_cost_usd", 1.0)))
        turns = state.get("turns", 0)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": step_prompt}]
        msgs += trim_turns(state.get("messages", []), int(budget_chars * 0.35))
        final_only = turns >= max_turns or not tools
        if turns >= max_turns:
            msgs.append({"role": "user", "content": "Turn limit reached. Give your final answer now, without tools."})
        target = (await gw.resolve_chain(primary))[0]
        await emit("llm_start", task_id=task["id"], step_id=step["id"], actor=spec["id"], payload={
            "model": target["alias"], "model_name": target.get("model"), "provider": target["provider"], "turn": turns + 1,
            "prompt_chars": sum(len(str(m.get("content") or "")) for m in msgs), "tools_offered": 0 if final_only else len(tools)})
        res = await gw.chat(primary, msgs, tools=None if final_only else tools, meta=ctx.meta, temperature=temperature,
                            max_tokens=1500)
        errs = invalid_calls(res.tool_calls) if res.tool_calls else []
        if errs:
            # Retry once with the validation error, then fail over to the fallback alias.
            retry_msgs = msgs + [_assistant_msg(res.content, []), {"role": "user", "content":
                                 f"Your tool call was invalid: {'; '.join(errs)}. Available tools: "
                                 f"{', '.join(resolved)}. Call a valid tool with valid arguments, or answer directly."}]
            res = await gw.chat(primary, retry_msgs, tools=tools, meta=ctx.meta, temperature=temperature)
            errs = invalid_calls(res.tool_calls) if res.tool_calls else []
            if errs and fallback:
                res = await gw.chat(fallback, msgs, tools=tools, meta=ctx.meta, temperature=temperature)
                errs = invalid_calls(res.tool_calls) if res.tool_calls else []
            if errs:
                await emit("tool_call_invalid", task_id=task["id"], step_id=step["id"], actor=spec["id"],
                           payload={"errors": errs})
                res.tool_calls = []
        for i, c in enumerate(res.tool_calls):
            c["id"] = f"c{turns}_{i}"
        await emit("llm_turn", task_id=task["id"], step_id=step["id"], actor=spec["id"], payload={
            "model": res.alias, "model_name": res.model, "turn": turns + 1, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "latency_ms": res.latency_ms,
            "cost_usd": res.cost_usd, "content": res.content[:1500],
            "tool_calls": [{"name": resolved[c["name"]].name, "args": c["arguments"]} for c in res.tool_calls]})
        update = {"messages": [_assistant_msg(res.content, res.tool_calls)], "turns": turns + 1}
        if not res.tool_calls:
            update["final"] = (res.content or "").strip() or "(the agent returned an empty answer)"
        return update

    async def tools_node(state: StepState):
        calls = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"])}
                 for tc in state["messages"][-1].get("tool_calls", [])]
        counts = dict(state.get("call_counts") or {})
        for c in calls:
            sig = hashlib.sha1(f"{c['name']}:{json.dumps(c['arguments'], sort_keys=True)}".encode()).hexdigest()[:16]
            counts[sig] = counts.get(sig, 0) + 1
            if counts[sig] >= settings.identical_tool_call_limit:
                raise StepHalted("loop", f"identical call to {c['name'].replace('__', '.')} repeated "
                                         f"{settings.identical_tool_call_limit} times")
        needing = [c for c in calls if c["name"] in resolved and resolved[c["name"]].needs_approval]
        decisions = {}
        if needing:
            for c in needing:
                aid = await db.fetchval(
                    "INSERT INTO approvals(task_id, step_id, team_id, tool, args, tool_call_id, reason) VALUES"
                    " (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (step_id, tool_call_id) DO NOTHING RETURNING id",
                    task["id"], step["id"], task["team_id"], resolved[c["name"]].name, db.J(c["arguments"]),
                    f"{step['thread_id']}:{c['id']}", "side-effecting tool")
                if aid:
                    await emit("approval_requested", task_id=task["id"], step_id=step["id"], actor=spec["id"],
                               payload={"approval_id": str(aid), "tool": resolved[c["name"]].name, "args": c["arguments"]})
            ids = [f"{step['thread_id']}:{c['id']}" for c in needing]
            rows = await db.fetch("SELECT tool_call_id, status FROM approvals WHERE step_id=%s AND tool_call_id = ANY(%s)",
                                  step["id"], ids)
            if any(r["status"] == "pending" for r in rows):
                interrupt({"approvals": ids})
                rows = await db.fetch("SELECT tool_call_id, status FROM approvals WHERE step_id=%s AND tool_call_id = ANY(%s)",
                                      step["id"], ids)
            decisions = {r["tool_call_id"]: r["status"] for r in rows}
        results = []
        for c in calls:
            t = resolved.get(c["name"])
            call_id = f"{step['thread_id']}:{c['id']}"
            cached = await _cached_result(step["id"], call_id)
            if cached is not None:
                content = cached
            elif t is None:
                content = "This tool is no longer available to you."
            elif t.needs_approval and decisions.get(call_id) != "approved":
                content = "REJECTED by a human reviewer. Do not retry this action; explain in your final answer."
            else:
                content = await execute(ctx, t, c["arguments"], call_id)
            results.append({"role": "tool", "tool_call_id": c["id"], "content": content})
        return {"messages": results, "call_counts": counts}

    def after_model(state: StepState):
        return "end" if state.get("final") is not None else "tools"

    g = StateGraph(StepState)
    g.add_node("model", model_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, {"tools": "tools", "end": END})
    g.add_edge("tools", "model")
    return g.compile(checkpointer=saver)


async def run_step(step: dict, worker_id: str, resumed_from: str | None = None) -> dict:
    task = await db.fetchrow("SELECT * FROM tasks WHERE id=%s", step["task_id"])
    spec = await get_spec(step["agent_id"], step["agent_version"])
    if not spec:
        raise StepHalted("config", f"agent {step['agent_id']} not found")
    meta = UsageMeta(user_id=task["created_by"], team_id=task["team_id"], agent_id=spec["id"], task_id=task["id"],
                     step_id=step["id"])
    ctx = ToolContext(task=task, step=step, spec=spec, meta=meta)
    resolved = await resolve_tools(spec, task["team_id"])
    chain = await gateway().resolve_chain(spec["model"]["primary"])
    budget_chars = gateway().context_chars(chain[0])
    system = await compile_system(task, step, spec, budget_chars, meta)
    saver = await checkpointer()
    graph = build_graph(ctx, resolved, system, budget_chars, saver)
    config = {"configurable": {"thread_id": step["thread_id"]},
              "recursion_limit": int(spec["limits"].get("max_steps", 10)) * 2 + 8}

    snap = await graph.aget_state(config)
    has_interrupt = bool(snap.next) and any(getattr(t, "interrupts", None) for t in snap.tasks)
    if snap.next:
        await emit("resumed_by", task_id=task["id"], step_id=step["id"], actor=worker_id, payload={
            "worker": worker_id, "previous_owner": resumed_from, "from_checkpoint": True, "next": list(snap.next),
            "turns_so_far": snap.values.get("turns", 0), "approval_resume": has_interrupt})
        inp = Command(resume=True) if has_interrupt else None
    elif snap.values.get("final") is not None:
        return {"status": "done", "output": snap.values["final"], "extra": {}}
    else:
        if resumed_from:
            await emit("resumed_by", task_id=task["id"], step_id=step["id"], actor=worker_id,
                       payload={"worker": worker_id, "previous_owner": resumed_from, "from_checkpoint": False})
        inp = {"messages": [], "turns": 0, "final": None, "call_counts": {}}

    await graph.ainvoke(inp, config)
    snap = await graph.aget_state(config)
    if snap.next and any(getattr(t, "interrupts", None) for t in snap.tasks):
        return {"status": "waiting_approval"}
    return {"status": "done", "output": snap.values.get("final") or "", "extra": ctx.extra}
