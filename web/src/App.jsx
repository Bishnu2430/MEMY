import { createContext, useContext, useEffect, useRef, useState } from "react";
import { ACTIVE, api, getToken, setToken } from "./api.js";
import Icon from "./icons.jsx";
import Chat from "./pages/Chat.jsx";
import Live from "./pages/Live.jsx";
import Tasks from "./pages/Tasks.jsx";
import TaskView from "./pages/TaskView.jsx";
import Approvals from "./pages/Approvals.jsx";
import Agents from "./pages/Agents.jsx";
import Knowledge from "./pages/Knowledge.jsx";
import Tools from "./pages/Tools.jsx";
import Schedules from "./pages/Schedules.jsx";
import Usage from "./pages/Usage.jsx";
import Admin from "./pages/Admin.jsx";
import Projects from "./pages/Projects.jsx";
import Studio from "./pages/Studio.jsx";
import { Toasts } from "./components.jsx";

export const Ctx = createContext(null);
export const useApp = () => useContext(Ctx);

const NAV = [
  ["chat", "Chat"], ["live", "Live trace"], ["tasks", "Tasks"], ["approvals", "Approvals"], ["projects", "Projects"],
  ["studio", "Studio"], ["knowledge", "Knowledge"], ["agents", "Agents"], ["tools", "Tools & MCP"], ["schedules", "Schedules"],
  ["usage", "Usage"], ["admin", "Admin"],
];
const ICON = { chat: "chat", live: "live", tasks: "tasks", approvals: "approvals", agents: "agents", knowledge: "knowledge",
  tools: "tools", schedules: "schedules", usage: "usage", admin: "admin", studio: "studio", projects: "projects" };

