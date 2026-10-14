import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { ArtifactView, Bar, Status, act, useInterval } from "../components.jsx";
import { Mermaid } from "../Markdown.jsx";
import Select from "../select.jsx";

const TABS = [
  ["speech", "Speech", "Text to speech with a chosen voice. About 1 credit per character."],
  ["sound", "Sound effects", "Describe a sound; 0.5–30 seconds."],
  ["music", "Music", "Compose music from a prompt. Needs a paid ElevenLabs plan."],
  ["image", "Image", "Image generation via the ElevenLabs Image API. Needs a Pro plan and the Image & Video key permission."],
  ["video", "Video", "Short video clips via the ElevenLabs Video API. Needs a Pro plan and the Image & Video key permission."],
  ["transcribe", "Transcribe", "Upload a recording and get a transcript (Groq Whisper, or ElevenLabs Scribe)."],
  ["diagram", "Diagram", "Write Mermaid and render it to SVG with live preview."],
];

const JOB_LABEL = { speech: "Speech", sound: "Sound effect", music: "Music", image: "Image", video: "Video", transcribe: "Transcript", diagram: "Diagram" };

function Credits({ status }) {
  if (!status) return null;
  if (!status.configured) return <div className="notice warn">ElevenLabs is not configured: {status.error}</div>;
  const used = status.character_count || 0, limit = status.character_limit || 1;
  return (
    <div className="credits" title="ElevenLabs credits used this billing period">
      <span className="small">ElevenLabs <b>{status.tier}</b> plan</span>
      <div style={{ flex: 1, maxWidth: 260 }}><Bar value={used} max={limit} label={`${used} of ${limit} credits used`} /></div>
      <span className="small">{(limit - used).toLocaleString()} of {limit.toLocaleString()} credits left</span>
      {status.next_reset_unix && <span className="faint tiny">resets {new Date(status.next_reset_unix * 1000).toLocaleDateString()}</span>}
    </div>
  );
}

