// "Bound to code": a record's anchors with their status words, the code snippet behind a
// leaf anchor (12 lines, "show more" up to 200) and the link that opens the file in an editor.
// see design/superpowers/specs/2026-11-17-portal-design.md (D5)
import { describeError } from "../api.js";
import { html, useRef, useState } from "../ui.js";
import { Banner } from "./banners.js";
import { StatusWord } from "./status-word.js";
import { useResource } from "./use-resource.js";

const TIER_LABELS = { 0: "abstract anchor", 1: "group anchor", 2: "leaf anchor" };
const WATCH = ["records", "graph", "sync"];
const FIRST_PAGE = 12;

/**
 * The editor link: the settings' `editor_url` template with `{path}` set to the anchor's
 * absolute `editor_path` and `{line}` to its first line. Nothing else goes into it.
 */
export function editorHref(template, anchor) {
  // One pass, so text in the path cannot be taken for a placeholder. A file name may hold
  // characters a URL reads as its own (#, ?, %, &, spaces, tabs); every character except unreserved
  // ones and the path's own separators (/, :, \) is percent-escaped.
  const path = anchor.editor_path.replace(/[^A-Za-z0-9\-._~/:\\]/gu, encodeURIComponent);
  return template.replace(/\{(path|line)\}/g, (_, name) => (name === "path" ? path : String(anchor.start ?? 1)));
}

function Snippet({ recordId, anchor }) {
  const [full, setFull] = useState(false);
  const snippet = useResource(
    `/api/records/${encodeURIComponent(recordId)}/anchors/${encodeURIComponent(anchor.entity_id)}/snippet`,
    { params: { full }, watch: WATCH },
  );
  // A read that finds the source gone invalidates the lines it replaced, for the next read too;
  // a refresh that merely fails keeps them.
  const lost = useRef(null);
  if (["not_found", "refused"].includes(snippet.error?.code)) lost.current = snippet.data;
  const unavailable = snippet.data != null && snippet.data === lost.current;
  if (snippet.error && (!snippet.data || unavailable)) {
    return html`<p class="snippet-note" role="alert">${describeError(snippet.error)}</p>`;
  }
  if (!snippet.data || unavailable) return html`<p class="snippet-note muted">Loading code…</p>`;
  const { lines, start, shifted } = snippet.data;
  // An anchor with no end runs to the end of its file: a full first page may have more behind it.
  const unbounded = anchor.end == null;
  const span = unbounded ? lines.length : anchor.end - anchor.start + 1;
  const more = unbounded ? lines.length >= FIRST_PAGE : span > FIRST_PAGE;
  return html`${shifted && html`<${Banner} tone="warn">Lines may have shifted since the graph was built.<//>`}
    <div class="code" role="region" tabindex="0" aria-label=${`Code of ${anchor.name}`}>
      ${lines.map(
        (line, index) =>
          html`<div class="code-line"><span class="ln" aria-hidden="true">${start + index}</span>${line}</div>`,
      )}
    </div>
    ${(more || full) &&
    html`<div class="snippet-more">
      <button type="button" class="btn" onClick=${() => setFull(!full)}>
        ${full ? "Show fewer lines" : unbounded ? "Show more lines" : `Show more (${Math.min(span, 200) - lines.length} more lines)`}
      </button>
      ${full && span > lines.length && html`<span class="muted small">Showing the first ${lines.length} of ${span} lines.</span>`}
    </div>`}
    ${snippet.error && html`<p class="snippet-note muted" role="alert">${describeError(snippet.error)}</p>`}`;
}

function AnchorRow({ recordId, anchor, editorUrl, firstView }) {
  // The first anchor with code opens by itself, as in the mockup; the others load on request.
  const [open, setOpen] = useState(firstView);
  const hasCode = anchor.file != null && anchor.start != null;
  return html`<div class=${`anchor anchor-${anchor.status_word}`}>
    <div class="anchor-head">
      <span class="mono anchor-name">${anchor.name}</span>
      <span class="small muted">${TIER_LABELS[anchor.tier]}</span>
      <${StatusWord} word=${anchor.status_word} />
      ${anchor.community && html`<span class="small muted">community: ${anchor.community}</span>`}
      <span class="anchor-spacer"></span>
      ${anchor.file && html`<span class="mono small">${anchor.file}${anchor.start != null ? `:${anchor.start}` : ""}</span>`}
      ${!anchor.file && anchor.name.startsWith("domain:") &&
      html`<a class="small" href=${`#/records?domain=${encodeURIComponent(anchor.name.slice(7))}`}>Records in this domain</a>`}
      ${anchor.editor_path &&
      html`<a class="small" href=${editorHref(editorUrl, anchor)}>Open in editor</a>`}
      ${hasCode &&
      html`<button type="button" class="btn" aria-expanded=${open} onClick=${() => setOpen(!open)}>
        ${open ? "Hide code" : "Show code"}
      </button>`}
    </div>
    ${open && hasCode && html`<${Snippet} recordId=${recordId} anchor=${anchor} />`}
  </div>`;
}

export function BoundToCode({ recordId, anchors, editorUrl }) {
  const first = anchors.findIndex((anchor) => anchor.file != null && anchor.start != null);
  return html`<section class="panel bound-code">
    <h2>Bound to code · ${anchors.length} ${anchors.length === 1 ? "anchor" : "anchors"}</h2>
    ${anchors.map(
      (anchor, index) =>
        html`<${AnchorRow} key=${anchor.entity_id} recordId=${recordId} anchor=${anchor} editorUrl=${editorUrl} firstView=${index === first} />`,
    )}
  </section>`;
}
