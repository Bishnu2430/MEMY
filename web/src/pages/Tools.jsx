import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";

export default function Tools() {
  const { me } = useApp();
  const [d, setD] = useState(null);
  const [skills, setSkills] = useState([]);
  const [grant, setGrant] = useState({ team_id: "", tool: "", needs_approval: false });
  const [srv, setSrv] = useState({ id: "", name: "", transport: "stdio", command: "", args: "", url: "" });
  const [msg, setMsg] = useState("");
  const load = () => { api("/tools").then(setD).catch(() => {}); api("/skills").then(setSkills).catch(() => {}); };
  useEffect(() => { load(); }, []);
  const act = async (fn) => { setMsg(""); try { const r = await fn(); setMsg(JSON.stringify(r)); load(); } catch (e) { setMsg(e.message); } };
  if (!d) return <div className="muted">Loading…</div>;
  return (
    <div>
      <div className="page-head"><h2>Tools</h2><span className="muted">one tool gateway: grants, pinned MCP tool hashes, approvals</span></div>
      {msg && <pre className="small card">{msg}</pre>}
      <div className="card">
        <h3>MCP servers</h3>
        {d.servers.map((s) => (
          <div key={s.id} className="server">
            <div className="row"><b>{s.id}</b> <span className="muted small">{s.transport} · {s.command} {(s.args || []).join(" ")}{s.url}</span>
              <span className="muted small">refreshed {ago(s.refreshed_at)}</span>
              {s.last_error && <span className="error small">{s.last_error}</span>}
              <button className="right" onClick={() => act(() => api(`/mcp/servers/${s.id}/refresh`, { method: "POST" }))}>Re-check hashes</button></div>
            <table><thead><tr><th>Tool</th><th>Description</th><th>Pinned hash</th><th>Live hash</th><th>Status</th><th></th></tr></thead><tbody>
              {d.mcp_tools.filter((t) => t.server_id === s.id).map((t) => (
                <tr key={t.name} className={t.status !== "approved" ? "row-warn" : ""}>
                  <td><code>mcp.{s.id}.{t.name}</code></td><td className="small">{t.description}</td>
                  <td className="mono small">{t.pinned_hash?.slice(0, 10) || "—"}</td><td className="mono small">{t.current_hash?.slice(0, 10)}</td>
                  <td>{t.status === "approved" ? "✓ pinned" : "⚠ needs review"}</td>
                  <td>{t.status !== "approved" && <button onClick={() => act(() => api(`/mcp/tools/${s.id}/${t.name}/approve`, { method: "POST" }))}>Approve new hash</button>}</td>
                </tr>))}
            </tbody></table>
          </div>
        ))}
        <h4>Register MCP server</h4>
        <div className="row">
          <input placeholder="id" value={srv.id} onChange={(e) => setSrv({ ...srv, id: e.target.value })} />
          <input placeholder="name" value={srv.name} onChange={(e) => setSrv({ ...srv, name: e.target.value })} />
          <select value={srv.transport} onChange={(e) => setSrv({ ...srv, transport: e.target.value })}><option>stdio</option><option>http</option></select>
          {srv.transport === "stdio" ? <>
            <input placeholder="command (e.g. python)" value={srv.command} onChange={(e) => setSrv({ ...srv, command: e.target.value })} />
            <input placeholder="args (space separated)" value={srv.args} onChange={(e) => setSrv({ ...srv, args: e.target.value })} />
          </> : <input placeholder="https://…/mcp" value={srv.url} onChange={(e) => setSrv({ ...srv, url: e.target.value })} />}
          <button onClick={() => act(() => api("/mcp/servers", { method: "POST", body: { ...srv, args: srv.args.split(" ").filter(Boolean) } }))}>Add</button>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Team grants</h3>
          <table><thead><tr><th>Team</th><th>Tool pattern</th><th>Approval</th><th></th></tr></thead><tbody>
            {d.grants.map((g) => (
              <tr key={g.id}><td>{g.team_name}</td><td><code>{g.tool}</code></td><td>{g.needs_approval ? "per call" : "—"}</td>
                <td><button className="link" onClick={() => act(() => api(`/grants/${g.id}`, { method: "DELETE" }))}>revoke</button></td></tr>))}
          </tbody></table>
          <div className="row">
            <select value={grant.team_id} onChange={(e) => setGrant({ ...grant, team_id: e.target.value })}>
              <option value="">team…</option>{me.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
            <input placeholder="mcp.server.tool or mcp.server.*" value={grant.tool} onChange={(e) => setGrant({ ...grant, tool: e.target.value })} />
            <label className="small"><input type="checkbox" checked={grant.needs_approval} onChange={(e) => setGrant({ ...grant, needs_approval: e.target.checked })} /> approval</label>
            <button onClick={() => act(() => api("/grants", { method: "POST", body: grant }))}>Grant</button>
          </div>
        </div>
        <div className="card">
          <h3>Built-in tools</h3>
          <table><tbody>{d.builtin.map((t) => (
            <tr key={t.name}><td><code>{t.name}</code></td><td className="small">{t.description}</td><td>{t.side_effect ? "side effect" : "read"}</td></tr>))}
          </tbody></table>
          <h3>Skills</h3>
          <table><tbody>{skills.map((s) => (
            <tr key={s.name}><td><code>{s.name}</code></td><td className="small">{s.description}</td>
              <td>{s.enabled ? "✓ approved" : <button onClick={() => act(() => api(`/skills/${s.name}/approve`, { method: "POST" }))}>Approve content</button>}</td></tr>))}
          </tbody></table>
        </div>
      </div>
    </div>
  );
}
