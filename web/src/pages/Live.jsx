import { useEffect, useState } from "react";
import { api, describe, money, num, stream } from "../api.js";
import { EventRow, LiveStep, Status, useInterval } from "../components.jsx";

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
          <div className="muted small">{live.workers.map((w) => w.worker).join(", ")}</div></div>
      </div>
      <div className="card">
        <h3>Working now</h3>
        {active.length === 0 && <div className="muted">No agent is working right now.</div>}
        {active.map((s) => (
          <div key={s.id}><a href={`#/tasks/${s.task_id}`} className="small">{s.task_title}</a><LiveStep s={s} /></div>
        ))}
        {queued.length > 0 && <><h4>Queued</h4>{queued.map((s) => <div key={s.id} className="small">@{s.agent_id} · {s.title} <span className="muted">({s.task_title})</span></div>)}</>}
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Active tasks</h3>
          <table><thead><tr><th>Task</th><th>Status</th><th>Now</th><th>Usage</th></tr></thead><tbody>
            {live.tasks.map((t) => (
              <tr key={t.id}><td><a href={`#/tasks/${t.id}`}>{t.title}</a></td><td><Status s={t.status} /></td>
                <td className="small">{describe(t.last_event)}</td>
                <td className="small">{money(t.usage?.cost_usd)} · {num((t.usage?.tokens_in || 0) + (t.usage?.tokens_out || 0))} tok</td></tr>
            ))}
          </tbody></table>
          <h3>Models</h3>
          <table><thead><tr><th>Alias</th><th>Model</th><th>Configured</th><th>Resolves to</th></tr></thead><tbody>
            {live.models.map((m) => (
              <tr key={m.alias}><td>{m.alias}</td><td className="small">{m.provider} · {m.model}</td>
                <td>{m.configured ? "✓" : "—"}</td><td>{m.resolves_to}</td></tr>
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
