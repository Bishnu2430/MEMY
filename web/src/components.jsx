import { useEffect, useState } from "react";
import { api, artifactUrl, describe, elapsed, money, num } from "./api.js";
import Markdown from "./Markdown.jsx";

export function Status({ s }) {
  return <span className={`status s-${s}`}>{(s || "").replaceAll("_", " ")}</span>;
}

export function Bar({ value, max }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return <div className="bar"><div style={{ width: `${pct}%` }} className={pct >= 100 ? "over" : pct >= 80 ? "warnbar" : ""} /></div>;
}

export function useInterval(fn, ms, deps = []) {
  useEffect(() => {
    fn();
    if (!ms) return;
    const t = setInterval(fn, ms);
    return () => clearInterval(t);
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
}

export function useTick(ms = 1000) {
  const [, set] = useState(0);
  useEffect(() => { const t = setInterval(() => set((x) => x + 1), ms); return () => clearInterval(t); }, [ms]);
}

/** One live row per active step: agent, model, current operation, elapsed time, live usage. */
export function LiveStep({ s }) {
  useTick(1000);
  const llm = s.last_llm || {};
  const u = s.usage || {};
  const ev = s.last_event;
  const busy = ["llm_start", "tool_call", "tool_progress", "review_start", "step_claimed", "resumed_by"].includes(ev?.type)
    || s.status === "reviewing";
  return (
    <div className={`live-step ${s.status}`}>
      <div className="live-head">
        <span className={`pulse ${s.status === "running" || s.status === "reviewing" ? "on" : ""}`} />
        <b>@{s.agent_id}</b>
        {llm.model && <span className="chip">{llm.model_name || llm.model}</span>}
        <Status s={s.status} />
        <span className="faint small right">{s.key} · {s.title?.slice(0, 56)}</span>
      </div>
      <div className="live-op">
        {busy && <span className="spinner" />}
        <span className={busy ? "shimmer" : ""}>{describe(ev) || "Queued — waiting for a worker"}</span>
        {ev?.created_at && <span className="faint small">{elapsed(ev.created_at)}</span>}
      </div>
      <div className="live-usage">
        <span><b>{num(u.tokens_in)}</b> in</span><span><b>{num(u.tokens_out)}</b> out</span>
        <span><b>{money(u.cost_usd)}</b></span><span><b>{u.llm_calls || 0}</b> model calls</span><span><b>{u.tool_calls || 0}</b> tool calls</span>
        {s.started_at && <span>running {elapsed(s.started_at)}</span>}
        {s.locked_by && <span className="faint">on {s.locked_by}</span>}
        {s.attempts > 0 && <span className="warn-text">attempt {s.attempts + 1}</span>}
        {s.reassignments > 0 && <span className="warn-text">reassigned</span>}
      </div>
    </div>
  );
}

export function ArtifactView({ a }) {
  const [text, setText] = useState(null);
  const url = artifactUrl(a.id);
  useEffect(() => {
    if (["code", "markdown", "mermaid", "text", "json"].includes(a.kind)) {
      fetch(url).then((r) => r.text()).then(setText).catch(() => setText("(failed to load)"));
    }
  }, [a.id]);
  let body = null;
  if (a.kind === "diagram") body = <img src={url} alt={a.name} className="artifact-img" />;
  else if (a.kind === "audio") body = <audio controls src={url} />;
  else if (text !== null) {
    const lang = a.kind === "code" ? "python" : a.kind === "mermaid" ? "mermaid" : a.kind === "json" ? "json" : "";
    body = a.kind === "markdown" ? <Markdown>{text}</Markdown> : <Markdown>{"```" + lang + "\n" + text + "\n```"}</Markdown>;
  }
  return (
    <div className="artifact">
      <div className="artifact-head"><b>{a.name}</b> <span className="muted small">{a.kind} · {num(a.size)} bytes</span>
        <a className="right small" href={url} target="_blank" rel="noreferrer">open</a></div>
      {body}
    </div>
  );
}

export function EventRow({ e }) {
  const [open, setOpen] = useState(false);
  const p = e.payload || {};
  const detail = p.content || p.result || p.output || p.correction || p.detail;
  return (
    <div className={`event ev-${e.type}`}>
      <div className="event-line" onClick={() => setOpen(!open)}>
        <span className="muted small mono">{new Date(e.created_at).toLocaleTimeString()}</span>
        <span className="actor">{e.actor?.length > 24 ? e.actor.slice(0, 8) : e.actor}</span>
        <span>{describe(e)}</span>
        {p.tokens_in !== undefined && <span className="muted small">· {num(p.tokens_in)}→{num(p.tokens_out)} tok · {p.latency_ms} ms</span>}
      </div>
      {open && (
        <div className="event-detail">
          {typeof detail === "string" ? <Markdown>{detail}</Markdown> : null}
          <pre className="small">{JSON.stringify(p, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export function useAgents() {
  const [agents, setAgents] = useState([]);
  useEffect(() => { api("/agents").then(setAgents).catch(() => {}); }, []);
  return agents;
}
