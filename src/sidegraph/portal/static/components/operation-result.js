// The outcome of an edit operation: what was written, what failed, what is left. A part that
// failed leaves the operation open, and Retry sends the same request again so it resumes.
// see design/superpowers/specs/2026-11-17-portal-design.md (D7, D8)
import { html } from "../ui.js";

const link = (id) => `#/records/${encodeURIComponent(id)}`;
const OUTCOME = { done: "done", done_now: "done now", failed: "failed", skipped: "waiting" };
const PART_LABEL = {
  drop_draft: "Reject the agent's draft",
  version: "Write the new version",
  close_predecessor: "Close the old version",
  bindings: "Copy the code anchors",
  cascade: "Settle the draft's other evidence",
};

const partLabel = (part) => (part.startsWith("fact:") ? "Write a supporting fact" : PART_LABEL[part] ?? part);

// Ids are shown whole: records written in one operation share the first eight characters.
// Only a finished operation links to its records; an open one keeps the person on this page to retry.
function Parts({ parts, linked }) {
  return html`<ol class="edit-parts">
    ${parts.map(
      (part) => html`<li key=${part.part} class=${`edit-part edit-part-${part.outcome}`}>
        <span>${partLabel(part.part)}</span>
        ${part.id && html`<span class="mono muted small edit-part-id">${linked ? html`<a href=${link(part.id)}>${part.id}</a>` : part.id}</span>`}
        <span class="edit-part-outcome">${OUTCOME[part.outcome] ?? part.outcome}</span>
        ${part.message && html`<span class="edit-part-message">${part.message}</span>`}
      </li>`,
    )}
  </ol>`;
}

/** `summary` says what happened to the old record; `onRetry` is shown while the operation is open. */
export function OperationResult({ result, summary, onRetry, retrying, error }) {
  if (result.state === "done") {
    return html`<div class="edit-result edit-result-ok" role="status">
        <strong>Saved${result.new_record_id && html` as <span class="mono">${result.new_record_id}</span>`}.</strong>
        <span>${summary}</span>
        ${result.new_record_id && html`<a class="edit-result-link" href=${link(result.new_record_id)}>Open the new version</a>`}
      </div>
      <${Parts} parts=${result.parts} linked=${true} />`;
  }
  return html`<div class="edit-result edit-result-warn" role="alert">
      <strong>Saving stopped part-way.</strong>
      <span>What is marked done stays written. Retry continues with the rest and writes nothing twice.</span>
      <button type="button" class="btn btn-primary" aria-disabled=${retrying} onClick=${onRetry}>Retry</button>
      ${error && html`<span class="edit-part-message">The last try failed: ${error}</span>`}
    </div>
    <${Parts} parts=${result.parts} linked=${false} />`;
}
