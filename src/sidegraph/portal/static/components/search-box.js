// The header search: a debounced combobox over /api/search. Arrow keys move through the hits,
// Enter or a click opens the record card, Escape closes the list. A query that is the start
// of exactly one record id (8 or more characters) opens that record at once.
// see design/superpowers/specs/2026-11-17-portal-design.md (D4)
import { describeError, get } from "../api.js";
import { onLive } from "../live.js";
import { html, useEffect, useRef, useState } from "../ui.js";

const DEBOUNCE_MS = 250;
// One more than the largest count shown, so "50+" is only said when there really are more.
const COUNT_CAP = 50;
const SHOWN = 8;
const WATCH = ["records", "graph", "sync"];

const KIND_LABELS = { adr: "ADR", fact: "FACT" };

function kindLabel(hit) {
  const kind = hit.kind ?? hit.type;
  return KIND_LABELS[kind] ?? kind.toUpperCase();
}

/** `text` with every case-insensitive occurrence of `words` wrapped in <mark>, as vnodes. */
export function highlight(text, words) {
  const needles = [...new Set(words.filter(Boolean).map((word) => word.toLowerCase()))].sort(
    (a, b) => b.length - a.length,
  );
  if (!needles.length) return text;
  // The server matches on lowercased text (D4), and lowercasing can change the length ("İ"
  // becomes two characters). So match on the whole lowercased text (a final "Σ" depends on its
  // neighbours) and map each match back through the span of the original character every
  // lowercased unit came from.
  const lower = text.toLowerCase();
  const spans = [];
  let at = 0;
  for (const char of text) {
    for (let i = 0; i < char.toLowerCase().length; i++) spans.push([at, at + char.length]);
    at += char.length;
  }
  if (spans.length !== lower.length) return text;
  const found = new RegExp(needles.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  const parts = [];
  let from = 0;
  for (const match of lower.matchAll(found)) {
    const start = spans[match.index][0];
    const end = spans[match.index + match[0].length - 1][1];
    if (start < from) continue;
    if (start > from) parts.push(text.slice(from, start));
    parts.push(html`<mark>${text.slice(start, end)}</mark>`);
    from = end;
  }
  if (from < text.length) parts.push(text.slice(from));
  return parts;
}

// The graph page's "Superseded / history" choice. The search on that page follows it; the
// page sets it, and an open search asks again when it changes.
let searchHistory = false;
const historyListeners = new Set();

export function setSearchHistory(value) {
  searchHistory = value;
  for (const listener of historyListeners) listener(value);
}

export function open(id) {
  location.hash = `#/records/${encodeURIComponent(id)}`;
}

function Option({ hit, id, active, onPick, onHover }) {
  const words = hit.match.words;
  return html`<a
    id=${id}
    role="option"
    class=${`search-option${active ? " is-active" : ""}`}
    href=${`#/records/${encodeURIComponent(hit.id)}`}
    aria-selected=${active}
    tabindex="-1"
    onClick=${onPick}
    onMouseMove=${onHover}
  >
    <span class="search-option-head">
      <span class=${`kind-chip kind-${hit.kind ?? hit.type}`}>${kindLabel(hit)}</span>
      <span class="muted small">${[hit.status, ...hit.domains].join(" · ")}</span>
    </span>
    <span class="search-option-title">${highlight(hit.title, words)}</span>
    ${hit.snippet && html`<span class="search-option-snippet muted small">${highlight(hit.snippet, words)}</span>`}
    ${hit.match.via_anchor &&
    html`<span class="small muted">found through anchor: <code>${highlight(hit.match.via_anchor, words)}</code></span>`}
  </a>`;
}

export function SearchBox() {
  const [text, setText] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [shown, setShown] = useState(false);
  const [active, setActive] = useState(0);
  const [history, setHistory] = useState(searchHistory);
  const box = useRef(null);
  // True while the person's own typing decides where an id-prefix hit goes; any dismissal clears it.
  const typing = useRef(false);
  const latest = useRef(null);
  // True from the keystroke until its answer arrives: a live refresh must not cut it short.
  const pending = useRef(false);
  // A change that arrived while the lookup was pending: ask again once it is answered.
  const stale = useRef(false);
  // The id of the highlighted hit, so a refresh keeps it highlighted wherever it moved to.
  const activeId = useRef(null);
  // Read by `run` at the moment it asks, so a re-ask queued before the change still uses the new value.
  const historyNow = useRef(history);
  historyNow.current = history;
  const query = text.trim();

  const dismiss = () => {
    typing.current = false;
    setShown(false);
  };

  /** Ask for `q`; a newer request supersedes this one. Only typing follows an id-prefix hit. */
  const run = (q, follow) => {
    latest.current?.abort();
    const abort = new AbortController();
    latest.current = abort;
    get("/api/search", { q, limit: COUNT_CAP + 1, history: historyNow.current }, { signal: abort.signal, user: follow ? undefined : false }).then(
      (found) => {
        if (latest.current !== abort) return;
        if (follow) pending.current = false;
        if (found.open_id && follow && typing.current) {
          // The person typed (the start of) a record id: go to the card, unless they moved on.
          dismiss();
          open(found.open_id);
          return;
        }
        setResult({ query: q, ...found });
        setError(null);
        // A refresh keeps the highlighted hit while it is still listed, else the nearest row.
        const rows = found.hits.slice(0, SHOWN);
        const kept = rows.findIndex((hit) => hit.id === activeId.current);
        setActive(follow ? 0 : (row) => (kept >= 0 ? kept : Math.max(0, Math.min(row, rows.length - 1))));
        if (follow && stale.current) {
          stale.current = false;
          run(q, false);
        }
      },
      (failure) => {
        if (failure.name === "AbortError" || latest.current !== abort) return;
        if (follow) pending.current = false;
        stale.current = false;
        setError(describeError(failure));
      },
    );
  };

  // Each keystroke restarts the wait; a newer query aborts the one in flight.
  useEffect(() => {
    stale.current = false;
    if (!query) {
      pending.current = false;
      setResult(null);
      setError(null);
      return undefined;
    }
    pending.current = true;
    setError(null);
    const timer = setTimeout(() => run(query, true), DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      latest.current?.abort();
    };
  }, [query]);

  // What is on screen follows the store: a change, a reconnect or a tab coming back refetches it.
  // The history setting is part of the question, so a change of it asks again.
  const refresh = () => {
    if (!query) return;
    if (pending.current) stale.current = true;
    else run(query, false);
  };
  useEffect(
    () =>
      onLive((event) => {
        if (event.type === "refetch" || (event.type === "changed" && WATCH.includes(event.what))) refresh();
      }),
    [query],
  );
  useEffect(() => {
    historyListeners.add(setHistory);
    return () => historyListeners.delete(setHistory);
  }, []);
  useEffect(refresh, [history]);

  const hits = result && result.query === query ? result.hits : null;
  const visible = shown && query !== "" && (hits !== null || error !== null);
  const options = hits ? hits.slice(0, SHOWN) : [];
  const total = hits ? hits.length : 0;
  activeId.current = options[active]?.id ?? null;
  const totalText = total > COUNT_CAP ? `${COUNT_CAP}+` : String(total);

  // Keep the highlighted row inside the scrolling list while the arrow keys move it.
  useEffect(() => {
    if (visible) document.getElementById(`search-option-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active, visible]);

  const pick = (id) => {
    dismiss();
    open(id);
  };
  const onKeyDown = (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!shown) setShown(true);
      if (options.length) {
        const step = event.key === "ArrowDown" ? 1 : options.length - 1;
        setActive((active + step) % options.length);
      }
    } else if (event.key === "Enter" && visible && options[active]) {
      event.preventDefault();
      pick(options[active].id);
    }
  };
  // Escape works from the field and from the "All in Records" link. The first one closes the
  // list; a second one clears the field, as a search input does.
  const onBoxKeyDown = (event) => {
    if (event.key !== "Escape") return;
    if (visible) {
      event.preventDefault();
      box.current.querySelector("input").focus();
    }
    dismiss();
  };
  // Focus leaving the whole widget (a click elsewhere, a Tab out) closes the list. The box is
  // gone when the page changes: its element then loses focus with nothing left to close.
  const onFocusOut = (event) => {
    if (box.current && !box.current.contains(event.relatedTarget)) dismiss();
  };

  return html`<div class="search" ref=${box} onFocusOut=${onFocusOut} onKeyDown=${onBoxKeyDown}>
    <label class="search-field">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" />
      </svg>
      <span class="graph-sr">Search records</span>
      <input
        type="search"
        role="combobox"
        placeholder="Search records"
        autocomplete="off"
        aria-expanded=${visible && options.length > 0}
        aria-controls=${visible && options.length > 0 ? "search-results" : undefined}
        aria-activedescendant=${visible && options[active] ? `search-option-${active}` : undefined}
        value=${text}
        onInput=${(event) => {
          typing.current = true;
          setText(event.target.value);
          setShown(true);
        }}
        onFocus=${() => setShown(true)}
        onKeyDown=${onKeyDown}
      />
      ${hits !== null && html`<span class="search-count muted small">${totalText} found</span>`}
    </label>
    ${
      visible &&
      html`<div class="search-list" onMouseDown=${(event) => event.preventDefault()}>
        ${error && html`<p class="search-note" role="alert">${error}</p>`}
        ${!error && !options.length && html`<p class="search-note muted">No records match “${query}”.</p>`}
        ${
          options.length > 0 &&
          html`<div id="search-results" role="listbox" aria-label="Search results">
            ${options.map(
              (hit, index) => html`<${Option}
                key=${hit.id}
                hit=${hit}
                id=${`search-option-${index}`}
                active=${index === active}
                onPick=${(event) => {
                  event.preventDefault();
                  pick(hit.id);
                }}
                onHover=${() => index !== active && setActive(index)}
              />`,
            )}
          </div>`
        }
        <div class="search-foot small muted">
          <span>↑↓ choose · Enter opens</span>
          ${total > 0 && html`<a href=${`#/records?q=${encodeURIComponent(query)}${history ? "&history=true" : ""}`} onClick=${dismiss}>All ${totalText} in Records</a>`}
        </div>
      </div>`
    }
  </div>`;
}
