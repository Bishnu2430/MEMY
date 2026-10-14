import { useEffect, useRef, useState } from "react";
import { ACTIVE, api, describe, money, num } from "../api.js";
import { useApp } from "../App.jsx";
import { ArtifactView, LiveStep, Status, useAgents, useInterval } from "../components.jsx";
import Icon from "../icons.jsx";
import Markdown from "../Markdown.jsx";

const EXAMPLES = [
  "@coder write a function that checks if a string is a palindrome, with tests",
  "Explain the CAP theorem and show availability as $A = \\frac{MTBF}{MTBF + MTTR}$",
  "@diagrammer draw a sequence diagram of an OAuth login",
  "Research pgvector using https://github.com/pgvector/pgvector, write a Python helper that builds a cosine-distance query with tests, and diagram the flow",
  "@ops post 'deploy at 5pm' to the general channel",
];

function Turn({ id, onChange }) {
  const [t, setT] = useState(null);
  const [live, setLive] = useState(null);
  const active = !t || ACTIVE.includes(t.status) || t.status === "awaiting_user";
  useInterval(async () => {
    if (t && !active) return;
    try {
      const d = await api(`/tasks/${id}`);
      setT(d);
      if (ACTIVE.includes(d.status)) setLive(await api(`/live?task_id=${id}`));
    } catch { /* transient */ }
  }, 1500, [id, active]);
  if (!t) return null;
  const working = (live?.steps || []).filter((s) => ["running", "reviewing", "waiting_approval", "ready"].includes(s.status));
  const taskEvent = live?.tasks?.[0]?.last_event;
  const agents = [...new Set(t.steps.map((s) => s.agent_id))];
  const choose = async (agent) => { await api(`/tasks/${id}/choose`, { method: "POST", body: { agent_id: agent } }); onChange?.(); };
  const running = ACTIVE.includes(t.status);
  return (
    <div className="turn">
      <div className="user-msg"><div className="bubble"><Markdown>{t.request}</Markdown></div></div>
      <div className="agent-msg">
        <div className="agent-head">
          <span className={`mark ${running ? "busy" : ""}`} />
          {agents.length ? agents.map((a) => <span key={a} className="chip agent">@{a}</span>) : <span>routing…</span>}
          {t.steps.length > 1 && <span className="faint">· {t.steps.filter((s) => s.status === "done").length}/{t.steps.length} steps</span>}
          {!["completed"].includes(t.status) && <Status s={t.status} />}
          <span className="faint">· {money(t.cost?.cost_usd)} · {num((t.cost?.tokens_in || 0) + (t.cost?.tokens_out || 0))} tok</span>
          <a className="right small" href={`#/tasks/${id}`}>View trace</a>
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
            {(t.router?.shortlist || []).slice(0, 3).map((a) => (
              <button key={a.id} onClick={() => choose(a.id)}>@{a.id}</button>
            ))}
          </div>
        )}
        {t.status === "paused_budget" && <div className="notice warn">Paused — the task budget was reached. Open the trace to raise it.</div>}
        {t.status === "needs_attention" && <div className="notice bad">Needs attention: {t.error}</div>}
        {t.approvals?.some((a) => a.status === "pending") && (
          <div className="notice warn">Waiting for your approval — <a href={`#/tasks/${id}`}>review the action</a>.</div>
        )}
        {t.result && <div className="answer"><Markdown>{t.result}</Markdown></div>}
        {t.status === "completed" && t.artifacts?.length > 0 && (
          <div className="artifacts">{t.artifacts.filter((a) => a.kind !== "mermaid").map((a) => <ArtifactView key={a.id} a={a} />)}</div>
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
  const endRef = useRef(null);
  const boxRef = useRef(null);

  const [fresh, setFresh] = useState(false);
  const loadHistory = () => team && api(`/tasks?team_id=${team}&limit=6`).then((r) => {
    const latest = r.reverse().map((t) => t.id);
    setIds((cur) => (cur.join() === latest.join() ? cur : latest));
  }).catch(() => {});
  // Show the team's latest tasks (including ones started from the CLI or another tab) unless a fresh thread was started.
  useInterval(() => { if (!fresh) loadHistory(); }, 4000, [team, fresh]);
  useEffect(() => {
    const fresh = () => { setFresh(true); setIds([]); setText(""); setTimeout(() => boxRef.current?.focus(), 50); };
    window.addEventListener("memy-new-chat", fresh);
    return () => window.removeEventListener("memy-new-chat", fresh);
  }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [ids.length]);

  const onChange = (v) => {
    setText(v);
    const m = /(^|\s)@([a-z0-9_-]*)$/.exec(v);
    setSuggest(m ? agents.filter((a) => a.enabled && a.id.startsWith(m[2])).slice(0, 6) : []);
  };
  const pick = (id) => { setText(text.replace(/@([a-z0-9_-]*)$/, `@${id} `)); setSuggest([]); boxRef.current?.focus(); };
  const send = async () => {
    if (!text.trim()) return;
    setErr("");
    try {
      const t = await api("/tasks", { method: "POST", body: { team_id: team, text, private: priv } });
      setText("");
      setSuggest([]);
      setIds((x) => [...x, t.id]);
    } catch (e) { setErr(e.message); }
  };

  const composer = (
    <div className="composer">
      {suggest.length > 0 && (
        <div className="suggest">
          {suggest.map((a) => <div key={a.id} onClick={() => pick(a.id)}><b>@{a.id}</b><span className="muted small">{a.description}</span></div>)}
        </div>
      )}
      {err && <div className="error small">{err}</div>}
      <textarea ref={boxRef} value={text} onChange={(e) => onChange(e.target.value)} disabled={role === "viewer"} autoFocus
        placeholder={role === "viewer" ? "Viewers can read but not create tasks" : "What should your agents work on?  Type @ to pick an agent"}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} rows={2} />
      <div className="composer-row">
        <label className="toggle"><input type="checkbox" checked={priv} onChange={(e) => setPriv(e.target.checked)} /> Private</label>
        <span className="faint tiny">Auto-routed unless you @mention an agent</span>
        <button className="send right" onClick={send} disabled={role === "viewer" || !text.trim()} title="Send (Enter)">
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
            <p className="muted">Describe a task and the router picks the right agent — or the supervisor plans it across several.
              Every step is checkpointed, verified and costed.</p>
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
    <div className="chat">
      <div className="chat-scroll">
        <div className="chat-col">
          {ids.map((id) => <Turn key={id} id={id} onChange={() => {}} />)}
          <div ref={endRef} />
        </div>
      </div>
      <div className="composer-wrap">
        {composer}
        <div className="composer-hint">Agents can make mistakes. Side-effecting actions always wait for your approval.</div>
      </div>
    </div>
  );
}
