// The four anchor status words with tooltips reachable by Tab and Enter.
// see design/superpowers/specs/2026-11-17-portal-design.md (D5)
import { html, useEffect, useRef, useState } from "../ui.js";

export const STATUS_WORDS = {
  found: {
    glyph: "●",
    tip: "Found in the code: the graph holds exactly this symbol (status: live).",
  },
  ambiguous: {
    glyph: "◐",
    tip: "Several symbols now match this anchor; Sidegraph does not guess which one (status: degraded).",
  },
  missing: {
    glyph: "○",
    tip: "The symbol is no longer in the graph: deleted, or renamed without a move Sidegraph could confirm (status: orphaned).",
  },
  unchecked: {
    glyph: "◌",
    tip: "Not checked: the index was rebuilt and no sync has run since. The next sync (a commit, a new session, or sidegraph-sync) updates it.",
  },
};

let nextId = 0;

/** `word` is one of found | ambiguous | missing | unchecked. */
export function StatusWord({ word }) {
  const [open, setOpen] = useState(false);
  const [id] = useState(() => `status-tip-${++nextId}`);
  const tip = useRef(null);
  // Keep the tooltip inside the viewport: a word at the end of a row on a phone would push it out.
  useEffect(() => {
    const element = tip.current;
    if (!element) return;
    element.style.left = "0px";
    const overflow = element.getBoundingClientRect().right - (document.documentElement.clientWidth - 8);
    if (overflow > 0) element.style.left = `${-overflow}px`;
  }, [open]);
  const entry = STATUS_WORDS[word];
  if (!entry) return null;
  const show = () => setOpen(true);
  const hide = () => setOpen(false);
  return html`<span class="status-word-wrap">
    <button
      type="button"
      class=${`status-word sw-${word}`}
      aria-describedby=${open ? id : undefined}
      onClick=${show}
      onFocus=${show}
      onBlur=${hide}
      onMouseEnter=${show}
      onMouseLeave=${hide}
      onKeyDown=${(event) => event.key === "Escape" && hide()}
    >
      <span aria-hidden="true">${entry.glyph} </span>${word}
    </button>
    ${open && html`<span role="tooltip" id=${id} class="status-tip" ref=${tip}>${entry.tip}</span>`}
  </span>`;
}
