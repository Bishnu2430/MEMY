import { useEffect, useRef, useState } from "react";
import { ACTIVE, TERMINAL, api, canStop, describe, money, num } from "../api.js";
import { useApp } from "../App.jsx";
import { ArtifactView, AttachmentChip, ConfirmButton, KnowledgeList, LiveStep, Status, act, useAgents, useInterval } from "../components.jsx";
import CopyButton from "../copy.jsx";
import Icon from "../icons.jsx";
import Markdown from "../Markdown.jsx";

const EXAMPLES = [
  "@coder write a function that checks if a string is a palindrome, with tests",
  "Explain the CAP theorem and show availability as $A = \\frac{MTBF}{MTBF + MTTR}$",
  "@diagrammer draw a sequence diagram of an OAuth login",
  "@designer design an SVG logo for a coffee shop called Ember",
  "@narrator turn this into a 15-second voiceover: our store now ships worldwide",
];

function Turn({ id, onRerun }) {
  const [t, setT] = useState(null);
  const [live, setLive] = useState(null);
  const [know, setKnow] = useState(null);
  const active = !t || ACTIVE.includes(t.status) || t.status === "awaiting_user";
  useInterval(async () => {
    if (t && !active && know) return;
    try {
      const d = await api(`/tasks/${id}`);
      setT(d);
      if (ACTIVE.includes(d.status)) setLive(await api(`/live?task_id=${id}`));
      if (d.knowledge_count) setKnow(await api(`/tasks/${id}/knowledge`));
      else if (!ACTIVE.includes(d.status)) setKnow([]);
    } catch { /* transient */ }
  }, 1500, [id, active, !!know]);
  if (!t) return null;
  const working = (live?.steps || []).filter((s) => ["running", "reviewing", "waiting_approval", "ready"].includes(s.status));
  const taskEvent = live?.tasks?.[0]?.last_event;
  const agents = [...new Set(t.steps.map((s) => s.agent_id))];
  const choose = async (agent) => { await api(`/tasks/${id}/choose`, { method: "POST", body: { agent_id: agent } }); };
  const running = ACTIVE.includes(t.status);
  const inputs = t.artifacts.filter((a) => a.role === "input");
  const outputs = t.artifacts.filter((a) => a.role !== "input" && a.kind !== "mermaid");
  return (
    <div className="turn">
      <div className="user-msg">
        <div className="bubble">
          <Markdown>{t.request}</Markdown>
          {inputs.length > 0 && <div className="att-row">{inputs.map((a) => <AttachmentChip key={a.id} a={a} />)}</div>}
        </div>
      </div>
      <div className="agent-msg">
        <div className="agent-head">
          <span className={`mark ${running ? "busy" : ""}`} />
          {agents.length ? agents.map((a) => <span key={a} className="chip agent">@{a}</span>) : <span>choosing an agent…</span>}
          {t.steps.length > 1 && <span className="faint">· {t.steps.filter((s) => s.status === "done").length} of {t.steps.length} steps</span>}
          {t.status !== "completed" && <Status s={t.status} />}
          <span className="faint" title="Cost and tokens used by this task so far">· {money(t.cost?.cost_usd)} · {num((t.cost?.tokens_in || 0) + (t.cost?.tokens_out || 0))} tokens</span>
          <span className="right row" style={{ margin: 0 }}>
            {canStop(t.status) && <ConfirmButton className="btn-stop" confirm="Confirm stop" title="Stop all agents working on this task"
              onConfirm={() => act(() => api(`/tasks/${id}/cancel`, { method: "POST" }), "Task stopped")}>Stop</ConfirmButton>}
            <a className="small" href={`#/tasks/${id}`}>Open trace</a>
          </span>
        </div>
        {running && (
          <div className="trace-box">
            {working.length ? working.map((s) => <LiveStep key={s.id} s={s} />)
              : <div className="live-step"><div className="live-op"><span className="spinner" /><span className="shimmer">{describe(taskEvent) || "Starting…"}</span></div></div>}
          </div>
        )}
        {t.status === "awaiting_user" && (
          <div className="notice warn choice">
            <span>I'm not sure which agent fits best. Pick one:</span>
            {(t.router?.shortlist || []).slice(0, 3).map((a) => <button key={a.id} onClick={() => choose(a.id)}>@{a.id}</button>)}
          </div>
        )}
        {t.status === "paused_budget" && <div className="notice warn">Paused — the task budget was reached. <a href={`#/tasks/${id}`}>Raise the budget</a> to continue.</div>}
        {t.status === "needs_attention" && <div className="notice bad">Needs attention: {t.error} <a href={`#/tasks/${id}`}>Resolve</a></div>}
        {t.status === "cancelled" && <div className="notice">Stopped{t.error ? ` — ${t.error}` : ""}.</div>}
        {t.approvals?.some((a) => a.status === "pending") && (
          <div className="notice warn">Waiting for approval — <a href={`#/tasks/${id}`}>review the exact action</a>.</div>
        )}
        {t.result && <div className="answer"><Markdown>{t.result}</Markdown></div>}
        {TERMINAL.includes(t.status) && (
          <div className="answer-actions">
            {t.result && <CopyButton text={t.result} label="Copy answer" />}
            {t.result && <a href={`data:text/markdown;charset=utf-8,${encodeURIComponent(t.result)}`} download={`${t.title.slice(0, 40).replace(/[^a-z0-9]+/gi, "-")}.md`}>Download .md</a>}
            <button onClick={() => act(() => api(`/tasks/${id}/rerun`, { method: "POST" }), "Started again").then((n) => onRerun?.(n.id))} title="Run the same request again (with the same files)">Run again</button>
          </div>
        )}
        {t.status === "completed" && outputs.length > 0 && (
          <div className="artifacts">{outputs.map((a) => <ArtifactView key={a.id} a={a} compact />)}</div>
        )}
        {know?.length > 0 && (
          <details className="know-chip">
            <summary><Icon name="knowledge" size={14} /> {know.length} knowledge entr{know.length === 1 ? "y" : "ies"} saved from this task</summary>
            <KnowledgeList items={know} />
          </details>
        )}
      </div>
    </div>
  );
}

