import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { ConfirmButton, Status, act } from "../components.jsx";
import { titleCase, toolLabel } from "../labels.js";
import Select from "../select.jsx";

const parseMap = (txt) => Object.fromEntries(txt.split("\n").map((l) => l.trim()).filter((l) => l.includes("="))
  .map((l) => l.split("=").map((x) => x.trim())));
const showMap = (m) => Object.entries(m || {}).map(([k, v]) => `${k}=${v}`).join("\n");

function ServerCard({ s, tools, onChange, isAdmin }) {
  const [secrets, setSecrets] = useState(showMap(s.secret_env));
  const [editing, setEditing] = useState(false);
  return (
    <div className="server card">
      <div className="row">
        <b>{s.name}</b> <span className="faint tiny mono">{s.id}</span> <Status s={s.status} />
        <span className="faint small">{s.transport === "stdio" ? "Local (stdio)" : "Remote (HTTP)"}</span><span className="faint tiny mono">{s.transport === "stdio" ? `${s.command} ${(s.args || []).join(" ")}` : s.url}</span>
        <span className="faint tiny right">hashes checked {ago(s.refreshed_at)}</span>
      </div>
      {s.description && <div className="muted small">{s.description}</div>}
      {s.context_env && <div className="faint tiny">Receives the calling team and project, so each team only reaches its own files.</div>}
      {s.last_error && <div className="error small">Connection error: {s.last_error}</div>}
      <table>
        <thead><tr><th>Tool</th><th>What it does</th><th title="Read-only tools cannot change anything">Effect</th>
          <th title="Hash of the tool name, description and schema when an admin approved it">Approved hash</th><th>Live hash</th><th>Status</th><th /></tr></thead>
        <tbody>
          {tools.map((t) => (
            <tr key={t.name} className={t.status !== "approved" ? "row-warn" : ""}>
              <td>{toolLabel(`mcp.${s.id}.${t.name}`).split(" · ")[1]}<div className="faint tiny mono">{t.name}</div></td>
              <td className="small">{t.description}</td>
              <td><span className={`chip ${t.side_effect ? "warn-chip" : ""}`}>{t.side_effect ? "changes things" : "read-only"}</span></td>
              <td className="mono tiny">{t.pinned_hash?.slice(0, 10) || "—"}</td>
              <td className="mono tiny">{t.current_hash?.slice(0, 10)}</td>
              <td><Status s={t.status} /></td>
              <td>{isAdmin && t.status !== "approved" && <button onClick={() => act(() => api(`/mcp/tools/${s.id}/${t.name}/approve`, { method: "POST" }), "Approved new tool definition").then(onChange)}>Approve</button>}</td>
            </tr>))}
        </tbody>
      </table>
      {isAdmin && <div className="row">
        <button onClick={() => act(() => api(`/mcp/servers/${s.id}/refresh`, { method: "POST" }), "Re-checked tool definitions").then(onChange)}>Re-check hashes</button>
        <button onClick={() => act(() => api(`/mcp/servers/${s.id}/status?active=${s.status !== "active"}`, { method: "POST" }),
          s.status === "active" ? "Server disabled" : "Server enabled").then(onChange)}>{s.status === "active" ? "Disable" : "Enable"}</button>
        <button onClick={() => setEditing(!editing)}>Secrets</button>
        <ConfirmButton className="right" confirm={`Remove ${s.id} and its grants?`} onConfirm={() => act(() => api(`/mcp/servers/${s.id}`, { method: "DELETE" }), "Server removed").then(onChange)}>Remove</ConfirmButton>
      </div>}
      {editing && <div>
        <label className="field">Environment secrets — one <code>ENV_VAR=provider</code> per line. The provider's key is stored encrypted under
          Admin › Provider keys and injected only when the server starts; agents never see it.
          <textarea rows={3} className="mono" value={secrets} onChange={(e) => setSecrets(e.target.value)} placeholder="GITHUB_TOKEN=github" /></label>
        <button className="primary" onClick={() => act(() => api(`/mcp/servers/${s.id}`, { method: "PATCH", body: { secret_env: parseMap(secrets) } }), "Secrets saved").then(() => { setEditing(false); onChange(); })}>Save secrets</button>
      </div>}
    </div>
  );
}

