import { useEffect, useMemo, useState } from "react";
import { ACTIVE, api, money, num, stream } from "../api.js";
import { useApp } from "../App.jsx";
import { ArtifactView, EventRow, LiveStep, Status, useAgents, useInterval } from "../components.jsx";
import Markdown from "../Markdown.jsx";

function StepCard({ s, cost, agents, onAction }) {
  const [open, setOpen] = useState(false);
  const [agent, setAgent] = useState("");
  const [manual, setManual] = useState("");
  const ver = s.verification;
  return (
    <div className={`step-card st-${s.status}`}>
      <div className="step-head" onClick={() => setOpen(!open)}>
        <b>{s.key}</b> <span>{s.title}</span>
        <span className="chip">@{s.agent_id} v{s.agent_version}</span>
        <Status s={s.status} />
        {s.depends_on?.length > 0 && <span className="muted small">after {s.depends_on.join(", ")}</span>}
        {(s.attempts > 0 || s.reassignments > 0) && <span className="warn-text small">attempt {s.attempts + 1}{s.reassignments ? ` · reassigned (${s.agents_tried.join(" → ")})` : ""}</span>}
        <span className="muted small right">{money(cost?.cost_usd)} · {num(cost?.tokens)} tok</span>
      </div>
      {ver && (
        <div className="checks">
          {ver.checks.map((c) => <span key={c.name} className={`check ${c.ok ? "ok" : "bad"}`} title={c.detail}>{c.ok ? "✓" : "✗"} {c.name}</span>)}
          {s.review?.llm && <span className={`check ${s.review.verdict === "accept" ? "ok" : "bad"}`}>review: {s.review.verdict} ({s.review.model})</span>}
        </div>
      )}
      {open && (
        <div className="step-body">
          <div className="muted small">Instruction</div>
          <Markdown>{s.instruction}</Markdown>
          {s.correction && <><div className="muted small">Correction note</div><Markdown>{s.correction}</Markdown></>}
          {s.output && <><div className="muted small">Output</div><Markdown>{s.output}</Markdown></>}
          {s.error && <div className="error">{s.error}</div>}
          {s.review?.issues?.length > 0 && (
            <ul>{s.review.issues.map((i, k) => <li key={k}><b>[{i.severity}]</b> {i.description} — <i>{i.fix}</i></li>)}</ul>
          )}
          {["escalated", "failed", "paused", "done", "review"].includes(s.status) && (
            <div className="row">
              <button onClick={() => onAction(s.id, { action: "retry" })}>Retry step</button>
              <select value={agent} onChange={(e) => setAgent(e.target.value)}>
                <option value="">reassign to…</option>
                {agents.map((a) => <option key={a.id} value={a.id}>@{a.id}</option>)}
              </select>
              <button disabled={!agent} onClick={() => onAction(s.id, { action: "reassign", agent_id: agent })}>Reassign</button>
              <input placeholder="or write the output yourself" value={manual} onChange={(e) => setManual(e.target.value)} />
              <button disabled={!manual} onClick={() => onAction(s.id, { action: "complete", output: manual })}>Take over</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function TaskView({ id }) {
  const { role } = useApp();
  const agents = useAgents();
  const [t, setT] = useState(null);
  const [events, setEvents] = useState([]);
  const [live, setLive] = useState(null);
  const [budget, setBudget] = useState("");
  const [tab, setTab] = useState("trace");
  const active = t && (ACTIVE.includes(t.status) || ["awaiting_user"].includes(t.status));

  const load = () => api(`/tasks/${id}`).then(setT).catch(() => {});
  useEffect(() => {
    setEvents([]);
    load();
    const close = stream(`/tasks/${id}/stream`, (e) => {
      setEvents((prev) => (prev.length && prev[prev.length - 1].id >= e.id ? prev : [...prev, e]));
    });
    return close;
  }, [id]);
  useInterval(() => { if (active !== false) { load(); api(`/live?task_id=${id}`).then(setLive).catch(() => {}); } }, 1500, [id, active]);

  const byStep = useMemo(() => {
    const m = {};
    events.forEach((e) => { const k = e.step_id || "task"; (m[k] = m[k] || []).push(e); });
    return m;
  }, [events]);

  if (!t) return <div className="muted">Loading…</div>;
  const action = async (stepId, body) => { await api(`/steps/${stepId}/action`, { method: "POST", body }); load(); };
  const decide = async (aid, decision) => { await api(`/approvals/${aid}`, { method: "POST", body: { decision } }); load(); };
  const activeSteps = (live?.steps || []).filter((s) => ["running", "reviewing", "waiting_approval", "ready"].includes(s.status));
  const taskLive = live?.tasks?.[0];

  return (
    <div className="taskview">
      <div className="page-head">
        <h2>{t.title}</h2>
        <Status s={t.status} />
        <span className="chip">{t.mode}</span>
        <span className="muted small">{money(t.cost.cost_usd)} of {money(t.budget_usd)} · {num(t.cost.tokens_in + t.cost.tokens_out)} / {num(t.budget_tokens)} tok · {t.cost.llm_calls} LLM · {t.cost.tool_calls} tool calls</span>
        <div className="right row">
          {ACTIVE.includes(t.status) && role !== "viewer" && <button onClick={() => api(`/tasks/${id}/cancel`, { method: "POST" }).then(load)}>Cancel</button>}
        </div>
      </div>

      {t.status === "paused_budget" && (
        <div className="card warn-card">
          Budget stop fired: {t.error}
          <div className="row"><input placeholder="new budget USD" value={budget} onChange={(e) => setBudget(e.target.value)} />
            <button onClick={() => api(`/tasks/${id}/budget`, { method: "POST", body: { budget_usd: Number(budget) || null, budget_tokens: (t.budget_tokens * 2) } }).then(load)}>Raise budget &amp; resume</button></div>
        </div>
      )}
      {t.status === "needs_attention" && <div className="card warn-card">Needs attention: {t.error}. Retry, reassign or take over a step below.</div>}
      {t.status === "awaiting_user" && (
        <div className="card warn-card">Router confidence too low — pick an agent:
          {(t.router?.shortlist || []).slice(0, 3).map((a) => <button key={a.id} onClick={() => api(`/tasks/${id}/choose`, { method: "POST", body: { agent_id: a.id } }).then(load)}>@{a.id}</button>)}
        </div>
      )}
      {t.approvals.filter((a) => a.status === "pending").map((a) => (
        <div key={a.id} className="card warn-card">
          <b>Approval needed:</b> <code>{a.tool}</code>
          <pre className="small">{JSON.stringify(a.args, null, 2)}</pre>
          <button className="primary" onClick={() => decide(a.id, "approve")}>Approve</button>
          <button onClick={() => decide(a.id, "reject")}>Reject</button>
        </div>
      ))}

      {ACTIVE.includes(t.status) && (
        <div className="card">
          <h3>Live now</h3>
          {activeSteps.length ? activeSteps.map((s) => <LiveStep key={s.id} s={s} />)
            : <div className="muted">{taskLive?.last_event ? <EventRow e={taskLive.last_event} /> : "Waiting…"}</div>}
        </div>
      )}

      <div className="grid2">
        <div className="card">
          <h3>Ledger</h3>
          <div className="small"><b>Goal:</b> {t.goal || t.request}</div>
          {t.acceptance?.length > 0 && <ul className="small">{t.acceptance.map((c, i) => <li key={i}>{c}</li>)}</ul>}
          {t.router && <div className="muted small">Router: {t.router.mode} → @{t.router.agent_id} ({Number(t.router.confidence).toFixed(2)}) — {t.router.reason}</div>}
          {t.steps.map((s) => <StepCard key={s.id} s={s} cost={t.step_costs[s.id]} agents={agents} onAction={action} />)}
        </div>
        <div className="card">
          <div className="tabs">
            {["trace", "result", "artifacts", "request"].map((k) => <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{k}{k === "artifacts" ? ` (${t.artifacts.length})` : ""}</button>)}
          </div>
          {tab === "trace" && (
            <div className="trace">
              {(byStep.task || []).map((e) => <EventRow key={e.id} e={e} />)}
              {t.steps.map((s) => byStep[s.id] && (
                <div key={s.id} className="trace-group">
                  <div className="trace-title">{s.key} · @{s.agent_id} · {s.title}</div>
                  {byStep[s.id].map((e) => <EventRow key={e.id} e={e} />)}
                </div>
              ))}
            </div>
          )}
          {tab === "result" && (t.result ? <Markdown>{t.result}</Markdown> : <div className="muted">No result yet.</div>)}
          {tab === "artifacts" && (t.artifacts.length ? t.artifacts.map((a) => <ArtifactView key={a.id} a={a} />) : <div className="muted">No artifacts.</div>)}
          {tab === "request" && <Markdown>{t.request}</Markdown>}
        </div>
      </div>
    </div>
  );
}