function useHash() {
  const [hash, setHash] = useState(window.location.hash.slice(2) || "chat");
  useEffect(() => {
    const on = () => setHash(window.location.hash.slice(2) || "chat");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export const go = (path) => { window.location.hash = `/${path}`; };

function Login({ onDone }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    try {
      const r = await api("/auth/login", { method: "POST", body: { email, password } });
      setToken(r.token);
      onDone();
    } catch (ex) { setErr(ex.message); }
  };
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <h1><span className="brand-mark" /> Memy</h1>
        <p className="muted" style={{ marginTop: -6 }}>Sign in to your team's agent workspace</p>
        <label>Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {err && <div className="error" role="alert">{err === "invalid credentials" ? "That email and password don't match an account." : err}</div>}
        <button className="primary" style={{ padding: "9px 12px" }} disabled={!email || !password}>Sign in</button>
        <p className="faint small">Demo accounts and passwords are listed in the project's <code>.env</code> file.</p>
      </form>
    </div>
  );
}

function TeamSwitcher({ teams, team, setTeam }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const cur = teams.find((t) => t.id === team) || teams[0];
  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  if (!cur) return null;
  return (
    <div className="team-switch" ref={ref}>
      <button className="team-btn" onClick={() => setOpen(!open)} aria-haspopup="listbox" aria-expanded={open} title="Switch team">
        <span className="team-avatar">{cur.name[0]}</span>
        <span className="team-name">{cur.name}<span className="faint tiny">{cur.role[0].toUpperCase() + cur.role.slice(1)}</span></span>
        <Icon name="chevron" size={14} className={`chev ${open ? "up" : ""}`} />
      </button>
      {open && (
        <div className="team-menu" role="listbox" aria-label="Teams">
          <div className="faint tiny" style={{ padding: "4px 10px" }}>Switch team</div>
          {teams.map((t) => (
            <button key={t.id} role="option" aria-selected={t.id === cur.id} className={`team-opt ${t.id === cur.id ? "active" : ""}`}
              onClick={() => { setTeam(t.id); setOpen(false); }}>
              <span className="team-avatar">{t.name[0]}</span>
              <span className="team-name">{t.name}<span className="faint tiny">{t.role[0].toUpperCase() + t.role.slice(1)}</span></span>
              {t.id === cur.id && <Icon name="approvals" size={14} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [authed, setAuthed] = useState(!!getToken());
  const [team, setTeamState] = useState(() => { try { return localStorage.getItem("memy_team") || ""; } catch { return ""; } });
  const [pending, setPending] = useState(0);
  const [recent, setRecent] = useState([]);
  const hash = useHash();

  const setTeam = (t) => { setTeamState(t); try { localStorage.setItem("memy_team", t); } catch { /* ignore */ } };

  useEffect(() => {
    const out = () => { setAuthed(false); setMe(null); };
    window.addEventListener("memy-logout", out);
    return () => window.removeEventListener("memy-logout", out);
  }, []);

  useEffect(() => {
    if (!authed) return;
    api("/me").then((u) => {
      setMe(u);
      if (!u.teams.find((t) => t.id === team) && u.teams.length) setTeam(u.teams[0].id);
    }).catch(() => setAuthed(false));
  }, [authed]);

  useEffect(() => {
    if (!me) return;
    const poll = () => {
      api("/approvals").then((a) => setPending(a.length)).catch(() => {});
      if (team) api(`/tasks?team_id=${team}&limit=14`).then(setRecent).catch(() => {});
    };
    poll();
    const t = setInterval(poll, 4000);
    return () => clearInterval(t);
  }, [me, team]);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  if (!me) return <div className="center muted">Loading…</div>;

  const [page, ...rest] = hash.split("/");
  const pageName = (NAV.find(([k]) => k === (page || "chat")) || [null, "Chat"])[1];
  document.title = `${pending ? `(${pending}) ` : ""}${pageName} · Memy`;
  const role = me.teams.find((t) => t.id === team)?.role;
  let body;
  if (page === "tasks" && rest[0]) body = <TaskView id={rest[0]} />;
  else if (page === "live") body = <Live />;
  else if (page === "tasks") body = <Tasks />;
  else if (page === "approvals") body = <Approvals />;
  else if (page === "agents") body = <Agents id={rest[0]} />;
  else if (page === "knowledge") body = <Knowledge id={rest[0]} />;
  else if (page === "studio") body = <Studio />;
  else if (page === "projects") body = <Projects />;
  else if (page === "tools") body = <Tools />;
  else if (page === "schedules") body = <Schedules />;
  else if (page === "usage") body = <Usage />;
  else if (page === "admin") body = <Admin />;
  else body = <Chat />;

  return (
    <Ctx.Provider value={{ me, team, setTeam, role }}>
      <div className="shell">
        <aside className="side">
          <div className="brand"><span className="brand-mark" /> Memy</div>
          <button className="new-chat" onClick={() => { go("chat"); window.dispatchEvent(new Event("memy-new-chat")); }}>
            <Icon name="plus" size={16} /> New task
          </button>
          <nav>
            {NAV.map(([k, label]) => (
              <a key={k} href={`#/${k}`} className={page === k || (!page && k === "chat") ? "active" : ""}>
                <Icon name={ICON[k]} size={17} />{label}
                {k === "approvals" && pending > 0 && <span className="badge warn">{pending}</span>}
              </a>
            ))}
          </nav>
          <div className="side-label">Recent</div>
          <div className="recents">
            {recent.map((t) => (
              <a key={t.id} href={`#/tasks/${t.id}`} title={t.title}>
                <span className={`dot ${ACTIVE.includes(t.status) ? "on" : ""}`} />{t.title}
              </a>
            ))}
          </div>
          <TeamSwitcher teams={me.teams} team={team} setTeam={setTeam} />
          <div className="side-foot">
            <div className="avatar">{(me.name || "?")[0].toUpperCase()}</div>
            <div style={{ minWidth: 0 }}>
              <div className="small" style={{ fontWeight: 500 }}>{me.name}</div>
              <div className="faint tiny" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{me.email}</div>
            </div>
            <button className="ghost right" title="Sign out" onClick={() => { setToken(null); setAuthed(false); setMe(null); }}>
              <Icon name="logout" size={16} />
            </button>
          </div>
        </aside>
        <main className="main">{page === "chat" || !page ? body : <div className="page">{body}</div>}</main>
        <Toasts />
      </div>
    </Ctx.Provider>
  );
}
