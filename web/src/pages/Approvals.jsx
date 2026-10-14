import { useState } from "react";
import { ago, api } from "../api.js";
import { Status, useInterval } from "../components.jsx";
import { toolLabel } from "../labels.js";

export default function Approvals() {
  const [items, setItems] = useState([]);
  const [done, setDone] = useState([]);
  const [err, setErr] = useState("");
  const load = () => {
    api("/approvals").then(setItems).catch(() => {});
    api("/approvals?status=approved").then((a) => api("/approvals?status=rejected").then((r) => setDone([...a, ...r].slice(0, 30)))).catch(() => {});
  };
  useInterval(load, 3000, []);
  const decide = async (id, decision) => {
    setErr("");
    try { await api(`/approvals/${id}`, { method: "POST", body: { decision } }); load(); } catch (e) { setErr(e.message); }
  };
  return (
    <div>
      <div className="page-head"><h2>Approvals</h2><span className="muted">actions with side effects wait here, showing the exact tool and arguments</span></div>
      {err && <div className="error">{err}</div>}
      {items.length === 0 && <div className="card muted">Nothing waiting for approval.</div>}
      {items.map((a) => (
        <div key={a.id} className="card">
          <div className="row"><b>@{a.agent_id}</b> wants to run <b>{toolLabel(a.tool)}</b> <code className="faint">{a.tool}</code>
            <span className="muted small">in <a href={`#/tasks/${a.task_id}`}>{a.task_title}</a> · {ago(a.requested_at)}</span></div>
          <pre>{JSON.stringify(a.args, null, 2)}</pre>
          <button className="primary" onClick={() => decide(a.id, "approve")}>Approve</button>
          <button onClick={() => decide(a.id, "reject")}>Reject</button>
        </div>
      ))}
      {done.length > 0 && (
        <div className="card"><h3>Recent decisions</h3>
          <table><thead><tr><th>Action</th><th>Decision</th><th>Arguments</th><th>When</th></tr></thead><tbody>{done.map((a) => (
            <tr key={a.id}><td>{toolLabel(a.tool)}</td><td><Status s={a.status} /></td><td className="small">{JSON.stringify(a.args).slice(0, 80)}</td><td className="muted small">{ago(a.decided_at)}</td></tr>
          ))}</tbody></table>
        </div>
      )}
    </div>
  );
}
