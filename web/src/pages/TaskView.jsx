import { useEffect, useMemo, useState } from "react";
import { ACTIVE, CHECK_LABEL, TERMINAL, api, canStop, money, num, stream } from "../api.js";
import CopyButton from "../copy.jsx";
import { useApp } from "../App.jsx";
import { ArtifactView, Bar, ConfirmButton, EventRow, KnowledgeList, LiveStep, Status, TraceLegend, act, useAgents,
  useInterval } from "../components.jsx";
import Markdown from "../Markdown.jsx";
import { toolLabel } from "../labels.js";
import Select from "../select.jsx";

function StepCard({ s, cost, agents, onAction }) {
  const [open, setOpen] = useState(false);
  const [agent, setAgent] = useState("");
  const [manual, setManual] = useState("");
  const ver = s.verification;
  return (
    <div className={`step-card st-${s.status}`}>
      <div className="step-head" onClick={() => setOpen(!open)}>
        <b>{s.key}</b> <span>{s.title}</span>
        <span className="chip agent" title={`Agent spec version ${s.agent_version}`}>@{s.agent_id}</span>
        <Status s={s.status} />
        {s.depends_on?.length > 0 && <span className="muted small">after {s.depends_on.join(", ")}</span>}
        {(s.attempts > 0 || s.reassignments > 0) && <span className="warn-text small">attempt {s.attempts + 1}{s.reassignments ? ` · reassigned (${s.agents_tried.join(" → ")})` : ""}</span>}
        <span className="muted small right">{money(cost?.cost_usd)} · {num(cost?.tokens)} tok</span>
      </div>
      {ver && (
        <div className="checks">
          {ver.checks.map((c) => <span key={c.name} className={`check ${c.ok ? "ok" : "bad"}`} title={c.detail}>{c.ok ? "✓" : "✗"} {CHECK_LABEL[c.name] || c.name}</span>)}
          {s.review?.llm && <span className={`check ${s.review.verdict === "accept" ? "ok" : "bad"}`} title="Second-model review">reviewer: {s.review.verdict === "accept" ? "accepted" : "asked for changes"}</span>}
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
              <button onClick={() => onAction(s.id, { action: "retry" })} title="Run this step again with the same agent">Retry step</button>
              <Select value={agent} onChange={(e) => setAgent(e.target.value)}>
                <option value="">reassign to…</option>
                {agents.map((a) => <option key={a.id} value={a.id}>@{a.id}</option>)}
              </Select>
              <button disabled={!agent} onClick={() => onAction(s.id, { action: "reassign", agent_id: agent })}>Reassign</button>
              <input placeholder="or write the result yourself" aria-label="Manual result" value={manual} onChange={(e) => setManual(e.target.value)} />
              <button disabled={!manual} onClick={() => onAction(s.id, { action: "complete", output: manual })} title="Mark the step done with your text as its result">Take over</button>
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
  const [know, setKnow] = useState([]);
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
  useInterval(() => {
    if (active !== false) { load(); api(`/live?task_id=${id}`).then(setLive).catch(() => {}); }
    api(`/tasks/${id}/knowledge`).then(setKnow).catch(() => {});
  }, 2000, [id, active]);

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

  const outputs = t.artifacts.filter((a) => a.role !== "input");
  const inputs = t.artifacts.filter((a) => a.role === "input");
  const tokens = t.cost.tokens_in + t.cost.tokens_out;
  const TABS = [["trace", "Trace"], ["result", "Result"], ["outputs", `Outputs (${outputs.length})`],
    ["inputs", `Attached (${inputs.length})`], ["knowledge", `Knowledge (${know.length})`], ["request", "Request"]];

  return (
    <div className="taskview">
      <div className="page-head">
        <h2>{t.title}</h2>
        <Status s={t.status} />
        <span className="chip" title={t.mode === "manual" ? "You picked the agent with an @mention" : "The router chose the agent"}>
          {t.mode === "manual" ? "@mention" : "auto-routed"}</span>
        {t.private && <span className="chip" title="Visible only to you and team admins">private</span>}
        <div className="right row">
          <CopyButton text={id} label="Copy task ID" />
          {canStop(t.status) && role !== "viewer" &&
            <ConfirmButton className="btn-stop" confirm="Confirm stop" title="Stop all agents on this task" onConfirm={() => act(() => api(`/tasks/${id}/cancel`, { method: "POST" }), "Task stopped").then(load)}>Stop task</ConfirmButton>}
          {TERMINAL.includes(t.status) && role !== "viewer" &&
            <button onClick={() => act(() => api(`/tasks/${id}/rerun`, { method: "POST" }), "Started again").then((n) => { window.location.hash = `/tasks/${n.id}`; })}>Run again</button>}
        </div>
      </div>
      <div className="metrics">
        <div><span className="faint tiny">Cost</span><b>{money(t.cost.cost_usd)}</b><Bar value={t.cost.cost_usd} max={t.budget_usd} label={`budget ${money(t.budget_usd)}`} /><span className="faint tiny">of {money(t.budget_usd)} budget</span></div>
        <div><span className="faint tiny">Tokens</span><b>{num(tokens)}</b><Bar value={tokens} max={t.budget_tokens} /><span className="faint tiny">of {num(t.budget_tokens)} budget</span></div>
        <div><span className="faint tiny">Model calls</span><b>{t.cost.llm_calls}</b></div>
        <div><span className="faint tiny">Tool calls</span><b>{t.cost.tool_calls}</b></div>
        <div><span className="faint tiny">Steps</span><b>{t.steps.filter((s) => s.status === "done").length} / {t.steps.length}</b></div>
        <div><span className="faint tiny">Knowledge saved</span><b>{know.length}</b></div>
      </div>

      {t.status === "paused_budget" && (
        <div className="card warn-card">
          <b>Paused at the budget limit.</b> {t.error}
          <div className="row"><label className="field">New budget (USD)<input type="number" step="0.1" min="0" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder={String(t.budget_usd * 2)} /></label>
            <button className="primary" onClick={() => act(() => api(`/tasks/${id}/budget`, { method: "POST", body: { budget_usd: Number(budget) || t.budget_usd * 2, budget_tokens: t.budget_tokens * 2 } }), "Budget raised — resuming").then(load)}>Raise budget &amp; resume</button></div>
        </div>
      )}
      {t.status === "needs_attention" && <div className="card warn-card"><b>Needs attention.</b> {t.error} Open the step below to retry it, reassign it to another agent, or write the result yourself.</div>}
      {t.status === "awaiting_user" && (
        <div className="card warn-card">The router was not confident enough to choose. Pick an agent:
          {(t.router?.shortlist || []).slice(0, 3).map((a) => <button key={a.id} onClick={() => api(`/tasks/${id}/choose`, { method: "POST", body: { agent_id: a.id } }).then(load)}>@{a.id}</button>)}
        </div>
      )}
      {t.approvals.filter((a) => a.status === "pending").map((a) => (
        <div key={a.id} className="card warn-card">
          <b>Approval needed</b> — an agent wants to run <b>{toolLabel(a.tool)}</b> with exactly these arguments:
          <pre className="small">{JSON.stringify(a.args, null, 2)}</pre>
          <button className="primary" onClick={() => act(() => decide(a.id, "approve"), "Approved")}>Approve</button>
          <button onClick={() => act(() => decide(a.id, "reject"), "Rejected")}>Reject</button>
        </div>
      ))}

      {ACTIVE.includes(t.status) && (
        <div className="card">
          <h3>Working now</h3>
          {activeSteps.length ? activeSteps.map((s) => <LiveStep key={s.id} s={s} />)
            : <div className="muted">{taskLive?.last_event ? <EventRow e={taskLive.last_event} /> : "Waiting…"}</div>}
        </div>
      )}

      <div className="grid2">
        <div className="card">
          <h3>Ledger</h3>
          <div className="small"><b>Goal</b> — {t.goal || t.request}</div>
          {t.acceptance?.length > 0 && <><div className="small" style={{ marginTop: 6 }}><b>Acceptance criteria</b></div>
            <ul className="small">{t.acceptance.map((c, i) => <li key={i}>{c}</li>)}</ul></>}
          {t.router && <div className="muted small">Routing: {t.router.mode === "split" ? "split into a plan" : `@${t.router.agent_id}`} · {Math.round(Number(t.router.confidence) * 100)}% confident — {t.router.reason}</div>}
          <h4>Steps</h4>
          {t.steps.length === 0 && <div className="muted small">No steps yet — routing or planning in progress.</div>}
          {t.steps.map((s) => <StepCard key={s.id} s={s} cost={t.step_costs[s.id]} agents={agents} onAction={action} />)}
        </div>
        <div className="card">
          <div className="tabs" role="tablist">
            {TABS.map(([k, label]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{label}</button>)}
          </div>
          {tab === "trace" && (
            <div className="trace">
              <TraceLegend />
              {(byStep.task || []).map((e) => <EventRow key={e.id} e={e} />)}
              {t.steps.map((s) => byStep[s.id] && (
                <div key={s.id} className="trace-group">
                  <div className="trace-title">{s.key} · @{s.agent_id} · {s.title}</div>
                  {byStep[s.id].map((e) => <EventRow key={e.id} e={e} />)}
                </div>
              ))}
            </div>
          )}
          {tab === "result" && (t.result ? <>
            <div className="answer-actions" style={{ marginBottom: 8 }}><CopyButton text={t.result} label="Copy result" />
              <a href={`data:text/markdown;charset=utf-8,${encodeURIComponent(t.result)}`} download={`${t.title.slice(0, 40).replace(/[^a-z0-9]+/gi, "-")}.md`}>Download .md</a></div>
            <Markdown>{t.result}</Markdown></> : <div className="muted">No result yet.</div>)}
          {tab === "outputs" && (outputs.length ? outputs.map((a) => <ArtifactView key={a.id} a={a} />) : <div className="muted">No files produced.</div>)}
          {tab === "inputs" && (inputs.length ? inputs.map((a) => <ArtifactView key={a.id} a={a} />) : <div className="muted">No files were attached.</div>)}
          {tab === "knowledge" && <>
            <p className="muted small">Everything this task added to the team knowledge base: attached files, web pages fetched by
              agents, a work record per step and the distilled task knowledge. Agents in later tasks can search all of it.</p>
            <KnowledgeList items={know} />
          </>}
          {tab === "request" && <><div className="answer-actions" style={{ marginBottom: 8 }}><CopyButton text={t.request} label="Copy request" /></div><Markdown>{t.request}</Markdown></>}
        </div>
      </div>
    </div>
  );
}
