// The edit view. #/edit/<id> saves a new version of an accepted record (the old one closes and
// stays in history); #/edit/<id>?mode=accept rejects an agent's draft and records the person's
// corrected text as a new accepted record. Both are one operation with a client key: the same
// request keeps its key on retry, so a lost response or a failed part never writes twice.
// see design/superpowers/specs/2026-11-17-portal-design.md (D7, D8)
import { describeError, post } from "../api.js";
import { OperationResult } from "../components/operation-result.js";
import { StatusWord } from "../components/status-word.js";
import { useResource } from "../components/use-resource.js";
import { html, useRef, useState } from "../ui.js";

const WATCH = ["records"];
const KINDS = ["adr", "lesson", "constraint", "gotcha"].map((kind) => [kind, kind]);
const LAYERS = [["", "none"], ["technical", "technical"], ["business", "business"]];
// An `optional` field may be emptied; an emptied one is sent as null, so the record then has none.
const DECISION_FIELDS = [
  { name: "title", label: "Title" },
  { name: "kind", label: "Kind", options: KINDS },
  { name: "layer", label: "Layer", options: LAYERS, optional: true },
  { name: "context", label: "Context", rows: 3 },
  { name: "choice", label: "Choice", rows: 4 },
  { name: "rejected", label: "Rejected", hint: "what was tried and abandoned, and why", rows: 3, optional: true },
  { name: "consequences", label: "Consequences", rows: 2, optional: true },
];
const FACT_FIELDS = [
  { name: "statement", label: "Statement", rows: 3 },
  { name: "source", label: "Source" },
];
const ROUTES = {
  edit: (id) => `/api/records/${encodeURIComponent(id)}/edit`,
  "edit-and-accept": (id) => `/api/records/${encodeURIComponent(id)}/edit-and-accept`,
  "save-as-new": (id) => `/api/records/${encodeURIComponent(id)}/save-as-new`,
};

const link = (id) => `#/records/${encodeURIComponent(id)}`;
const editLink = (id) => `#/edit/${encodeURIComponent(id)}`;
const short = (id) => id.slice(0, 8);
const typeOf = (record) => (record.domain_id ? "domain" : record.statement !== undefined ? "fact" : "decision");
const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
const QUEUE = [["#/queue", "Open the queue"]];
const NO_CHANGE = "Nothing changed. To confirm that this record still holds, record a drift check instead.";

// The refusals the card already shows before any request: nothing to type into.
function preflight(card, mode, queue) {
  const { record, history } = card;
  const id = record.id ?? record.domain_id;
  if (typeOf(record) === "domain") {
    return { text: "Domains are not edited. Accept or reject them in the queue.", links: QUEUE };
  }
  if (mode === "accept") {
    if (record.status !== "proposed") {
      return { text: "This record is no longer a proposal, so there is nothing to accept.", links: [[link(id), "Open the record"]] };
    }
    // Evidence nested under a proposed decision has no verdict of its own (D6): only a fact the queue lists by itself is edited here.
    if (typeOf(record) === "fact" && queue && !queue.facts.some((item) => item.record.id === id)) {
      return { text: "This fact is evidence for a proposed decision, so it follows that decision's verdict. Review it in the queue.", links: QUEUE };
    }
    return null;
  }
  if (record.status === "proposed") {
    return {
      text: "This record is still a proposal. To correct it, use Edit and accept.",
      // A fact may be evidence nested under a decision; the queue says which actions it has.
      links: typeOf(record) === "fact" ? QUEUE : [[`${editLink(id)}?mode=accept`, "Edit and accept"]],
    };
  }
  if (record.status !== "accepted") {
    const head = history.at(-1)?.id;
    return {
      text: `This version is ${record.status}, so it cannot be edited.`,
      links: head && head !== id ? [[link(head), "Open the current version"], [editLink(head), "Edit the current version"]] : [[link(id), "Open the record"]],
    };
  }
  if (card.pending_successor) {
    return {
      text: "An agent's proposal to replace this record is waiting in the queue. Decide on it first.",
      links: QUEUE,
    };
  }
  if (card.pending_evidence.length > 0) {
    return {
      text: `This record has ${plural(card.pending_evidence.length, "pending evidence fact", "pending evidence facts")}; decide on them in the queue first.`,
      links: QUEUE,
    };
  }
  return null;
}

