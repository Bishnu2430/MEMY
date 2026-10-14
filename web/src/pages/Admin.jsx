import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { act } from "../components.jsx";
import { auditLabel, modelLabel, titleCase } from "../labels.js";
import Select from "../select.jsx";

const ROLE_ORDER = ["owner", "admin", "member", "viewer"];

function AccessMatrix() {
  const { me } = useApp();
  const [d, setD] = useState(null);
  const [team, setTeam] = useState({ name: "", slug: "" });
  const load = () => api("/access").then(setD).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!d) return null;
  return (
    <div className="card">
      <h3>Access</h3>
      <p className="muted small">Each person has one role per team. Agents act with the rights of the person who started the task — never more.
        The organisation owner can act in every team.</p>
      <div className="table-wrap"><table className="matrix">
        <thead><tr><th>Person</th>{d.teams.map((t) => <th key={t.id}>{t.name}</th>)}</tr></thead>
        <tbody>{d.users.map((u) => (
          <tr key={u.id}><td>{u.name}<div className="faint tiny">{u.email}{u.is_owner ? " · organisation owner" : ""}</div></td>
            {d.teams.map((t) => { const r = u.is_owner ? "owner" : u.roles[t.id]; return (
              <td key={t.id}>{r ? <span className={`role role-${r}`}>{r}</span> : <span className="faint tiny">no access</span>}</td>); })}
          </tr>))}</tbody>
      </table></div>
      <div className="legend roles">
        {ROLE_ORDER.slice().reverse().map((r) => (
          <div key={r}><span className={`role role-${r}`}>{r}</span> <span className="small">{d.capabilities[r].join(" · ")}</span>{r !== "viewer" && <span className="faint tiny"> (plus everything above)</span>}</div>
        ))}
      </div>
      {me.is_owner && <div className="row">
        <input placeholder="New team name" value={team.name} aria-label="Team name"
          onChange={(e) => setTeam({ name: e.target.value, slug: e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") })} />
        <input placeholder="slug" value={team.slug} aria-label="Team slug" onChange={(e) => setTeam({ ...team, slug: e.target.value })} />
        <button disabled={!team.name || !team.slug} onClick={() => act(() => api("/teams", { method: "POST", body: team }), `Team ${team.name} created`).then(() => { setTeam({ name: "", slug: "" }); load(); })}>Create team</button>
      </div>}
    </div>
  );
}

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
      <div className="page-head"><h2>Admin</h2><span className="muted">people, access, budgets, keys, instructions and the audit trail</span></div>
      {msg && <div className="error">{msg}</div>}
      <AccessMatrix />
      <div className="card">
        <h3>Kill switch</h3>
        <div className="row">
          <button className={paused("all") ? "danger" : ""} onClick={() => toggle("all")}>{paused("all") ? "Resume all agents" : "Pause all agents"}</button>
          {me.teams.map((t) => (
            <button key={t.id} className={paused(`team:${t.id}`) ? "danger" : ""} onClick={() => toggle(`team:${t.id}`)}>
              {paused(`team:${t.id}`) ? `Resume ${t.name}` : `Pause ${t.name}`}</button>))}
        </div>
        <div className="muted small">{controls.some((c) => c.paused) ? `Paused: ${controls.filter((c) => c.paused).map((c) => c.scope === "all" ? "everything" : c.scope.startsWith("team:") ? (me.teams.find((t) => t.id === c.scope.slice(5))?.name || "a team") : `@${c.scope.slice(6)}`).join(", ")}` : "Nothing is paused. Pausing stops new steps immediately; running steps stop at their next model call."}</div>
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Users & roles</h3>
          <table><tbody>{users.map((u) => (
            <tr key={u.id}><td>{u.name}<div className="muted small">{u.email}</div></td>
              <td className="small">{u.is_owner ? "Organisation owner · " : ""}{u.teams.map((t) => `${t.team}: ${titleCase(t.role)}`).join(", ") || "No team yet"}</td></tr>))}
          </tbody></table>
          <div className="row">
            <input placeholder="email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} />
            <input placeholder="name" value={nu.name} onChange={(e) => setNu({ ...nu, name: e.target.value })} />
            <input placeholder="password" type="password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
            <Select value={nu.team_id} onChange={(e) => setNu({ ...nu, team_id: e.target.value })}><option value="">team…</option>
              {me.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</Select>
            <Select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}>{["viewer", "member", "admin", "owner"].map((r) => <option key={r} value={r}>{titleCase(r)}</option>)}</Select>
            <button onClick={() => act(() => api("/admin/users", { method: "POST", body: nu }))}>Add / set role</button>
          </div>
        </div>
        <div className="card">
          <h3>Team budgets (per task)</h3>
          {teams.map((t) => <TeamBudget key={t.id} t={t} onSave={load} />)}
          <h3>Provider keys (encrypted at rest)</h3>
          <div className="muted small">Stored: {creds.map((c) => `${titleCase(c.provider)} (version ${c.key_version})`).join(", ") || "none (using .env)"}</div>
          <div className="row">
            <Select value={cred.provider} onChange={(e) => setCred({ ...cred, provider: e.target.value })}>{[["groq", "Groq"], ["elevenlabs", "ElevenLabs"], ["anthropic", "Anthropic"], ["openrouter", "OpenRouter"], ["github", "GitHub (for MCP)"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>
            <input type="password" placeholder="API key" value={cred.secret} onChange={(e) => setCred({ ...cred, secret: e.target.value })} />
            <button onClick={() => act(() => api("/admin/credentials", { method: "POST", body: cred }).then(() => setCred({ ...cred, secret: "" })))}>Save</button>
          </div>
          <h3>Models</h3>
          <p className="muted small">Agents name a role (like Supervisor model); each role resolves to a hosted model and overflows to the others when rate-limited.</p>
          <table><tbody>{models.map((m) => <tr key={m.alias}><td>{modelLabel(m.alias)}<div className="faint tiny">{m.alias}</div></td><td className="small">{m.provider} · {m.model}</td><td>{m.configured ? "Ready" : "No key"}</td><td className="small">uses {modelLabel(m.resolves_to)}</td></tr>)}</tbody></table>
        </div>
      </div>
      <div className="card">
        <h3>Instruction pool</h3>
        <table><tbody>{instr.map((i) => <tr key={i.id}><td>{titleCase(i.scope_type)}{i.scope_type === "team" ? `: ${me.teams.find((t) => t.id === i.scope_id)?.name || "team"}` : i.scope_type === "org" ? "" : `: ${i.scope_id}`}</td><td>v{i.version}</td><td className="small">{i.body}</td></tr>)}</tbody></table>
        <div className="row">
          <Select value={ins.scope_type} onChange={(e) => setIns({ ...ins, scope_type: e.target.value, scope_id: e.target.value === "org" ? "default" : "" })}>
            {[["org", "Organisation"], ["team", "Team"], ["project", "Project"], ["agent", "Agent"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>
          {ins.scope_type === "team" ? (
            <Select value={ins.scope_id} onChange={(e) => setIns({ ...ins, scope_id: e.target.value })}><option value="">team…</option>
              {me.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</Select>
          ) : <input placeholder="scope id" value={ins.scope_id} onChange={(e) => setIns({ ...ins, scope_id: e.target.value })} />}
        </div>
        <textarea rows={3} value={ins.body} onChange={(e) => setIns({ ...ins, body: e.target.value })} placeholder="instructions (later layers override earlier ones)" />
        <button onClick={() => act(() => api("/instructions", { method: "POST", body: ins }))}>Save new version</button>
      </div>
      <div className="card">
        <h3>Audit log</h3>
        <table><tbody>{audit.map((a) => (
          <tr key={a.id}><td className="muted small nowrap">{ago(a.at)}</td><td>{a.email || "system"}</td><td>{auditLabel(a.action)}</td>
            <td className="small">{a.target}</td><td className="small muted">{Object.entries(a.detail || {}).map(([k, v]) => `${titleCase(k)}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ").slice(0, 140)}</td></tr>))}
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
