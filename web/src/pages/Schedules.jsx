import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { useAgents } from "../components.jsx";
import { cronLabel } from "../labels.js";
import Select from "../select.jsx";

export default function Schedules() {
  const { team } = useApp();
  const agents = useAgents();
  const [rows, setRows] = useState([]);
  const [f, setF] = useState({ name: "", cron: "0 9 * * 1", request: "", agent_id: "" });
  const [msg, setMsg] = useState("");
  const load = () => api("/schedules").then(setRows).catch(() => {});
  useEffect(() => { load(); }, []);
  const add = async () => {
    setMsg("");
    try { await api("/schedules", { method: "POST", body: { ...f, team_id: team, agent_id: f.agent_id || null } }); load(); }
    catch (e) { setMsg(e.message); }
  };
  return (
    <div>
      <div className="page-head"><h2>Schedules</h2><span className="muted">recurring tasks — each run uses the permissions of the person who created it, never more</span></div>
      <div className="card">
        <div className="row">
          <label className="field">Name<input placeholder="Weekly competitor check" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          <label className="field">Schedule (cron)<input value={f.cron} onChange={(e) => setF({ ...f, cron: e.target.value })} className="mono" aria-describedby="cron-help" /></label>
          <span id="cron-help" className="faint small">{cronLabel(f.cron)}</span>
          <label className="field">Agent<Select value={f.agent_id} onChange={(e) => setF({ ...f, agent_id: e.target.value })}>
            <option value="">Auto-route each run</option>{agents.map((a) => <option key={a.id} value={a.id}>{a.name} (@{a.id})</option>)}</Select></label>
        </div>
        <textarea rows={2} aria-label="Request" placeholder="What should run each time, e.g. re-check these sources and summarise what changed" value={f.request} onChange={(e) => setF({ ...f, request: e.target.value })} />
        <button className="primary" onClick={add} disabled={!f.name || !f.request}>Create schedule</button>
        {msg && <div className="error">{msg}</div>}
      </div>
      <div className="card">
        <table><thead><tr><th>Name</th><th>When</th><th>Agent</th><th>Next run</th><th>Last run</th><th title="Runs with this person's permissions">Runs as</th><th></th></tr></thead><tbody>
          {rows.map((s) => (
            <tr key={s.id}><td>{s.name}<div className="muted small">{s.request}</div></td><td>{cronLabel(s.cron)}<div className="faint tiny mono">{s.cron}</div>{!s.enabled && <div className="warn-text tiny">disabled</div>}</td><td>{s.agent_id ? `@${s.agent_id}` : "Auto-routed"}</td>
              <td className="small">{new Date(s.next_run_at).toLocaleString()}</td>
              <td className="small">{s.last_task_id ? <a href={`#/tasks/${s.last_task_id}`}>{ago(s.last_run_at)}</a> : "—"}</td>
              <td>{s.created_by_name}</td>
              <td className="nowrap">
                <button className="link" onClick={() => api(`/schedules/${s.id}/run`, { method: "POST" }).then(load)}>Run now</button>
                <button className="link" onClick={() => api(`/schedules/${s.id}/enabled?enabled=${!s.enabled}`, { method: "POST" }).then(load)}>{s.enabled ? "Disable" : "Enable"}</button>
                <button className="link" onClick={() => api(`/schedules/${s.id}`, { method: "DELETE" }).then(load)}>Delete</button></td></tr>))}
        </tbody></table>
      </div>
    </div>
  );
}