// A failed request, as {text, links, startAgain, saveAsNew}: what to say and what the person can do.
function explain(error, mode) {
  const { code, details } = error;
  const keeping = mode === "accept" ? "keeping" : "carrying over";
  if (code === "not_current") {
    const head = details.head;
    return {
      text: mode === "accept" ? "This draft was already decided." : "This record has a newer version.",
      links: [],
      compare: head && mode !== "accept" ? head : null,
      startAgain: head && mode !== "accept" ? editLink(head) : true,
      saveAsNew: details.offer_save_as_new === true,
    };
  }
  if (code === "pending_successor") {
    return { text: "An agent's proposal to replace this record is waiting in the queue. Decide on it first.", links: QUEUE };
  }
  if (code === "pending_proposal") return { text: "This record is still a proposal. Review it in the queue.", links: QUEUE };
  if (code === "pending_evidence") {
    const count = details.ids?.length ?? 0;
    return {
      text: `This record has ${plural(count, "pending evidence fact", "pending evidence facts")}; decide on them in the queue first.`,
      links: QUEUE,
    };
  }
  if (code === "evidence_conflict") {
    return {
      text: `A fact you are ${keeping} changed, or has a replacement waiting in the queue.`,
      links: [...(details.id ? [[link(details.id), "Open that fact"]] : []), ...(details.pending ? QUEUE : [])],
      startAgain: true,
    };
  }
  if (code === "op_key_reused") return { text: "This save was already used for different text.", links: [], startAgain: true };
  return { text: details.field ? `${describeError(error)} (field: ${details.field})` : describeError(error), links: [] };
}

// No answer arrived, so the save may have landed: only the same request, with its key, is safe to send again.
// Answers that say nothing about whether the first try landed (the store was busy, the portal refused for its own reasons).
// A `no_change` refusal is not one of them: it says the text equals the record, so nothing was written.
const INCONCLUSIVE = ["network", "server", "busy", "refused"];
const UNRESOLVED = {
  text: "The portal did not confirm that saving finished. Retry sends the same request again, so nothing is written twice.",
  links: [],
  retry: true,
};

function Callout({ tone, children }) {
  return html`<div class=${`edit-callout edit-callout-${tone}`}>${children}</div>`;
}

function Refusal({ refusal }) {
  return html`<${Callout} tone="warn">
    <span role="alert">${refusal.text}</span>
    <span class="edit-links">${refusal.links.map(([href, label]) => html`<a key=${href} class="btn" href=${href}>${label}</a>`)}</span>
  <//>`;
}

function Field({ spec, value, base, locked, onInput }) {
  const id = `edit-${spec.name}`;
  const changed = value !== base;
  const props = {
    id,
    value,
    disabled: locked,
    class: `edit-input${changed ? " is-changed" : ""}`,
    "aria-describedby": changed ? `${id}-changed` : undefined,
    onInput: (event) => onInput(event.target.value),
  };
  return html`<div class="edit-field">
    <label for=${id}>${spec.label}${spec.hint && html` <span class="muted edit-hint">· ${spec.hint}</span>`}</label>
    ${spec.options
      ? html`<select ...${props}>${spec.options.map(([option, label]) => html`<option key=${option} value=${option}>${label}</option>`)}</select>`
      : spec.rows
        ? html`<textarea ...${props} rows=${spec.rows}></textarea>`
        : html`<input ...${props} type="text" />`}
    ${changed && html`<span id=${`${id}-changed`} class="edit-changed">Changed</span>`}
  </div>`;
}

function Diff({ specs, base, values }) {
  return specs.map(
    (spec) => html`<div key=${spec.name} class="edit-diff">
      <span class="edit-diff-name">${spec.label}</span>
      <div class="diff-line diff-del">
        <span aria-hidden="true">− </span><span class="visually-hidden">Before: </span>${base[spec.name] || html`<span class="muted">(empty)</span>`}
      </div>
      <div class="diff-line diff-add">
        <span aria-hidden="true">+ </span><span class="visually-hidden">After: </span>${values[spec.name] || html`<span class="muted">(empty)</span>`}
      </div>
    </div>`,
  );
}

