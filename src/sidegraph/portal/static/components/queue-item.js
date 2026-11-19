// One item of the ratification queue: the proposed record, its nested evidence, the signals
// (anchors, similar records, provenance), the agent's advice and the person's marks.
// Evidence has no marks of its own: it follows its decision's verdict (D6).
// see design/superpowers/specs/2026-11-17-portal-design.md (D6, D9)
import { html } from "../ui.js";
import { KindBadge } from "./kind-badge.js";
import { StatusWord } from "./status-word.js";

const short = (id) => id.slice(0, 8);
const link = (id) => `#/records/${encodeURIComponent(id)}`;
const VERDICT_LABEL = { accept: "Accept", reject: "Reject", edit: "Edit and accept" };

/** The id of a queue record: a domain carries `domain_id`. */
export const recordId = (record) => record.id ?? record.domain_id;

function Field({ label, tone, children }) {
  return html`<div class=${`queue-field${tone ? ` queue-field-${tone}` : ""}`}>
    <h3>${label}</h3>
    <p>${children}</p>
  </div>`;
}

function Body({ type, record }) {
  if (type === "fact") return html`<${Field} label="Source">${record.source}<//>`;
  if (type === "domain") {
    return html`<${Field} label="Summary">${record.summary}<//>
      ${record.path_prefixes?.length > 0 && html`<${Field} label="Path prefixes"><span class="mono">${record.path_prefixes.join(", ")}</span><//>`}
      ${record.seed_anchors?.length > 0 && html`<${Field} label="Seed anchors"><span class="mono">${record.seed_anchors.map((seed) => seed.name).join(", ")}</span><//>`}`;
  }
  return html`<${Field} label="Context">${record.context}<//>
    <${Field} label="Choice">${record.choice}<//>
    ${record.rejected && html`<${Field} label="Rejected" tone="rejected">${record.rejected}<//>`}
    ${record.consequences && html`<${Field} label="Consequences">${record.consequences}<//>`}`;
}

// What the cascade preview says will happen to an item of this batch. `evidence_of` lists the
// decisions a fact supports; they are named by what the person marked: a fact is accepted with
// a supporter marked Accept, rejected with every supporter marked Reject, and stays while one
// supporter is not rejected. `labels` maps a queue id to its title or statement; an id outside
// the queue is shown long enough to tell apart.
function previewText(outcome, labels, marks) {
  if (!outcome) return null;
  const supporters = outcome.evidence_of ?? [];
  const named = (ids) => ids.map((id) => labels[id] ?? id.slice(0, 12)).join(", ");
  const word = { accepted: "accepted", rejected: "rejected", dropped: "dropped" }[outcome.outcome];
  // A top-level item has no supporters to name.
  if (!supporters.length) return outcome.outcome === "stays" ? "Stays in the queue." : `Will be ${word}.`;
  if (outcome.outcome === "stays") {
    const alive = named(supporters.filter((id) => marks[id] !== "reject"));
    return alive
      ? `Stays in the queue: it also supports ${alive}, which this batch does not reject.`
      : "Stays in the queue: another decision it supports is not rejected.";
  }
  const who = named(supporters.filter((id) => marks[id] === (outcome.outcome === "accepted" ? "accept" : "reject")));
  return who ? `Will be ${word} with ${who}.` : `Will be ${word}.`;
}

// No control in here: a verdict on a fact is the verdict of the decision it supports.
function Evidence({ facts, preview, labels, marks }) {
  return html`<div class="queue-evidence">
    <h3>Supporting facts · ${facts.length} <span class="muted">(they follow this decision's verdict)</span></h3>
    <ul class="plain">
      ${facts.map((fact) => {
        const text = previewText(preview?.[fact.id], labels, marks);
        return html`<li key=${fact.id}>
          <span>${fact.statement}</span>
          <span class="muted small">${fact.source} · <span class="mono">${short(fact.id)}</span></span>
          ${text && html`<span class="queue-preview">${text}</span>`}
        </li>`;
      })}
    </ul>
  </div>`;
}

function Signals({ signals, provenance }) {
  const captured = [
    provenance.source,
    provenance.author,
    provenance.session_id && `session ${short(provenance.session_id)}`,
    provenance.commit && `commit ${provenance.commit.slice(0, 7)}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return html`<dl class="queue-signals">
    <div><dt>Anchors</dt><dd>
      ${signals.anchors.length
        ? signals.anchors.map(
            (anchor) => html`<span key=${anchor.entity_id} class="queue-anchor">
              <span class="mono">${anchor.name}</span> <${StatusWord} word=${anchor.status_word} />
            </span>`,
          )
        : html`<span class="muted">none</span>`}
    </dd></div>
    <div><dt>Similar</dt><dd>
      ${signals.similar.length
        ? html`<ul class="plain">${signals.similar.map(
            (row) => html`<li key=${row.id}>
              <a href=${link(row.id)}>${row.title}</a>
              <span class="muted small"><${KindBadge} row=${row} /> ${row.status}</span>
            </li>`,
          )}</ul>`
        : html`<span class="muted">none</span>`}
    </dd></div>
    <div><dt>Captured</dt><dd class="muted">${captured}</dd></div>
  </dl>`;
}

// "Reviewing…" is shown by the page only while the chat agent may still be writing advice.
function Advice({ advice, reviewing }) {
  const tone = advice ? advice.verdict : "none";
  return html`<aside class=${`queue-side advice-${tone}`} aria-label="Agent recommendation">
    <span class="queue-side-label">Agent recommends</span>
    ${advice
      ? html`<strong class="advice-verdict">${VERDICT_LABEL[advice.verdict]}</strong>
          <p>${advice.reason}</p>`
      : reviewing
        ? html`<strong class="advice-none-title">Reviewing…</strong>
            <p class="muted">The agent is still reviewing this item. You can decide without waiting.</p>`
        : html`<strong class="advice-none-title">No recommendation</strong>
            <p class="muted">No agent has recommended a verdict for this item. You can decide without it.</p>`}
  </aside>`;
}

function Actions({ type, id, mark, onMark }) {
  return html`<div class="queue-actions">
    <div class="queue-marks" role="group" aria-label="Verdict">
      <button type="button" class="btn btn-accept" aria-pressed=${mark === "accept"} onClick=${() => onMark(id, "accept")}>Accept</button>
      ${type !== "domain" && html`<a class="btn" href=${`#/edit/${encodeURIComponent(id)}?mode=accept`}>Edit and accept</a>`}
      <button type="button" class="btn btn-reject" aria-pressed=${mark === "reject"} onClick=${() => onMark(id, "reject")}>Reject</button>
    </div>
    <a class="queue-open" href=${link(id)}>Open full record</a>
  </div>`;
}

/**
 * `type` is decision | fact | domain; `item` a Queue item; `marks` maps ids to accept | reject;
 * `preview` the cascade preview's outcomes (or null); `labels` id → title; `onMark(id, verdict)` toggles a mark.
 */
export function QueueItem({ type, item, marks, onMark, preview, labels, reviewing }) {
  const { record, advice, signals } = item;
  const id = recordId(record);
  const title = record.title ?? record.statement;
  const mark = marks[id];
  const own = previewText(preview?.[id], labels, marks);
  return html`<article class=${`queue-item${mark ? ` queue-item-${mark}` : ""}`} aria-labelledby=${`queue-${id}`}>
    <div class="queue-main">
      <div class="queue-meta">
        <${KindBadge} row=${{ type, kind: record.kind }} />
        <span class="badge status-proposed queue-proposed">proposed</span>
        <span class="mono muted small" title=${id}>${short(id)}</span>
      </div>
      <h2 id=${`queue-${id}`}>${title}</h2>
      <${Body} type=${type} record=${record} />
      ${item.evidence?.length > 0 && html`<${Evidence} facts=${item.evidence} preview=${preview} labels=${labels} marks=${marks} />`}
      <${Signals} signals=${signals} provenance=${record.provenance} />
      ${mark &&
      html`<p class="queue-marked">
        <strong>Marked: ${mark}.</strong> ${own ?? ""} Nothing changes until you press Apply verdicts.
        <button type="button" class="queue-clear" onClick=${() => onMark(id, mark)}>Clear mark</button>
      </p>`}
      <${Actions} type=${type} id=${id} mark=${mark} onMark=${onMark} />
    </div>
    <${Advice} advice=${advice} reviewing=${reviewing} />
  </article>`;
}
