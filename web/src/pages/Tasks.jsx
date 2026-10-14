import { useState } from "react";
import { ago, api, money } from "../api.js";
import { useApp } from "../App.jsx";
import { Status, useInterval } from "../components.jsx";

export default function Tasks() {
  const { team } = useApp();
  const [tasks, setTasks] = useState([]);
  useInterval(() => team && api(`/tasks?team_id=${team}&limit=100`).then(setTasks).catch(() => {}), 3000, [team]);
  return (
    <div>
      <div className="page-head"><h2>Tasks</h2><span className="muted">team task board</span></div>
      <div className="card">
        <table>
          <thead><tr><th>Task</th><th>Status</th><th>Mode</th><th>Steps</th><th>Cost</th><th>By</th><th>Created</th></tr></thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id} onClick={() => { window.location.hash = `/tasks/${t.id}`; }} className="clickable">
                <td>{t.title}</td><td><Status s={t.status} /></td>
                <td>{t.mode}{t.requested_agent ? ` @${t.requested_agent}` : ""}</td>
                <td>{t.steps_done}/{t.steps}</td><td>{money(t.cost_usd)}</td>
                <td>{t.created_by_name}</td><td className="muted small">{ago(t.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {tasks.length === 0 && <div className="muted">No tasks yet.</div>}
      </div>
    </div>
  );
}