// "Compare" on a newer version: the person's text against it, shown here so the typed text is not lost.
function Compare({ head, specs, values }) {
  const card = useResource(`/api/records/${encodeURIComponent(head)}`);
  if (!card.data) {
    return html`<p class="muted">${card.error ? describeError(card.error) : "Loading the current version…"}</p>`;
  }
  const current = Object.fromEntries(specs.map((spec) => [spec.name, card.data.record[spec.name] ?? ""]));
  const differing = specs.filter((spec) => current[spec.name] !== values[spec.name]);
  return html`<div class="edit-compare">
    <strong>Your text against the current version <span class="mono">${head}</span> · ${plural(differing.length, "field differs", "fields differ")}</strong>
    <span class="muted small">− the current version, + your text</span>
    <${Diff} specs=${differing} base=${current} values=${values} />
  </div>`;
}

function Evidence({ title, note, facts, shared, chosen, locked, onToggle }) {
  if (!facts.length && !shared.length) return null;
  return html`<div class="panel">
    <h2>${title} · ${facts.length + shared.length}</h2>
    ${facts.length > 0 && html`<p class="muted small edit-note-line">${note}</p>`}
    <ul class="plain">
      ${facts.map(
        (fact) => html`<li key=${fact.id}>
          <label class="check">
            <input type="checkbox" checked=${chosen.includes(fact.id)} disabled=${locked} onChange=${() => onToggle(fact.id)} />
            <span>${fact.statement}</span>
          </label>
          <span class="muted small">${fact.source} · <span class="mono">${fact.id}</span></span>
        </li>`,
      )}
      ${shared.map(
        (fact) => html`<li key=${fact.id}>
          <span>${fact.statement}</span>
          <span class="muted small">${fact.status === "proposed" ? "Also supports another proposal, so it stays in the queue" : `Already ${fact.status}, so it is not changed`} · <span class="mono">${fact.id}</span></span>
        </li>`,
      )}
    </ul>
  </div>`;
}

function WhatSavingDoes({ mode, id, supersedes, anchors, kept, unkept }) {
  const copy = anchors ? plural(anchors, "code anchor", "code anchors") : null;
  const steps =
    mode === "save-as-new"
      ? [
          "Writes your text as a separate new accepted record, with you as its author.",
          copy ? `Gives it the draft's ${copy}.` : "The draft has no code anchors to copy.",
          `Leaves ${id} as it is: it was already decided.`,
        ]
      : mode === "accept"
      ? [
          `Rejects the agent's draft ${id}. It stays in history as rejected.`,
          "Writes your text as a new accepted record, with you as its author.",
          supersedes && `Closes ${supersedes}, which the draft would have replaced: it is marked superseded. Nothing is deleted.`,
          copy ? `Gives it exactly the draft's ${copy}.` : "The draft has no code anchors to copy.",
          kept ? `Keeps ${plural(kept, "supporting fact", "supporting facts")} as accepted.` : unkept && "Keeps none of its supporting facts; they are rejected with the draft.",
        ]
      : [
          `Writes a new record that replaces ${id}, accepted at once, with you as its author.`,
          `Closes ${id}: its validity ends now and it is marked superseded. Nothing is deleted.`,
          copy ? `Copies the ${copy} to the new version unchanged.` : "The record has no code anchors to copy.",
          kept ? `Carries over ${plural(kept, "supporting fact", "supporting facts")} to the new version.` : "Carries over no supporting facts.",
        ];
  return html`<div class="panel">
    <h2>What saving does</h2>
    <ol class="edit-steps">
      ${[...steps.filter(Boolean), "Leaves the new files uncommitted. The portal never commits."].map((step) => html`<li key=${step}>${step}</li>`)}
    </ol>
  </div>`;
}

function Anchors({ anchors, mode }) {
  return html`<div class="panel">
    <h2>Anchors (${mode === "accept" ? "the new record gets the draft's" : "kept as they are"})</h2>
    ${anchors.length === 0 && html`<span class="muted">No code anchors.</span>`}
    <ul class="plain">
      ${anchors.map(
        (anchor) => html`<li key=${anchor.entity_id}>
          <span><span class="mono">${anchor.name}</span> <${StatusWord} word=${anchor.status_word} /></span>
          ${anchor.file && html`<span class="muted small mono">${anchor.file}${anchor.start ? `:${anchor.start}` : ""}</span>`}
        </li>`,
      )}
    </ul>
    <span class="muted small">Editing anchors is planned for a later version.</span>
  </div>`;
}

