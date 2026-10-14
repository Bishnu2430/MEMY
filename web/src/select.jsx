import { Children, isValidElement, useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * Themed drop-in replacement for <select>. Accepts the same <option value=…>label</option> children and calls
 * onChange({ target: { value } }) so existing handlers keep working. Keyboard: arrows, Home/End, Enter, Esc, type-ahead.
 */
export default function Select({ value, onChange, children, disabled, className = "", title, ...rest }) {
  const options = Children.toArray(children).filter((c) => isValidElement(c) && c.type === "option").map((o) => ({
    value: o.props.value !== undefined ? String(o.props.value) : textOf(o.props.children),
    label: o.props.children,
    disabled: !!o.props.disabled,
  }));
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [up, setUp] = useState(false);
  const ref = useRef(null);
  const listRef = useRef(null);
  const typed = useRef({ s: "", t: 0 });
  const current = options.find((o) => o.value === String(value ?? "")) || options[0];

  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  useLayoutEffect(() => {
    if (!open || !ref.current) return;
    const r = ref.current.getBoundingClientRect();
    setUp(window.innerHeight - r.bottom < 260 && r.top > window.innerHeight - r.bottom);
    listRef.current?.querySelector(".sel-opt.active")?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const pick = (o) => {
    if (!o || o.disabled) return;
    setOpen(false);
    if (o.value !== String(value ?? "")) onChange?.({ target: { value: o.value } });
  };
  const openAt = () => { setActive(Math.max(0, options.indexOf(current))); setOpen(true); };
  const onKey = (e) => {
    if (disabled) return;
    if (!open && ["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) { e.preventDefault(); openAt(); return; }
    if (!open) return;
    if (e.key === "Escape") { e.preventDefault(); setOpen(false); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(options.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setActive(0); }
    else if (e.key === "End") { e.preventDefault(); setActive(options.length - 1); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(options[active]); }
    else if (e.key === "Tab") setOpen(false);
    else if (e.key.length === 1) {
      const now = Date.now();
      typed.current = { s: (now - typed.current.t < 700 ? typed.current.s : "") + e.key.toLowerCase(), t: now };
      const i = options.findIndex((o) => textOf(o.label).toLowerCase().startsWith(typed.current.s));
      if (i >= 0) setActive(i);
    }
  };

  return (
    <div className={`sel ${className} ${disabled ? "disabled" : ""}`} ref={ref}>
      <button type="button" className="sel-btn" disabled={disabled} title={title} aria-haspopup="listbox" aria-expanded={open}
        aria-label={rest["aria-label"]} onClick={() => (open ? setOpen(false) : openAt())} onKeyDown={onKey}>
        <span className="sel-value">{current ? current.label : ""}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"
          className={`sel-chev ${open ? "open" : ""}`}><path d="M6 9l6 6 6-6" /></svg>
      </button>
      {open && (
        <div className={`sel-list ${up ? "up" : ""}`} role="listbox" ref={listRef} aria-label={rest["aria-label"]}>
          {options.map((o, i) => (
            <div key={o.value + i} role="option" aria-selected={o.value === current?.value} aria-disabled={o.disabled}
              className={`sel-opt ${i === active ? "active" : ""} ${o.value === current?.value ? "selected" : ""} ${o.disabled ? "disabled" : ""}`}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); pick(o); }}>
              <span>{o.label}</span>
              {o.value === current?.value && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7" /></svg>
              )}
            </div>
          ))}
          {options.length === 0 && <div className="sel-opt disabled">No options</div>}
        </div>
      )}
    </div>
  );
}

function textOf(node) {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf(node.props.children);
  return "";
}
