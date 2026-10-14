import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { useAgents } from "../components.jsx";

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
      <div className="page-head"><h2>Schedules</h2><span className="muted">recurring tasks run with the creator's rights, never elevated</span></div>
      <div className="card">
        <div className="row">
          <input placeholder="name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          <input placeholder="cron" value={f.cron} onChange={(e) => setF({ ...f, cron: e.target.value })} className="mono" />
          <select value={f.agent_id} onChange={(e) => setF({ ...f, agent_id: e.target.value })}>
            <option value="">auto-route</option>{agents.map((a) => <option key={a.id} value={a.id}>@{a.id}</option>)}</select>
        </div>
        <textarea rows={2} placeholder="request, e.g. re-check these sources" value={f.request} onChange={(e) => setF({ ...f, request: e.target.value })} />
        <button className="primary" onClick={add} disabled={!f.name || !f.request}>Create schedule</button>
        {msg && <div className="error">{msg}</div>}
      </div>
      <div className="card">
        <table><thead><tr><th>Name</th><th>Cron</th><th>Agent</th><th>Next run</th><th>Last run</th><th>Owner</th><th></th></tr></thead><tbody>
          {rows.map((s) => (
            <tr key={s.id}><td>{s.name}<div className="muted small">{s.request}</div></td><td className="mono">{s.cron}</td><td>{s.agent_id || "auto"}</td>
              <td className="small">{new Date(s.next_run_at).toLocaleString()}</td>
              <td className="small">{s.last_task_id ? <a href={`#/tasks/${s.last_task_id}`}>{ago(s.last_run_at)}</a> : "—"}</td>
              <td>{s.created_by_name}</td>
              <td className="nowrap">
                <button className="link" onClick={() => api(`/schedules/${s.id}/run`, { method: "POST" }).then(load)}>run now</button>
                <button className="link" onClick={() => api(`/schedules/${s.id}/enabled?enabled=${!s.enabled}`, { method: "POST" }).then(load)}>{s.enabled ? "disable" : "enable"}</button>
                <button className="link" onClick={() => api(`/schedules/${s.id}`, { method: "DELETE" }).then(load)}>delete</button></td></tr>))}
        </tbody></table>
      </div>
    </div>
  );
}
