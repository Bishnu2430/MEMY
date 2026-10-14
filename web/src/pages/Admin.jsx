import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";

export default function Admin() {
  const { me } = useApp();
  const [controls, setControls] = useState([]);
  const [users, setUsers] = useState([]);
  const [audit, setAudit] = useState([]);
  const [creds, setCreds] = useState([]);
  const [teams, setTeams] = useState([]);
  const [instr, setInstr] = useState([]);
  const [models, setModels] = useState([]);
  const [nu, setNu] = useState({ email: "", name: "", password: "", team_id: "", role: "member" });
  const [cred, setCred] = useState({ provider: "groq", secret: "" });
  const [ins, setIns] = useState({ scope_type: "org", scope_id: "default", body: "" });
  const [msg, setMsg] = useState("");
  const load = () => {
    api("/admin/controls").then(setControls).catch(() => {});
    api("/admin/users").then(setUsers).catch((e) => setMsg(e.message));
    api("/admin/audit").then(setAudit).catch(() => {});
    api("/admin/credentials").then(setCreds).catch(() => {});
    api("/teams").then(setTeams).catch(() => {});
    api("/instructions").then(setInstr).catch(() => {});
    api("/models").then(setModels).catch(() => {});
  };
  useEffect(() => { load(); }, []);
  const act = async (fn) => { setMsg(""); try { await fn(); load(); } catch (e) { setMsg(e.message); } };
  const paused = (scope) => controls.find((c) => c.scope === scope)?.paused;
  const toggle = (scope) => act(() => api("/admin/controls", { method: "POST", body: { scope, paused: !paused(scope) } }));

  return (
    <div>
      <div className="page-head"><h2>Admin</h2></div>
      {msg && <div className="error">{msg}</div>}
      <div className="card">
        <h3>Kill switch</h3>
        <div className="row">
          <button className={paused("all") ? "danger" : ""} onClick={() => toggle("all")}>{paused("all") ? "▶ Resume everything" : "■ Pause everything"}</button>
          {me.teams.map((t) => (
            <button key={t.id} className={paused(`team:${t.id}`) ? "danger" : ""} onClick={() => toggle(`team:${t.id}`)}>
              {paused(`team:${t.id}`) ? `▶ Resume ${t.name}` : `■ Pause ${t.name}`}</button>))}
        </div>
        <div className="muted small">{controls.filter((c) => c.paused).map((c) => c.scope).join(", ") || "nothing paused"}</div>
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Users & roles</h3>
          <table><tbody>{users.map((u) => (
            <tr key={u.id}><td>{u.name}<div className="muted small">{u.email}</div></td>
              <td className="small">{u.is_owner ? "org owner · " : ""}{u.teams.map((t) => `${t.team}: ${t.role}`).join(", ")}</td></tr>))}
          </tbody></table>
          <div className="row">
            <input placeholder="email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} />
            <input placeholder="name" value={nu.name} onChange={(e) => setNu({ ...nu, name: e.target.value })} />
            <input placeholder="password" type="password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
            <select value={nu.team_id} onChange={(e) => setNu({ ...nu, team_id: e.target.value })}><option value="">team…</option>
              {me.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
            <select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}>{["viewer", "member", "admin", "owner"].map((r) => <option key={r}>{r}</option>)}</select>
            <button onClick={() => act(() => api("/admin/users", { method: "POST", body: nu }))}>Add / set role</button>
          </div>
        </div>
        <div className="card">
          <h3>Team budgets (per task)</h3>
          {teams.map((t) => <TeamBudget key={t.id} t={t} onSave={load} />)}
          <h3>Provider keys (encrypted at rest)</h3>
          <div className="muted small">Stored: {creds.map((c) => `${c.provider} v${c.key_version}`).join(", ") || "none (using .env)"}</div>
          <div className="row">
            <select value={cred.provider} onChange={(e) => setCred({ ...cred, provider: e.target.value })}>{["groq", "elevenlabs", "anthropic", "openrouter"].map((p) => <option key={p}>{p}</option>)}</select>
            <input type="password" placeholder="API key" value={cred.secret} onChange={(e) => setCred({ ...cred, secret: e.target.value })} />
            <button onClick={() => act(() => api("/admin/credentials", { method: "POST", body: cred }).then(() => setCred({ ...cred, secret: "" })))}>Save</button>
          </div>
          <h3>Model aliases</h3>
          <table><tbody>{models.map((m) => <tr key={m.alias}><td>{m.alias}</td><td className="small">{m.model}</td><td>{m.configured ? "✓" : "—"}</td><td className="small">→ {m.resolves_to}</td></tr>)}</tbody></table>
        </div>
      </div>
      <div className="card">
        <h3>Instruction pool</h3>
        <table><tbody>{instr.map((i) => <tr key={i.id}><td>{i.scope_type}:{i.scope_id.slice(0, 8)}</td><td>v{i.version}</td><td className="small">{i.body}</td></tr>)}</tbody></table>
        <div className="row">
          <select value={ins.scope_type} onChange={(e) => setIns({ ...ins, scope_type: e.target.value, scope_id: e.target.value === "org" ? "default" : "" })}>
            {["org", "team", "project", "agent"].map((s) => <option key={s}>{s}</option>)}</select>
          {ins.scope_type === "team" ? (
            <select value={ins.scope_id} onChange={(e) => setIns({ ...ins, scope_id: e.target.value })}><option value="">team…</option>
              {me.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
          ) : <input placeholder="scope id" value={ins.scope_id} onChange={(e) => setIns({ ...ins, scope_id: e.target.value })} />}
        </div>
        <textarea rows={3} value={ins.body} onChange={(e) => setIns({ ...ins, body: e.target.value })} placeholder="instructions (later layers override earlier ones)" />
        <button onClick={() => act(() => api("/instructions", { method: "POST", body: ins }))}>Save new version</button>
      </div>
      <div className="card">
        <h3>Audit log</h3>
        <table><tbody>{audit.map((a) => (
          <tr key={a.id}><td className="muted small nowrap">{ago(a.at)}</td><td>{a.email || "system"}</td><td><code>{a.action}</code></td>
            <td className="small">{a.target}</td><td className="small muted">{JSON.stringify(a.detail).slice(0, 100)}</td></tr>))}
        </tbody></table>
      </div>
    </div>
  );
}

function TeamBudget({ t, onSave }) {
  const [usd, setUsd] = useState(t.budget_usd_per_task);
  const [tok, setTok] = useState(t.budget_tokens_per_task);
  return (
    <div className="row small">
      <b>{t.name}</b>
      <input type="number" step="0.01" value={usd} onChange={(e) => setUsd(e.target.value)} /> USD
      <input type="number" value={tok} onChange={(e) => setTok(e.target.value)} /> tokens
      <button onClick={() => api(`/teams/${t.id}/budget`, { method: "POST", body: { budget_usd_per_task: Number(usd), budget_tokens_per_task: Number(tok) } }).then(onSave)}>Save</button>
    </div>
  );
}
