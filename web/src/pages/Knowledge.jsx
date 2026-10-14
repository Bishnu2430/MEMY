import { useEffect, useState } from "react";
import { KNOWLEDGE_KIND, ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { ConfirmButton, Status, act } from "../components.jsx";
import Markdown from "../Markdown.jsx";
import Select from "../select.jsx";

function SourceDetail({ id }) {
  const [s, setS] = useState(null);
  useEffect(() => { api(`/knowledge/sources/${id}`).then(setS).catch((e) => setS({ error: e.message })); }, [id]);
  if (!s) return <div className="card muted">Loading…</div>;
  if (s.error) return <div className="card error">{s.error}</div>;
  return (
    <div className="card">
      <div className="row"><a href="#/knowledge" className="small">← All knowledge</a></div>
      <h3>{s.title}</h3>
      <div className="row small">
        <span className="chip">{KNOWLEDGE_KIND[s.kind] || s.kind}</span><Status s={s.status} />
        <span title="Quality / provenance score used to rank and filter">trust {Number(s.trust_score).toFixed(2)}</span>
        {s.agent_id && <span>recorded by <b>@{s.agent_id}</b></span>}
        {s.task_id && <span>from task <a href={`#/tasks/${s.task_id}`}>{s.task_title?.slice(0, 60) || "open"}</a></span>}
        {s.url && <a href={s.url} target="_blank" rel="noreferrer">source page</a>}
        <span className="faint">{ago(s.created_at)}</span>
      </div>
      {s.scores?.summary && <p className="muted small">{s.scores.summary}</p>}
      <h4>Stored chunks ({s.chunks.length}) — what agents retrieve</h4>
      {s.chunks.map((c) => (
        <div key={c.ord} className="chunk">
          <div className="faint tiny">chunk {c.ord + 1} · {c.embedded ? "embedded for semantic search" : "keyword search only"}</div>
          <Markdown>{c.content}</Markdown>
        </div>
      ))}
    </div>
  );
}

export default function Knowledge({ id }) {
  const { team, role } = useApp();
  const [sources, setSources] = useState([]);
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [memory, setMemory] = useState([]);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  const [url, setUrl] = useState("");
  const [purpose, setPurpose] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    if (!team) return;
    api(`/knowledge/sources?team_id=${team}${status ? `&status=${status}` : ""}`).then(setSources).catch(() => {});
    api("/memory").then(setMemory).catch(() => {});
  };
  useEffect(() => { load(); }, [team, status]);
  if (id) return <SourceDetail id={id} />;

  const run = async (fn, ok) => { setBusy(true); try { await act(fn, ok); load(); } catch { /* toast */ } setBusy(false); };
  const upload = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const form = new FormData();
    form.append("team_id", team);
    form.append("file", f);
    run(() => api("/knowledge/upload", { method: "POST", form }), `Added ${f.name}`);
    e.target.value = "";
  };
  const search = () => q.trim() && api(`/knowledge/search?team_id=${team}&q=${encodeURIComponent(q)}`).then(setHits);
  const writer = role && role !== "viewer";
  const isAdmin = role === "admin" || role === "owner";
  const shown = sources.filter((s) => !kind || s.kind === kind);
  const counts = sources.reduce((m, s) => ({ ...m, [s.kind]: (m[s.kind] || 0) + 1 }), {});

  return (
    <div>
      <div className="page-head"><h2>Knowledge</h2>
        <span className="muted">your team's shared memory — isolated from other teams at the database level, searched by keywords and meaning</span></div>
      <div className="grid2">
        <div className="card">
          <h3>Search</h3>
          <div className="row"><input className="grow" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search what your team knows"
            onKeyDown={(e) => e.key === "Enter" && search()} aria-label="Search knowledge" />
            <button onClick={search}>Search</button></div>
          {hits && (hits.length ? hits.map((h, i) => (
            <div key={h.chunk_id} className="hit">
              <div className="small"><b>[S{i + 1}] <a href={`#/knowledge/${h.source_id}`}>{h.title}</a></b>
                <span className="faint"> · {KNOWLEDGE_KIND[h.kind] || h.kind} · trust {h.trust} · relevance {h.score}</span></div>
              <Markdown className="small">{h.content.slice(0, 600)}</Markdown>
            </div>)) : <div className="muted small">No results in this team's knowledge.</div>)}
        </div>
        {writer && (
          <div className="card">
            <h3>Add knowledge</h3>
            <label className="field">Upload a document (PDF, DOCX, HTML, Markdown, text or code)<input type="file" onChange={upload} disabled={busy} /></label>
            <div className="row">
              <label className="field grow">Web page<input placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} /></label>
              <label className="field">Why it matters (used for quality scoring)<input placeholder="e.g. payment API reference" value={purpose} onChange={(e) => setPurpose(e.target.value)} /></label>
              <button disabled={busy || !url} onClick={() => run(() => api("/knowledge/url", { method: "POST", body: { team_id: team, url, purpose } }), "Page fetched and scored")}>Fetch page</button>
            </div>
            <label className="field">Note title<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Coding conventions" /></label>
            <textarea rows={3} placeholder="Paste text…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Note text" />
            <button disabled={busy || !text || !title} onClick={() => run(() => api("/knowledge/text", { method: "POST", body: { team_id: team, title, text } }), "Note added")}>Add note</button>
            {busy && <div className="muted small"><span className="spinner" /> reading, chunking and embedding…</div>}
          </div>
        )}
      </div>
      {memory.length > 0 && (
        <div className="card">
          <h3>Memory proposals waiting for review</h3>
          <p className="muted small">Agents can propose facts, but nothing enters shared memory until a person approves it.</p>
          {memory.map((m) => (
            <div key={m.id} className="row">
              <span className="grow">{m.fact} <span className="faint small">— proposed by @{m.agent_id}{m.source_url && <> · <a href={m.source_url}>source</a></>}</span></span>
              {writer && <><button onClick={() => run(() => api(`/memory/${m.id}`, { method: "POST", body: { decision: "approve" } }), "Approved into memory")}>Approve</button>
                <button onClick={() => run(() => api(`/memory/${m.id}`, { method: "POST", body: { decision: "reject" } }), "Rejected")}>Reject</button></>}
            </div>
          ))}
        </div>
      )}
      <div className="card">
        <div className="row"><h3 style={{ margin: 0 }}>Sources</h3>
          <Select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Filter by kind">
            <option value="">all kinds ({sources.length})</option>
            {Object.entries(counts).map(([k, n]) => <option key={k} value={k}>{KNOWLEDGE_KIND[k] || k} ({n})</option>)}
          </Select>
          <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
            <option value="">any status</option><option value="live">live</option><option value="quarantined">quarantine queue</option><option value="superseded">superseded</option>
          </Select></div>
        <table>
          <thead><tr><th>Title</th><th>Kind</th><th title="Quality / provenance score">Trust</th><th>Status</th><th title="Searchable pieces">Chunks</th><th>Added</th><th /></tr></thead>
          <tbody>{shown.map((s) => (
            <tr key={s.id}>
              <td><a href={`#/knowledge/${s.id}`}>{s.title}</a>
                {s.scores?.summary && <div className="faint small">{s.scores.summary}</div>}
                {s.scores?.filters_failed?.length > 0 && <div className="warn-text small">failed filters: {s.scores.filters_failed.join(", ")}</div>}</td>
              <td className="small">{KNOWLEDGE_KIND[s.kind] || s.kind}</td><td>{Number(s.trust_score).toFixed(2)}</td><td><Status s={s.status} /></td><td>{s.chunks}</td>
              <td className="faint small nowrap">{ago(s.created_at)}</td>
              <td className="nowrap">{isAdmin && <>
                {s.status !== "live" && <button className="link" onClick={() => run(() => api(`/knowledge/sources/${s.id}/status`, { method: "POST", body: { status: "live" } }), "Released")}>release</button>}
                {s.status === "live" && <button className="link" onClick={() => run(() => api(`/knowledge/sources/${s.id}/status`, { method: "POST", body: { status: "quarantined" } }), "Quarantined")}>quarantine</button>}
                <ConfirmButton className="link" confirm="Delete?" onConfirm={() => run(() => api(`/knowledge/sources/${s.id}`, { method: "DELETE" }), "Source and its chunks removed")}>delete</ConfirmButton>
              </>}</td>
            </tr>))}
          </tbody>
        </table>
        {shown.length === 0 && <div className="muted small">Nothing here yet. Knowledge is added when you upload or attach files, when agents fetch pages, and automatically after every task.</div>}
      </div>
    </div>
  );
}
