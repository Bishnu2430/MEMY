import { useEffect, useState } from "react";
import { api, describe, money, num, stream } from "../api.js";
import { ConfirmButton, EventRow, LiveStep, Status, act, useInterval } from "../components.jsx";
import { modelLabel, workerLabel } from "../labels.js";

const WAITING = {
  needs_attention: "Waiting for a person: retry, reassign or take over a step.",
  paused_budget: "Paused at its budget: raise it to continue.",
  awaiting_user: "Waiting for someone to pick an agent.",
};

export default function Live() {
  const [live, setLive] = useState(null);
  const [feed, setFeed] = useState([]);
  useInterval(() => api("/live").then(setLive).catch(() => {}), 1500, []);
  useEffect(() => stream("/stream", (e) => setFeed((f) => [e, ...f].slice(0, 150)), -1), []);
  if (!live) return <div className="muted">Loading…</div>;
  const active = live.steps.filter((s) => ["running", "reviewing", "waiting_approval"].includes(s.status));
  const queued = live.steps.filter((s) => s.status === "ready");
  return (
    <div>
      <div className="page-head"><h2>Live trace</h2><span className="muted">which agent, on which model, is doing what — with live usage</span></div>
      <div className="grid3">
        <div className="card stat"><div className="muted small">Active steps</div><div className="big">{active.length}</div></div>
        <div className="card stat"><div className="muted small">Queued steps</div><div className="big">{queued.length}</div></div>
        <div className="card stat"><div className="muted small">Busy workers</div><div className="big">{live.workers.length}</div>
          <div className="muted small">{live.workers.map((w) => `${workerLabel(w.worker)} (${w.steps} step${w.steps === 1 ? "" : "s"})`).join(", ")}</div></div>
      </div>
      <div className="card">
        <h3>Working now</h3>
        {active.length === 0 && <div className="muted">No agent is working right now.</div>}
        {active.map((s) => (
          <div key={s.id}>
            <div className="row" style={{ margin: "10px 0 0" }}><a href={`#/tasks/${s.task_id}`} className="small">{s.task_title}</a>
              <ConfirmButton className="btn-stop right" confirm="Confirm stop" title="Stop every agent on this task"
                onConfirm={() => act(() => api(`/tasks/${s.task_id}/cancel`, { method: "POST" }), "Task stopped")}>Stop task</ConfirmButton></div>
            <LiveStep s={s} /></div>
        ))}
        {queued.length > 0 && <><h4>Queued</h4>{queued.map((s) => <div key={s.id} className="small">@{s.agent_id} · {s.title} <span className="muted">({s.task_title})</span></div>)}</>}
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Active tasks</h3>
          <table><thead><tr><th>Task</th><th>Status</th><th>Happening now</th><th>Usage so far</th><th /></tr></thead><tbody>
            {live.tasks.map((t) => (
              <tr key={t.id}><td><a href={`#/tasks/${t.id}`}>{t.title}</a></td><td><Status s={t.status} /></td>
                <td className="small">{WAITING[t.status]
                  ? <span><b>{WAITING[t.status]}</b> <a href={`#/tasks/${t.id}`}>Resolve →</a></span>
                  : describe(t.last_event)}</td>
                <td className="small">{money(t.usage?.cost_usd)} · {num((t.usage?.tokens_in || 0) + (t.usage?.tokens_out || 0))} tokens</td>
                <td className="nowrap"><ConfirmButton className="btn-stop" confirm="Confirm stop" title="Stop this task"
                  onConfirm={() => act(() => api(`/tasks/${t.id}/cancel`, { method: "POST" }), "Task stopped")}>Stop</ConfirmButton></td></tr>
            ))}
          </tbody></table>
          <h3>Models</h3>
          <table><thead><tr><th>Model role</th><th>Provider · model</th><th>Status</th><th>Currently uses</th></tr></thead><tbody>
            {live.models.map((m) => (
              <tr key={m.alias}><td>{modelLabel(m.alias)}<div className="faint tiny">{m.alias}</div></td><td className="small">{m.provider} · {m.model}</td>
                <td>{m.configured ? "Ready" : "No key"}</td><td>{m.resolves_to ? modelLabel(m.resolves_to) : "—"}</td></tr>
            ))}
          </tbody></table>
        </div>
        <div className="card">
          <h3>Event stream</h3>
          <div className="feed">{feed.map((e) => <EventRow key={e.id} e={e} />)}</div>
        </div>
      </div>
    </div>
  );
}
