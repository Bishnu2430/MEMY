import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import Markdown from "../Markdown.jsx";

function diff(a, b) {
  const A = (a || "").split("\n"), B = (b || "").split("\n");
  const setA = new Set(A), setB = new Set(B);
  return [...A.filter((l) => !setB.has(l)).map((l) => `- ${l}`), ...B.filter((l) => !setA.has(l)).map((l) => `+ ${l}`)].join("\n");
}

function Editor({ id, onSaved }) {
  const { team } = useApp();
  const [a, setA] = useState(null);
  const [yaml, setYaml] = useState("");
  const [msg, setMsg] = useState("");
  const [cmp, setCmp] = useState(null);
  const [cloneId, setCloneId] = useState("");
  const [play, setPlay] = useState("");
  const load = () => api(`/agents/${id}`).then((d) => { setA(d); setYaml(d.yaml); });
  useEffect(() => { load(); setCmp(null); setMsg(""); }, [id]);
  if (!a) return <div className="muted">Loading…</div>;
  const save = async () => {
    setMsg("");
    try { const r = await api(`/agents/${id}`, { method: "PUT", body: { yaml } }); setMsg(`Saved version ${r.version}`); load(); onSaved(); }
    catch (e) { setMsg(e.message); }
  };
  const rollback = async (v) => { await api(`/agents/${id}/rollback`, { method: "POST", body: { version: v } }); load(); onSaved(); };
  const clone = async () => {
    try { await api(`/agents/${id}/clone`, { method: "POST", body: { new_id: cloneId, team_id: team } }); setMsg(`Cloned to ${cloneId}`); onSaved(); }
    catch (e) { setMsg(e.message); }
  };
  const playground = async () => {
    const t = await api("/tasks", { method: "POST", body: { team_id: team, text: play, agent_id: id } });
    window.location.hash = `/tasks/${t.id}`;
  };
  return (
    <div className="card">
      <div className="row"><h3>{a.spec?.name} <span className="muted">@{id} · v{a.current_version}</span></h3></div>
      <Markdown>{a.spec?.description || ""}</Markdown>
      <textarea className="mono yaml" rows={22} value={yaml} onChange={(e) => setYaml(e.target.value)} />
      <div className="row">
        <button className="primary" onClick={save}>Save new version</button>
        <input placeholder="clone as id…" value={cloneId} onChange={(e) => setCloneId(e.target.value)} />
        <button onClick={clone} disabled={!cloneId}>Clone for my team</button>
        <button onClick={() => api(`/agents/${id}/enabled?enabled=${!a.enabled}`, { method: "POST" }).then(load)}>{a.enabled ? "Disable" : "Enable"}</button>
        <button onClick={() => api("/admin/controls", { method: "POST", body: { scope: `agent:${id}`, paused: true } }).then(onSaved)}>Kill switch: pause</button>
        <button onClick={() => api("/admin/controls", { method: "POST", body: { scope: `agent:${id}`, paused: false } }).then(onSaved)}>Resume</button>
      </div>
      {msg && <div className="muted">{msg}</div>}
      <h4>Playground</h4>
      <div className="row"><input className="grow" placeholder={a.spec?.examples?.[0] || "try a task on this agent"} value={play} onChange={(e) => setPlay(e.target.value)} />
        <button onClick={playground} disabled={!play}>Run</button></div>
      <h4>Versions</h4>
      <table><tbody>
        {a.versions.map((v) => (
          <tr key={v.version}><td>v{v.version}</td><td className="muted small">{ago(v.created_at)}</td>
            <td><button className="link" onClick={() => setCmp(v)}>diff vs current</button></td>
            <td>{v.version !== a.current_version && <button className="link" onClick={() => rollback(v.version)}>roll back to this</button>}</td></tr>
        ))}
      </tbody></table>
      {cmp && <pre className="diff">{diff(cmp.yaml, a.yaml) || "(identical)"}</pre>}
    </div>
  );
}

export default function Agents({ id }) {
  const [agents, setAgents] = useState([]);
  const load = () => api("/agents").then(setAgents).catch(() => {});
  useEffect(() => { load(); }, []);
  return (
    <div>
      <div className="page-head"><h2>Agents</h2><span className="muted">every agent is a versioned AgentSpec run by one executor</span></div>
      <div className="grid-side">
        <div className="card list">
          {agents.map((a) => (
            <a key={a.id} href={`#/agents/${a.id}`} className={`agent-row ${id === a.id ? "active" : ""}`}>
              <div><b>@{a.id}</b> {a.paused && <span className="badge warn">paused</span>} {!a.enabled && <span className="badge">disabled</span>}</div>
              <div className="muted small">{a.name} · {a.model?.primary} · {a.verification}</div>
              <div className="muted small">{a.stats?.done || 0}/{a.stats?.steps || 0} steps done · {a.stats?.retried || 0} retried</div>
            </a>
          ))}
        </div>
        {id ? <Editor id={id} onSaved={load} /> : <div className="card muted">Select an agent to edit its spec, view versions, or run it in the playground.</div>}
      </div>
    </div>
  );
}
