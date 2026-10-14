import { useState } from "react";

async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for non-secure contexts (http on a LAN address)
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  }
}

/** Copy-to-clipboard button with a short "Copied" confirmation. `text` may be a string or a function. */
export default function CopyButton({ text, label = "Copy", className = "", title }) {
  const [state, setState] = useState("idle");
  const onClick = async (e) => {
    e.stopPropagation();
    const ok = await writeClipboard(typeof text === "function" ? text() : text);
    setState(ok ? "done" : "fail");
    setTimeout(() => setState("idle"), 1600);
  };
  return (
    <button type="button" className={`copy-btn ${state} ${className}`} onClick={onClick} title={title || label}
      aria-label={title || label}>
      {state === "done" ? "Copied" : state === "fail" ? "Copy failed" : label}
    </button>
  );
}