export default function Tools() {
  const { me } = useApp();
  const [d, setD] = useState(null);
  const [skills, setSkills] = useState([]);
  const [grant, setGrant] = useState({ team_id: "", tool: "", needs_approval: false });
  const [srv, setSrv] = useState({ id: "", name: "", transport: "stdio", command: "python", args: "", url: "", secrets: "" });
  const [probe, setProbe] = useState(null);
  const isAdmin = me.is_owner || me.teams.some((t) => ["admin", "owner"].includes(t.role));
  const load = () => { api("/tools").then(setD).catch(() => {}); api("/skills").then(setSkills).catch(() => {}); };
  useEffect(() => { load(); }, []);
  if (!d) return <div className="muted">Loading…</div>;
  const def = () => ({ transport: srv.transport, command: srv.command, args: srv.args.split(" ").filter(Boolean), url: srv.url,
    secret_env: parseMap(srv.secrets) });

  return (
    <div>
      <div className="page-head"><h2>Tools &amp; MCP</h2>
        <span className="muted">every tool call goes through one gateway: grants, pinned definitions, approvals, metering</span></div>
      <h3>MCP servers</h3>
      {d.servers.map((s) => <ServerCard key={s.id} s={s} tools={d.mcp_tools.filter((t) => t.server_id === s.id)} onChange={load} isAdmin={isAdmin} />)}

      {isAdmin && <div className="card">
        <h3>Connect an MCP server</h3>
        <p className="muted small">Local servers run as a subprocess (stdio); remote servers use streamable HTTP. Test the connection first —
          new tools start as <b>Needs review</b> until an admin approves their definitions.</p>
        <div className="row">
          <label className="field">Id<input value={srv.id} onChange={(e) => setSrv({ ...srv, id: e.target.value })} placeholder="github" /></label>
          <label className="field">Name<input value={srv.name} onChange={(e) => setSrv({ ...srv, name: e.target.value })} placeholder="GitHub" /></label>
          <label className="field">Transport<Select value={srv.transport} onChange={(e) => setSrv({ ...srv, transport: e.target.value })}><option value="stdio">stdio (local)</option><option value="http">HTTP (remote)</option></Select></label>
        </div>
        {srv.transport === "stdio" ? <div className="row">
          <label className="field">Command<input value={srv.command} onChange={(e) => setSrv({ ...srv, command: e.target.value })} /></label>
          <label className="field grow">Arguments<input value={srv.args} onChange={(e) => setSrv({ ...srv, args: e.target.value })} placeholder="-m mcp_server_time" /></label>
        </div> : <label className="field">Server URL<input value={srv.url} onChange={(e) => setSrv({ ...srv, url: e.target.value })} placeholder="https://example.com/mcp" /></label>}
        <label className="field">Secrets (optional, <code>ENV_VAR=provider</code> per line)<textarea rows={2} className="mono" value={srv.secrets} onChange={(e) => setSrv({ ...srv, secrets: e.target.value })} /></label>
        <div className="row">
          <button onClick={async () => { setProbe({ loading: true }); setProbe(await api("/mcp/probe", { method: "POST", body: def() }).catch((e) => ({ ok: false, error: e.message }))); }}>Test connection</button>
          <button className="primary" disabled={!srv.id || !srv.name}
            onClick={() => act(() => api("/mcp/servers", { method: "POST", body: { id: srv.id, name: srv.name, ...def() } }), "Server added — approve its tools below").then(() => { setProbe(null); load(); })}>Save server</button>
        </div>
        {probe?.loading && <div className="muted small"><span className="spinner" /> connecting…</div>}
        {probe && !probe.loading && (probe.ok
          ? <div className="notice">Connected — {probe.tools.length} tool(s): {probe.tools.map((t) => titleCase(t.name)).join(", ")}</div>
          : <div className="notice bad">Could not connect: {probe.error}</div>)}
      </div>}

      <div className="grid2">
        <div className="card">
          <h3>Team grants</h3>
          <p className="muted small">An agent can use an MCP tool only if its spec lists it <i>and</i> its team holds a grant. Patterns like <code>mcp.workspace.*</code> cover every tool of a server.</p>
          <table><thead><tr><th>Team</th><th>Tool pattern</th><th title="Every call waits for a person">Approval</th><th /></tr></thead><tbody>
            {d.grants.map((g) => (
              <tr key={g.id}><td>{g.team_name}</td><td>{toolLabel(g.tool)}<div className="faint tiny mono">{g.tool}</div></td><td>{g.needs_approval ? "every call" : "not required"}</td>
                <td>{isAdmin && <ConfirmButton className="link" confirm="Revoke?" onConfirm={() => act(() => api(`/grants/${g.id}`, { method: "DELETE" }), "Grant revoked").then(load)}>revoke</ConfirmButton>}</td></tr>))}
          </tbody></table>
          {isAdmin && <div className="row">
            <Select value={grant.team_id} onChange={(e) => setGrant({ ...grant, team_id: e.target.value })} aria-label="Team">
              <option value="">team…</option>{me.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</Select>
            <input placeholder="mcp.server.tool or mcp.server.*" value={grant.tool} onChange={(e) => setGrant({ ...grant, tool: e.target.value })} aria-label="Tool pattern" />
            <label className="small"><input type="checkbox" checked={grant.needs_approval} onChange={(e) => setGrant({ ...grant, needs_approval: e.target.checked })} /> require approval</label>
            <button disabled={!grant.team_id || !grant.tool} onClick={() => act(() => api("/grants", { method: "POST", body: grant }), "Grant saved").then(load)}>Grant</button>
          </div>}
        </div>
        <div className="card">
          <h3>Built-in tools</h3>
          <table><thead><tr><th>Tool</th><th>What it does</th><th>Effect</th></tr></thead><tbody>{d.builtin.map((t) => (
            <tr key={t.name}><td>{toolLabel(t.name)}<div className="faint tiny mono">{t.name}</div></td><td className="small">{t.description}</td>
              <td><span className={`chip ${t.side_effect ? "warn-chip" : ""}`}>{t.side_effect ? "needs approval" : "safe"}</span></td></tr>))}
          </tbody></table>
          <h3>Skills</h3>
          <p className="muted small">Edited skills are disabled until an admin approves the new content.</p>
          <table><tbody>{skills.map((s) => (
            <tr key={s.name}><td>{titleCase(s.name)}</td><td className="small">{s.description}</td>
              <td>{s.enabled ? <Status s="approved" /> : isAdmin ? <button onClick={() => act(() => api(`/skills/${s.name}/approve`, { method: "POST" }), "Skill approved").then(load)}>Approve content</button> : <Status s="needs_review" />}</td></tr>))}
          </tbody></table>
        </div>
      </div>
    </div>
  );
}