export default function Studio() {
  const { team, role } = useApp();
  const [tab, setTab] = useState("speech");
  const [status, setStatus] = useState(null);
  const [voices, setVoices] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [gallery, setGallery] = useState([]);
  const [filter, setFilter] = useState("");
  const [f, setF] = useState({ text: "", voice: "", prompt: "", duration_s: 3, length_s: 20, aspect_ratio: "1:1",
    model_id: "", duration_secs: 4, source: "flowchart LR\n  Browse --> Cart --> Checkout --> Paid", engine: "whisper" });
  const [audio, setAudio] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  useEffect(() => {
    api("/media/status").then(setStatus).catch(() => {});
    api("/media/voices").then((v) => {
      setVoices(v);
      const pick = v.find((x) => x.name.startsWith("George")) || v[0];
      if (pick) setF((cur) => ({ ...cur, voice: cur.voice || pick.voice_id }));
    }).catch(() => {});
  }, []);
  const running = jobs.some((j) => ["queued", "running"].includes(j.status));
  useInterval(() => {
    if (!team) return;
    api(`/media/jobs?team_id=${team}`).then(setJobs).catch(() => {});
    api(`/media/gallery?team_id=${team}${filter ? `&kind=${filter}` : ""}`).then(setGallery).catch(() => {});
  }, running ? 2500 : 8000, [team, filter, running]);

  const submit = async () => {
    setBusy(true);
    try {
      let params;
      if (tab === "transcribe") {
        if (!audio) throw new Error("choose a recording first");
        const form = new FormData();
        form.append("team_id", team);
        form.append("file", audio);
        const up = await api("/uploads", { method: "POST", form });
        params = { artifact_id: up.artifact_id, engine: f.engine };
      } else {
        params = {
          speech: { text: f.text, voice: f.voice }, sound: { prompt: f.prompt, duration_s: Number(f.duration_s) },
          music: { prompt: f.prompt, length_s: Number(f.length_s) },
          image: { prompt: f.prompt, aspect_ratio: f.aspect_ratio, model_id: f.model_id || undefined },
          video: { prompt: f.prompt, duration_secs: Number(f.duration_secs), model_id: f.model_id || undefined },
          diagram: { source: f.source },
        }[tab];
      }
      await act(() => api("/media/jobs", { method: "POST", body: { team_id: team, kind: tab, params } }), "Generation started");
      api(`/media/jobs?team_id=${team}`).then(setJobs);
    } catch { /* toast shown */ } finally { setBusy(false); }
  };
  const voice = voices.find((v) => v.voice_id === f.voice);
  const [, , help] = TABS.find((t) => t[0] === tab);
  const paid = ["music", "image", "video"].includes(tab) && status?.tier === "free";
  const canSubmit = role !== "viewer" && !busy && (tab === "speech" ? f.text.trim() : tab === "diagram" ? f.source.trim()
    : tab === "transcribe" ? !!audio : f.prompt.trim());

  return (
    <div>
      <div className="page-head"><h2>Studio</h2><span className="muted">generate, render and play media — every result is saved to the team gallery</span></div>
      <Credits status={status} />
      <div className="grid2">
        <div className="card">
          <div className="tabs" role="tablist">
            {TABS.map(([k, label]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{label}</button>)}
          </div>
          <p className="muted small">{help}</p>
          {paid && <div className="notice warn small">Your ElevenLabs workspace is on the free plan, so this will be rejected by ElevenLabs. The request is still sent so the result is visible here.</div>}
          {tab === "speech" && <>
            <label className="field">Voice
              <Select value={f.voice} onChange={set("voice")}>
                {voices.map((v) => <option key={v.voice_id} value={v.voice_id}>{v.name}{v.labels?.accent ? ` · ${v.labels.accent} accent` : ""}</option>)}
                {!voices.length && <option value="">Default voice</option>}
              </Select>
            </label>
            {voice?.preview_url && <><div className="faint tiny">Preview of {voice.name.split(" - ")[0]}</div>
              <audio controls src={voice.preview_url} className="player" aria-label="Voice preview" /></>}
            <label className="field">Text <span className="faint tiny">{f.text.length} characters ≈ {f.text.length} credits</span>
              <textarea rows={4} value={f.text} onChange={set("text")} placeholder="Welcome to Nimbus — sneakers made for the city." /></label>
          </>}
          {["sound", "music", "image", "video"].includes(tab) && (
            <label className="field">Prompt
              <textarea rows={3} value={f.prompt} onChange={set("prompt")} placeholder={{
                sound: "a soft cash-register chime", music: "upbeat lo-fi beat for a product video, warm keys",
                image: "a white sneaker on a pastel pink background, studio light", video: "a sneaker rotating slowly on a turntable" }[tab]} />
            </label>
          )}
          {tab === "sound" && <label className="field">Duration (seconds)<input type="number" min="0.5" max="30" step="0.5" value={f.duration_s} onChange={set("duration_s")} /></label>}
          {tab === "music" && <label className="field">Length (seconds)<input type="number" min="3" max="300" value={f.length_s} onChange={set("length_s")} /></label>}
          {tab === "image" && <div className="row">
            <label className="field">Aspect ratio<Select value={f.aspect_ratio} onChange={set("aspect_ratio")}>{["1:1", "16:9", "4:3", "3:4", "9:16"].map((a) => <option key={a}>{a}</option>)}</Select></label>
            <label className="field">Model<Select value={f.model_id} onChange={set("model_id")}><option value="">default</option>{(status?.image_models || []).map((m) => <option key={m}>{m}</option>)}</Select></label>
          </div>}
          {tab === "video" && <div className="row">
            <label className="field">Duration<Select value={f.duration_secs} onChange={set("duration_secs")}>{[4, 6, 8].map((d) => <option key={d} value={d}>{d} seconds</option>)}</Select></label>
            <label className="field">Model<Select value={f.model_id} onChange={set("model_id")}><option value="">default</option>{(status?.video_models || []).map((m) => <option key={m}>{m}</option>)}</Select></label>
          </div>}
          {tab === "transcribe" && <>
            <label className="field">Recording (audio or video, up to 25 MB)<input type="file" accept="audio/*,video/*" onChange={(e) => setAudio(e.target.files[0])} /></label>
            <label className="field">Engine<Select value={f.engine} onChange={set("engine")}>
              <option value="whisper">Groq Whisper (no ElevenLabs credits)</option><option value="elevenlabs">ElevenLabs Scribe</option></Select></label>
          </>}
          {tab === "diagram" && <>
            <label className="field">Mermaid source<textarea rows={7} className="mono" value={f.source} onChange={set("source")} /></label>
            <div className="faint tiny">Live preview</div>
            <Mermaid code={f.source} />
          </>}
          <button className="primary" onClick={submit} disabled={!canSubmit}>{busy ? "Starting…" : tab === "diagram" ? "Render & save" : "Generate"}</button>
        </div>
        <div className="card">
          <h3>Recent generations</h3>
          {jobs.length === 0 && <div className="muted small">Nothing generated yet.</div>}
          <table><tbody>{jobs.slice(0, 12).map((j) => (
            <tr key={j.id}>
              <td><span className="chip">{JOB_LABEL[j.kind] || j.kind}</span></td>
              <td className="small">{j.params?.text?.slice(0, 50) || j.params?.prompt?.slice(0, 50) || j.artifact_name || "—"}
                {j.error && <div className="error tiny">{j.error}</div>}
                {j.status === "running" && j.progress && <div className="faint tiny">{j.progress}</div>}</td>
              <td><Status s={j.status === "completed" ? "done" : j.status === "running" ? "running" : j.status} /></td>
              <td className="faint tiny nowrap">{j.user_name} · {ago(j.created_at)}</td>
            </tr>))}</tbody></table>
        </div>
      </div>
      <div className="card">
        <div className="row"><h3 style={{ margin: 0 }}>Team gallery</h3>
          <Select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter gallery">
            <option value="">all media</option><option value="audio">audio</option><option value="image">images</option>
            <option value="video">video</option><option value="diagram">diagrams</option></Select>
          <span className="faint small">{gallery.length} item(s) — from the studio and from agent tasks</span></div>
        <div className="gallery">
          {gallery.map((a) => (
            <div key={a.id}>
              <ArtifactView a={a} compact />
              <div className="faint tiny">{a.task_title ? <a href={`#/tasks/${a.task_id}`}>{a.task_title.slice(0, 50)}</a> : "Studio"} · {a.created_by_name || "Agent"} · {ago(a.created_at)}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