// `latest` is the card as the live stream last fetched it; the form itself keeps the card it opened on.
function EditForm({ card, latest, mode, id }) {
  const { record, anchors } = card;
  const specs = typeOf(record) === "fact" ? FACT_FIELDS : DECISION_FIELDS;
  const base = Object.fromEntries(specs.map((spec) => [spec.name, record[spec.name] ?? ""]));
  const [values, setValues] = useState(base);
  // Evidence the person can keep: an accepted record carries its accepted facts over; a draft
  // keeps the facts the card names (the ones Edit and accept treats as this draft's own). The rest is not touched.
  const evidence =
    mode === "accept"
      ? card.evidence.filter((fact) => card.keepable_evidence.includes(fact.id))
      : card.evidence.filter((fact) => fact.status === "accepted");
  const shared = mode === "accept" ? card.evidence.filter((fact) => !evidence.includes(fact)) : [];
  const [chosen, setChosen] = useState(() => evidence.map((fact) => fact.id));
  const [outcome, setOutcome] = useState(null);
  const [sendingNow, setSendingNow] = useState(false);
  const [comparing, setComparing] = useState(false);
  const sending = useRef(false);
  const attempt = useRef(null);

  const failure = outcome?.error && !outcome.result && (outcome.unresolved ? UNRESOLVED : explain(outcome.error, mode));
  // While a request is out, or its answer never came, the text and the selection stay as sent.
  const locked = sendingNow || Boolean(failure?.retry);
  const changed = specs.filter((spec) => values[spec.name] !== base[spec.name]);
  const notKept = evidence.filter((fact) => !chosen.includes(fact.id));
  const empty = changed.find((spec) => !spec.optional && values[spec.name].trim() === "");
  // An accepted record may be saved without a field change only to drop evidence; a draft is
  // accepted with a correction, otherwise it is Accept in the queue. Confirming that a record
  // still holds is a drift check, not a new version.
  const allowsNoFieldChange = mode === "version" && notKept.length > 0;
  const nothingChanged = changed.length === 0 && !allowsNoFieldChange;
  const problem =
    nothingChanged
      ? mode === "accept"
        ? "Change at least one field to save. To accept the draft as it is, use Accept in the queue."
        : NO_CHANGE
      : empty
        ? `${empty.label} cannot be empty.`
        : null;
  const fields = Object.fromEntries(
    changed.map((spec) => [spec.name, spec.optional && values[spec.name] === "" ? null : values[spec.name]]),
  );
  const kept = evidence.filter((fact) => chosen.includes(fact.id)).map((fact) => fact.id);
  // Save as new is its own operation: the preview and the evidence follow it, also while its answer is awaited.
  const recovery = Boolean(failure?.saveAsNew) || outcome?.route === "save-as-new";
  // Select fields share a row (kind and layer).
  const groups = specs.reduce((rows, spec) => {
    if (spec.options && rows.at(-1)?.[0].options) rows.at(-1).push(spec);
    else rows.push([spec]);
    return rows;
  }, []);
  const stale = !outcome && !sendingNow && (latest.error?.code === "not_found" || (latest.data && latest.data.record.status !== record.status));

  // One request at a time. The key is minted once per request and kept with it: sending the
  // same request again (a retry) sends the same key, a different request gets a new one.
  async function send(route, payload) {
    if (sending.current) return;
    sending.current = true;
    setSendingNow(true);
    const body = JSON.stringify([route, payload]);
    const key = attempt.current?.body === body ? attempt.current.key : crypto.randomUUID();
    const unresolved = attempt.current?.body === body && attempt.current.unresolved;
    attempt.current = { body, key, route, payload, unresolved };
    try {
      setOutcome({ route, result: await post(ROUTES[route](id), { op_key: key, ...payload }) });
      attempt.current.unresolved = false;
    } catch (error) {
      // A busy answer to a retry says nothing about the first try: the save stays unresolved until an answer settles it.
      const open = ["network", "server"].includes(error.code) || (unresolved && INCONCLUSIVE.includes(error.code) && error.details?.reason !== "no_change");
      attempt.current.unresolved = open;
      // The last result stays on screen: a failed retry must not hide what was already written.
      // (A plan that wrote nothing gives way to the server's offer to save the text as a new record.)
      setOutcome((previous) => ({
        route,
        error,
        result: previous?.result?.state === "planned" && error.code === "not_current" ? undefined : previous?.result,
        unresolved: open,
      }));
    } finally {
      sending.current = false;
      setSendingNow(false);
    }
  }
  const save = () => {
    if (problem) return;
    send(mode === "accept" ? "edit-and-accept" : "edit", {
      fields,
      [mode === "accept" ? "keep_facts" : "carry_facts"]: kept,
    });
  };
  const saveAsNew = () => {
    if (problem) return;
    send("save-as-new", { fields });
  };
  const retry = () => send(attempt.current.route, attempt.current.payload);
  const toggle = (factId) =>
    setChosen((now) => (now.includes(factId) ? now.filter((other) => other !== factId) : [...now, factId]));

  if (outcome?.result) {
    const problemText = outcome.error && describeError(outcome.error);
    const summary = {
      edit: `${id} is now superseded and kept in history.`,
      "edit-and-accept": `The agent's draft ${id} is rejected and kept in history.`,
      "save-as-new": "Your text is saved as a new accepted record.",
    }[outcome.route];
    return html`<${OperationResult} result=${outcome.result} summary=${summary} onRetry=${retry} retrying=${sendingNow} error=${problemText} />`;
  }
  return html`${stale &&
    html`<${Callout} tone="warn">
      <span role="alert">This record changed, or is no longer on this branch, since you opened it. Saving may be refused.</span>
      <span class="edit-links"><button type="button" class="btn" onClick=${() => location.reload()}>Start again</button></span>
    <//>`}
    ${card.drifted &&
    html`<${Callout} tone="warn"><span>${nothingChanged && mode === "version"
      ? "This record is marked for content review. To confirm that it still holds, record a drift check."
      : "This record is marked for content review. Saving records that this text holds for the current code."}</span><//>`}
    <div class="edit-layout">
      <form class="panel edit-form" onSubmit=${(event) => event.preventDefault()}>
        ${groups.map(
          (group) => html`<div key=${group[0].name} class="edit-row">${group.map(
            (spec) => html`<${Field}
              key=${spec.name}
              spec=${spec}
              value=${values[spec.name]}
              base=${base[spec.name]}
              locked=${locked}
              onInput=${(value) => setValues((now) => ({ ...now, [spec.name]: value }))}
            />`,
          )}</div>`,
        )}
      </form>
      <aside class="card-side">
        <${WhatSavingDoes}
          mode=${recovery ? "save-as-new" : mode}
          id=${id}
          supersedes=${mode === "accept" ? record.supersedes : null}
          anchors=${anchors.length}
          kept=${kept.length}
          unkept=${notKept.length}
        />
        ${!recovery && html`<${Evidence}
          title=${mode === "accept" ? "Keep this evidence" : "Carry over this evidence"}
          note=${mode === "accept"
            ? "Kept facts are accepted with your text. Facts left unchecked are rejected with the draft."
            : "Facts left unchecked stay attached to the old version."}
          facts=${evidence}
          shared=${shared}
          chosen=${chosen}
          locked=${locked}
          onToggle=${toggle}
        />`}
        <${Anchors} anchors=${anchors} mode=${mode} />
      </aside>
    </div>
    <section class="panel edit-review">
      <h2>Review the changes · ${plural(changed.length, "field", "fields")}</h2>
      ${changed.length === 0 && notKept.length === 0 && html`<p class="muted">Nothing changed yet.</p>`}
      <${Diff} specs=${changed} base=${base} values=${values} />
      ${notKept.length > 0 && !recovery &&
      html`<div class="edit-diff">
        <span class="edit-diff-name">${mode === "accept" ? "Evidence not kept" : "Evidence not carried over"}</span>
        ${notKept.map((fact) => html`<div key=${fact.id} class="diff-line diff-del"><span aria-hidden="true">− </span><span class="visually-hidden">Not kept: </span>${fact.statement}</div>`)}
      </div>`}
      ${failure &&
      html`<${Callout} tone="warn">
        <span role="alert">${failure.text}</span>
        <span class="edit-links">
          ${failure.links.map(([href, label]) => html`<a key=${href} class="btn" href=${href}>${label}</a>`)}
          ${failure.compare && html`<button type="button" class="btn" aria-expanded=${comparing} onClick=${() => setComparing(!comparing)}>Compare with the current version</button>`}
          ${failure.startAgain &&
          (failure.startAgain === true
            ? html`<button type="button" class="btn" onClick=${() => location.reload()}>Start again</button>`
            : html`<a class="btn" href=${failure.startAgain}>Start again</a>`)}
          ${failure.retry && html`<button type="button" class="btn btn-primary" aria-disabled=${sendingNow} onClick=${retry}>Retry</button>`}
          ${failure.saveAsNew &&
          html`<button type="button" class="btn btn-primary" aria-disabled=${Boolean(problem) || sendingNow} onClick=${saveAsNew}>Save your text as a new record</button>`}
        </span>
      <//>`}
      ${comparing && failure?.compare && html`<${Compare} head=${failure.compare} specs=${specs} values=${values} />`}
      <div class="edit-actions">
        ${!failure?.retry &&
        html`<button type="button" class="btn btn-primary" aria-disabled=${Boolean(problem) || sendingNow} onClick=${save}>
          ${mode === "accept" ? "Save and accept" : "Save as new version"}
        </button>`}
        <a class="btn" href=${link(id)}>Cancel</a>
        ${problem && !failure?.retry && html`<span class="muted" role="status">${problem}</span>`}
      </div>
    </section>`;
}

function EditView({ id, mode }) {
  const card = useResource(`/api/records/${encodeURIComponent(id)}`, { watch: WATCH });
  // Only a proposed fact needs the queue: nested evidence is not edited on its own.
  const needsQueue = mode === "accept" && card.data != null && typeOf(card.data.record) === "fact" && card.data.record.status === "proposed";
  const queue = useResource("/api/queue", { watch: ["records", "advice"], enabled: needsQueue });
  // The form opens on the card as it was when the page passed its checks and keeps it: a refetch
  // while the person types must neither reset the text nor swap the form for a refusal.
  const opened = useRef(null);
  const waiting = needsQueue && !queue.data;
  const refusal = card.data && !waiting ? preflight(card.data, mode, queue.data) : null;
  if (!opened.current && card.data && !waiting && !refusal) opened.current = card.data;
  const title = mode === "accept" ? "Edit and accept" : "Edit as new version";
  const crumbs = html`<p class="crumbs"><a href="#/records">Records</a> › <a class="mono" href=${link(id)}>${short(id)}</a> › ${title}</p>`;
  if (!opened.current && !refusal) {
    const error = card.error ?? (needsQueue ? queue.error : null);
    return html`<section class="page edit-page">${crumbs}
      ${error ? html`<${Callout} tone="warn"><span role="alert">${describeError(error)}</span>
        <span class="edit-links"><button type="button" class="btn" onClick=${card.error ? card.reload : queue.reload}>Try again</button></span><//>` : html`<p class="muted">Loading…</p>`}
    </section>`;
  }
  return html`<section class="page edit-page">
    ${crumbs}
    <h1>${title}</h1>
    ${opened.current &&
    html`<${Callout} tone="info">
      <span class="edit-note-text">
        <strong>${mode === "accept" ? "Saving does not edit the agent's draft. It rejects the draft and records your text as a new accepted record." : "Saving does not overwrite this record. It creates a new version."}</strong>
        <span>${mode === "accept"
          ? "Sidegraph memory is append-only. The draft stays in the record's history as rejected, so what the agent proposed is never lost."
          : "Sidegraph memory is append-only. The current record closes and moves to history as superseded; the new one replaces it everywhere memory is read. Both stay in the record's history."}</span>
      </span>
    <//>`}
    ${opened.current
      ? html`<${EditForm} card=${opened.current} latest=${card} mode=${mode} id=${id} />`
      : html`<${Refusal} refusal=${refusal} />`}
  </section>`;
}

export default function Page({ route }) {
  const [id] = route.rest;
  const mode = route.query.get("mode") === "accept" ? "accept" : "version";
  if (!id) {
    return html`<section class="page"><h1>Edit</h1><p>Open a record from the <a href="#/records">records</a> to edit it.</p></section>`;
  }
  // Keyed: another record or mode starts a fresh form, never the previous one's text.
  return html`<${EditView} key=${`${id}:${mode}`} id=${id} mode=${mode} />`;
}
