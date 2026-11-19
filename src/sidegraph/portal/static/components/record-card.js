// The record card: every field of a decision, fact or domain, its version chain, supporting
// facts, provenance, the ratifier stamp or the editor, the drift flag, and the code it is
// bound to. A record that is not on this branch gets its own explanation.
// see design/superpowers/specs/2026-11-17-portal-design.md (D5)
import { describeError } from "../api.js";
import { html, useRef } from "../ui.js";
import { BoundToCode } from "./anchor-snippet.js";
import { Banner, ErrorNotice } from "./banners.js";
import { KindBadge } from "./kind-badge.js";
import { useResource } from "./use-resource.js";

const WATCH = ["records", "graph", "sync"];

// Stamps may carry an offset; show the instant in UTC.
const when = (stamp) => {
  if (!stamp) return null;
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? stamp : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
};
const link = (id) => `#/records/${encodeURIComponent(id)}`;
const editLink = (id) => `#/edit/${encodeURIComponent(id)}`;

function Field({ label, tone, children }) {
  return html`<div class=${`field${tone ? ` field-${tone}` : ""}`}>
    <h2>${label}</h2>
    <p>${children}</p>
  </div>`;
}

function Fields({ record }) {
  if (record.statement !== undefined) {
    return html`<${Field} label="Statement">${record.statement}<//>
      <${Field} label="Source">${record.source}<//>
      ${record.supports?.length > 0 &&
      html`<div class="field"><h2>Supports</h2><ul class="plain">
        ${record.supports.map((id) => html`<li key=${id}><a class="mono" href=${link(id)}>${id}</a></li>`)}
      </ul></div>`}`;
  }
  if (record.summary !== undefined) {
    return html`<${Field} label="Summary">${record.summary}<//>
      <${Field} label="Slug"><span class="mono">${record.slug}</span><//>
      ${record.parent_id && html`<${Field} label="Parent"><a class="mono" href=${link(record.parent_id)}>${record.parent_id}</a><//>`}
      ${record.seed_anchors?.length > 0 &&
      html`<div class="field"><h2>Seed anchors</h2><ul class="plain">
        ${record.seed_anchors.map((seed) => html`<li key=${seed.name + (seed.file_path ?? "")}><span class="mono">${seed.name}</span>${seed.file_path && html` <span class="muted small mono">${seed.file_path}</span>`}</li>`)}
      </ul></div>`}
      ${record.path_prefixes?.length > 0 && html`<${Field} label="Path prefixes"><span class="mono">${record.path_prefixes.join(", ")}</span><//>`}
      ${record.communities?.length > 0 && html`<${Field} label="Communities"><span class="mono">${record.communities.join(", ")}</span><//>`}`;
  }
  return html`<${Field} label="Context">${record.context}<//>
    <${Field} label="Choice">${record.choice}<//>
    ${record.rejected && html`<${Field} label="Rejected" tone="rejected">${record.rejected}<//>`}
    ${record.consequences && html`<${Field} label="Consequences">${record.consequences}<//>`}
    ${record.retired && html`<${Field} label="Retired">${record.retired.reason}${record.retired.duplicate_of &&
    html` (duplicate of <a class="mono" href=${link(record.retired.duplicate_of)}>${record.retired.duplicate_of}</a>)`}<//>`}`;
}

function Row({ label, children }) {
  return html`<div class="meta-row"><span class="muted">${label}</span><span>${children}</span></div>`;
}

function RecordMeta({ record, drifted }) {
  const provenance = record.provenance;
  // A record ratified where git has no user name has a time and no ratifier. An edit made in the
  // portal, or a record a person added directly, has neither; it says who wrote it instead.
  const stamp = record.ratified_by
    ? `${record.ratified_by}${record.ratified_at ? `, ${when(record.ratified_at)}` : ""}`
    : record.ratified_at
      ? when(record.ratified_at)
      : provenance.source === "human" && provenance.author
        ? `Added or edited by ${provenance.author} (no ratification stamp)`
        : null;
  const captured = [provenance.source, provenance.author, provenance.ref, provenance.session_id]
    .filter(Boolean)
    .join(" · ");
  return html`<div class="panel">
    <h2>Record</h2>
    ${record.valid_from && html`<${Row} label="Valid from">${when(record.valid_from)}<//>`}
    ${record.valid_to && html`<${Row} label="Valid to">${when(record.valid_to)}<//>`}
    ${stamp && html`<${Row} label=${record.ratified_by ? "Ratified by" : "Accepted"}>${stamp}<//>`}
    <${Row} label="Captured">${captured}<//>
    ${provenance.graph_version && html`<${Row} label="Graph"><span class="mono" title=${provenance.graph_version}>${provenance.graph_version.slice(0, 12)}</span><//>`}
    ${provenance.commit && html`<${Row} label="Commit"><span class="mono" title=${provenance.commit}>${provenance.commit.slice(0, 12)}</span><//>`}
    ${record.supersedes && html`<${Row} label="Replaces"><a class="mono" href=${link(record.supersedes)}>${record.supersedes.slice(0, 8)}</a><//>`}
    <${Row} label="Content review">${drifted ? html`<strong class="drift-flag">needed</strong>` : "none needed"}<//>
  </div>`;
}

function History({ history, currentId }) {
  return html`<div class="panel">
    <h2>History · ${history.length} ${history.length === 1 ? "version" : "versions"}</h2>
    <ol class="history">
      ${[...history].reverse().map((row) => {
        const current = row.id === currentId;
        return html`<li key=${row.id} class=${current ? "history-current" : ""} aria-current=${current ? "true" : undefined}>
          <span class="history-dot" aria-hidden="true"></span>
          <span class="history-text">
            ${current ? html`<strong>This version</strong> ${row.title}` : html`<a href=${link(row.id)}>${row.title}</a>`}
            <span class="muted small"><span class="mono">${row.id.slice(0, 8)}</span> · ${row.status}</span>
          </span>
        </li>`;
      })}
    </ol>
  </div>`;
}

function Evidence({ evidence }) {
  return html`<div class="panel">
    <h2>Supporting facts · ${evidence.length}</h2>
    <ul class="plain">
      ${evidence.map(
        (fact) => html`<li key=${fact.id}>
          <a href=${link(fact.id)}>${fact.statement}</a>
          <span class="muted small">${fact.status} · ${fact.source}</span>
        </li>`,
      )}
    </ul>
  </div>`;
}

function Card({ card, session }) {
  const { record, history, evidence, anchors, drifted } = card;
  const id = record.id ?? record.domain_id;
  const type = record.domain_id ? "domain" : record.statement !== undefined ? "fact" : "decision";
  const current = history.find((row) => row.id === id);
  const domains = current?.domains ?? [];
  const title = record.title ?? record.statement;
  return html`<section class="page record-card">
    <p class="crumbs"><a href="#/records">Records</a> › <span class="mono">${id}</span></p>
    <div class="card-head">
      <div class="card-badges">
        <${KindBadge} row=${{ type, kind: record.kind }} />
        <span class=${`badge status-${record.status}`}>${record.status}</span>
        ${domains.map((slug) => html`<a key=${slug} class="badge badge-domain" href=${`#/records?domain=${encodeURIComponent(slug)}`}>domain: ${slug}</a>`)}
        ${(record.layer || record.scope) &&
        html`<span class="badge badge-chip">${[record.layer && `layer: ${record.layer}`, record.scope && `scope: ${record.scope}`].filter(Boolean).join(" · ")}</span>`}
      </div>
      <h1>${title}</h1>
      ${type !== "domain" && record.status === "accepted" && html`<p class="card-actions"><a class="btn" href=${editLink(id)}>Edit as new version</a></p>`}
    </div>
    ${drifted && html`<${Banner} tone="warn">This record needs a content review: the code it is bound to changed, or its last check no longer applies.<//>`}
    <div class="card-grid">
      <div class="panel card-fields"><${Fields} record=${record} /></div>
      <aside class="card-side">
        <${RecordMeta} record=${record} drifted=${drifted} />
        ${history.length > 1 && html`<${History} history=${history} currentId=${id} />`}
        ${evidence.length > 0 && html`<${Evidence} evidence=${evidence} />`}
      </aside>
    </div>
    ${anchors.length > 0 &&
    html`<${BoundToCode} recordId=${id} anchors=${anchors} editorUrl=${session.settings.editor_url} />`}
  </section>`;
}

export function RecordCard({ id, session }) {
  const card = useResource(`/api/records/${encodeURIComponent(id)}`, { watch: WATCH });
  // A read that finds the record gone (a branch switch) invalidates the payload it replaced:
  // it is not shown again while the next read runs or when that read fails.
  const lost = useRef(null);
  if (card.error?.code === "not_found") lost.current = card.data;
  const gone = card.data != null && card.data === lost.current;
  if (gone && !card.error) return html`<p class="page muted">Loading…</p>`;
  if (card.error && (!card.data || gone)) {
    return html`<section class="page">
      <p class="crumbs"><a href="#/records">Records</a> › <span class="mono">${id}</span></p>
      ${card.error.code === "not_found"
        ? html`<h1>Record not found</h1><p>${describeError(card.error)}</p>
            <p><a href="#/records">Back to the records</a></p>`
        : html`<${ErrorNotice} error=${card.error} onRetry=${card.reload} />`}
    </section>`;
  }
  if (!card.data) return html`<p class="page muted">Loading…</p>`;
  return html`${card.error && html`<${ErrorNotice} error=${card.error} onRetry=${card.reload} />`}<${Card} card=${card.data} session=${session} />`;
}
