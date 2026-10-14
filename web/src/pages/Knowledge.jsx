import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import Markdown from "../Markdown.jsx";

export default function Knowledge() {
  const { team, role } = useApp();
  const [sources, setSources] = useState([]);
  const [filter, setFilter] = useState("");
  const [memory, setMemory] = useState([]);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  const [url, setUrl] = useState("");
  const [purpose, setPurpose] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    if (!team) return;
    api(`/knowledge/sources?team_id=${team}${filter ? `&status=${filter}` : ""}`).then(setSources).catch(() => {});
    api("/memory").then(setMemory).catch(() => {});
  };
  useEffect(() => { load(); }, [team, filter]);

  const run = async (fn) => {
    setBusy(true); setMsg("");
    try { const r = await fn(); setMsg(JSON.stringify(r)); load(); } catch (e) { setMsg(e.message); }
    setBusy(false);
  };
  const upload = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const form = new FormData();
    form.append("team_id", team);
    form.append("file", f);
    run(() => api("/knowledge/upload", { method: "POST", form }));
  };
  const writer = role && role !== "viewer";

  return (
    <div>
      <div className="page-head"><h2>Knowledge</h2><span className="muted">team-scoped by row-level security · hybrid full-text + vector search</span></div>
      <div className="grid2">
        <div className="card">
          <h3>Search</h3>
          <div className="row"><input className="grow" value={q} onChange={(e) => setQ(e.target.value)} placeholder="search team knowledge"
            onKeyDown={(e) => e.key === "Enter" && api(`/knowledge/search?team_id=${team}&q=${encodeURIComponent(q)}`).then(setHits)} />
            <button onClick={() => api(`/knowledge/search?team_id=${team}&q=${encodeURIComponent(q)}`).then(setHits)}>Search</button></div>
          {hits && (hits.length ? hits.map((h, i) => (
            <div key={h.chunk_id} className="hit">
              <div className="small"><b>[S{i + 1}] {h.title}</b> <span className="muted">trust {h.trust} · {h.kind} · score {h.score}</span> {h.url && <a href={h.url} target="_blank" rel="noreferrer">source</a>}</div>
              <Markdown className="small">{h.content.slice(0, 600)}</Markdown>
            </div>)) : <div className="muted">No results in this team's scope.</div>)}
        </div>
        {writer && (
          <div className="card">
            <h3>Add knowledge</h3>
            <label className="small">Upload PDF, DOCX, HTML, Markdown or code<input type="file" onChange={upload} disabled={busy} /></label>
            <div className="row"><input className="grow" placeholder="https://… page to scrape" value={url} onChange={(e) => setUrl(e.target.value)} />
              <input placeholder="purpose (for scoring)" value={purpose} onChange={(e) => setPurpose(e.target.value)} />
              <button disabled={busy || !url} onClick={() => run(() => api("/knowledge/url", { method: "POST", body: { team_id: team, url, purpose } }))}>Scrape</button></div>
            <input placeholder="title" value={title} onChange={(e) => setTitle(e.target.value)} />
            <textarea rows={4} placeholder="paste text…" value={text} onChange={(e) => setText(e.target.value)} />
            <button disabled={busy || !text || !title} onClick={() => run(() => api("/knowledge/text", { method: "POST", body: { team_id: team, title, text } }))}>Add text</button>
            {busy && <div className="muted"><span className="spinner" /> working…</div>}
            {msg && <pre className="small">{msg}</pre>}
          </div>
        )}
      </div>
      {memory.length > 0 && (
        <div className="card">
          <h3>Pending memory proposals</h3>
          {memory.map((m) => (
            <div key={m.id} className="row">
              <span className="grow">{m.fact} <span className="muted small">— @{m.agent_id} {m.source_url && <a href={m.source_url}>source</a>}</span></span>
              <button onClick={() => api(`/memory/${m.id}`, { method: "POST", body: { decision: "approve" } }).then(load)}>Approve</button>
              <button onClick={() => api(`/memory/${m.id}`, { method: "POST", body: { decision: "reject" } }).then(load)}>Reject</button>
            </div>
          ))}
        </div>
      )}
      <div className="card">
        <div className="row"><h3>Sources</h3>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">all</option><option value="live">live</option><option value="quarantined">quarantine queue</option><option value="superseded">superseded</option>
          </select></div>
        <table>
          <thead><tr><th>Title</th><th>Kind</th><th>Trust</th><th>Status</th><th>Chunks</th><th>Added</th><th></th></tr></thead>
          <tbody>{sources.map((s) => (
            <tr key={s.id}>
              <td>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}
                {s.scores?.summary && <div className="muted small">{s.scores.summary}</div>}
                {s.scores?.filters_failed?.length > 0 && <div className="warn-text small">filters: {s.scores.filters_failed.join(", ")}</div>}</td>
              <td>{s.kind}</td><td>{Number(s.trust_score).toFixed(2)}</td><td>{s.status}</td><td>{s.chunks}</td>
              <td className="muted small">{ago(s.created_at)}</td>
              <td className="nowrap">{role === "admin" || role === "owner" ? <>
                {s.status !== "live" && <button className="link" onClick={() => api(`/knowledge/sources/${s.id}/status`, { method: "POST", body: { status: "live" } }).then(load)}>release</button>}
                {s.status === "live" && <button className="link" onClick={() => api(`/knowledge/sources/${s.id}/status`, { method: "POST", body: { status: "quarantined" } }).then(load)}>quarantine</button>}
                <button className="link" onClick={() => api(`/knowledge/sources/${s.id}`, { method: "DELETE" }).then(load)}>roll back</button>
              </> : null}</td>
            </tr>))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