export default function Chat() {
  const { team, role, me } = useApp();
  const agents = useAgents();
  const [text, setText] = useState("");
  const [ids, setIds] = useState([]);
  const [err, setErr] = useState("");
  const [suggest, setSuggest] = useState([]);
  const [priv, setPriv] = useState(false);
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const [fresh, setFresh] = useState(false);
  const endRef = useRef(null);
  const scrollRef = useRef(null);
  const [atBottom, setAtBottom] = useState(true);
  const boxRef = useRef(null);
  const fileRef = useRef(null);

  const loadHistory = () => team && api(`/tasks?team_id=${team}&limit=6`).then((r) => {
    const latest = r.reverse().map((t) => t.id);
    setIds((cur) => (cur.join() === latest.join() ? cur : latest));
  }).catch(() => {});
  // Show the team's latest tasks (including ones started from the CLI or another tab) unless a fresh thread was started.
  useInterval(() => { if (!fresh) loadHistory(); }, 4000, [team, fresh]);
  useEffect(() => {
    const onFresh = () => { setFresh(true); setIds([]); setText(""); setFiles([]); setTimeout(() => boxRef.current?.focus(), 50); };
    window.addEventListener("memy-new-chat", onFresh);
    return () => window.removeEventListener("memy-new-chat", onFresh);
  }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [ids.length]);

  const addFiles = (list) => {
    for (const f of list) {
      const local = { key: Math.random().toString(36).slice(2), name: f.name, size: f.size, uploading: true,
        kind: f.type.startsWith("image/") ? "image" : "file", preview_url: f.type.startsWith("image/") ? URL.createObjectURL(f) : null };
      setFiles((x) => [...x, local]);
      const form = new FormData();
      form.append("team_id", team);
      form.append("file", f);
      api("/uploads", { method: "POST", form })
        .then((r) => setFiles((x) => x.map((i) => (i.key === local.key ? { ...i, ...r, uploading: false } : i))))
        .catch((e) => setFiles((x) => x.map((i) => (i.key === local.key ? { ...i, uploading: false, error: e.message } : i))));
    }
  };
  const onChange = (v) => {
    setText(v);
    const el = boxRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 260)}px`; }
    const m = /(^|\s)@([a-z0-9_-]*)$/.exec(v);
    setSuggest(m ? agents.filter((a) => a.enabled && a.id.startsWith(m[2])).slice(0, 7) : []);
  };
  const pick = (id) => { setText(text.replace(/@([a-z0-9_-]*)$/, `@${id} `)); setSuggest([]); boxRef.current?.focus(); };
  const uploading = files.some((f) => f.uploading);
  const send = async () => {
    if (!text.trim() || uploading) return;
    setErr("");
    try {
      const t = await api("/tasks", { method: "POST", body: { team_id: team, text, private: priv,
        attachment_ids: files.filter((f) => f.artifact_id).map((f) => f.artifact_id) } });
      setText(""); setSuggest([]); setFiles([]);
      setIds((x) => [...x, t.id]);
    } catch (e) { setErr(e.message); }
  };

  const composer = (
    <div className={`composer ${drag ? "drag" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
      onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles([...e.dataTransfer.files]); }}>
      {suggest.length > 0 && (
        <div className="suggest" role="listbox">
          {suggest.map((a) => <div key={a.id} role="option" onClick={() => pick(a.id)}><b>@{a.id}</b><span className="muted small">{a.description}</span></div>)}
        </div>
      )}
      {err && <div className="error small">{err}</div>}
      {files.length > 0 && (
        <div className="att-row">{files.map((f) => <AttachmentChip key={f.key} a={f} onRemove={() => setFiles((x) => x.filter((i) => i.key !== f.key))} />)}</div>
      )}
      <textarea ref={boxRef} value={text} onChange={(e) => onChange(e.target.value)} disabled={role === "viewer"} autoFocus
        aria-label="Message"
        placeholder={role === "viewer" ? "You have view-only access in this team" : "What should your agents work on?  Type @ to pick an agent"}
        onPaste={(e) => { const imgs = [...e.clipboardData.files]; if (imgs.length) { e.preventDefault(); addFiles(imgs); } }}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} rows={2} />
      <div className="composer-row">
        <input ref={fileRef} type="file" multiple hidden onChange={(e) => { addFiles([...e.target.files]); e.target.value = ""; }}
          accept="image/*,audio/*,video/*,.pdf,.docx,.txt,.md,.csv,.json,.py,.js,.ts,.tsx,.jsx,.html,.css,.sql,.yaml,.yml" />
        <button className="ghost icon-btn" onClick={() => fileRef.current?.click()} disabled={role === "viewer"}
          title="Attach images, PDFs, documents, code or audio (or drag and drop / paste)" aria-label="Attach files">
          <Icon name="clip" size={18} />
        </button>
        <label className="toggle" title="Private tasks are visible only to you and team admins">
          <input type="checkbox" checked={priv} onChange={(e) => setPriv(e.target.checked)} /> Private</label>
        <span className="faint tiny hide-sm">Enter to send · Shift+Enter for a new line</span>
        <button className="send right" onClick={send} disabled={role === "viewer" || !text.trim() || uploading}
          title={uploading ? "Waiting for files to finish reading" : "Send (Enter)"} aria-label="Send">
          <Icon name="up" size={18} />
        </button>
      </div>
    </div>
  );

  if (ids.length === 0) {
    return (
      <div className="chat">
        <div className="chat-scroll">
          <div className="hero">
            <h1><span className="brand-mark" /> Good to see you, {me.name}</h1>
            <p className="muted">Describe a task and attach any files. The router picks the right agent, or the supervisor plans
              it across several. Every step is checkpointed, verified, costed and saved to your team's knowledge.</p>
            <div style={{ width: "100%", marginTop: 24 }}>{composer}</div>
            <div className="examples">
              {EXAMPLES.map((x) => <button key={x} className="example" onClick={() => { setText(x); boxRef.current?.focus(); }}>{x}</button>)}
            </div>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="chat" style={{ position: "relative" }}>
      <div className="chat-scroll" ref={scrollRef}
        onScroll={(e) => { const el = e.currentTarget; setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 120); }}>
        <div className="chat-col">
          {ids.map((id) => <Turn key={id} id={id} onRerun={(nid) => setIds((x) => [...x, nid])} />)}
          <div ref={endRef} />
        </div>
      </div>
      {!atBottom && <button className="scroll-latest" onClick={() => endRef.current?.scrollIntoView({ behavior: "smooth" })}>↓ Jump to latest</button>}
      <div className="composer-wrap">
        {composer}
        <div className="composer-hint">Agents can make mistakes. Actions with side effects always wait for approval.</div>
      </div>
    </div>
  );
}
