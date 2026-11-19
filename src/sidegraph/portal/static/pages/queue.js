// Queue page: proposed decisions, standalone facts and domains, each with the agent's advice.
// A verdict is marked, then applied with one button, because applying cannot be undone. The
// cascade preview is asked again whenever a mark changes, over the whole marked batch.
// see design/superpowers/specs/2026-11-17-portal-design.md (D6, D9)
import { ApiError, describeError, post } from "../api.js";
import { Banner, ErrorNotice } from "../components/banners.js";
import { QueueItem, recordId } from "../components/queue-item.js";
import { useResource } from "../components/use-resource.js";
import { html, useEffect, useMemo, useRef, useState } from "../ui.js";

const WATCH = ["records", "graph", "advice", "sync"];
// The Queue's lists, each with the type its items are shown as.
const SECTIONS = [["decisions", "decision"], ["facts", "fact"], ["domains", "domain"]];
const OUTCOME_TEXT = {
  accepted: "Accepted",
  rejected: "Rejected",
  dropped: "Dropped",
  already_accepted: "Already accepted; nothing changed",
  already_rejected: "Already rejected; nothing changed",
  already_dropped: "Already dropped; nothing changed",
  stays: "Stays in the queue: another decision it supports is still alive",
  busy: "Not applied: the store was busy. The mark is kept; press Apply verdicts again",
};
// Marks outlive the page component: opening a record from a card and coming back keeps them.
const held = { marks: {} };
const short = (id) => id.slice(0, 8);
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

// True until `until` (an ISO time) passes; the page re-renders when it does.
function useReviewing(until) {
  const [, tick] = useState(0);
  const left = until ? Date.parse(until) - Date.now() : 0;
  useEffect(() => {
    if (!(left > 0)) return undefined;
    const timer = setTimeout(() => tick((n) => n + 1), left + 50);
    return () => clearTimeout(timer);
  }, [until, left > 0]);
  return left > 0;
}

function outcomeText(outcome) {
  if (outcome.outcome !== "refused") return OUTCOME_TEXT[outcome.outcome];
  const { message, details } = outcome.error;
  return describeError(new ApiError(409, "refused", message, details));
}

function ApplyResult({ outcomes, labels, onDismiss }) {
  // Apply is pressed at the bottom of a long page and its bar goes away with the marks: the
  // result is at the top, so focus moves to it.
  const panel = useRef(null);
  useEffect(() => panel.current?.focus(), [outcomes]);
  return html`<section class="panel queue-result" role="status" aria-label="Apply result" tabindex="-1" ref=${panel}>
    <h2>Apply result</h2>
    <ul class="plain">
      ${Object.entries(outcomes).map(([id, outcome]) => {
        const tone = ["refused", "busy"].includes(outcome.outcome) ? "bad" : "ok";
        return html`<li key=${id} class=${`queue-outcome queue-outcome-${tone}`}>
          <span><span class="mono small" title=${id}>${short(id)}</span> ${labels[id] ?? ""}</span>
          <strong>${outcomeText(outcome)}${outcome.evidence_of && html` <span class="muted">(evidence of ${outcome.evidence_of.map((id) => labels[id] ?? short(id)).join(", ")})</span>`}</strong>
        </li>`;
      })}
    </ul>
    <p><button type="button" class="btn" onClick=${onDismiss}>Dismiss</button></p>
  </section>`;
}

function ApplyBar({ accept, reject, ready, applying, checking, onApply }) {
  const marked = accept.length + reject.length;
  const parts = [accept.length && `${accept.length} accept`, reject.length && `${reject.length} reject`];
  return html`<div class="apply-bar" role="region" aria-label="Apply verdicts">
    <strong>${plural(marked, "verdict")} marked</strong>
    <span class="apply-parts">${parts.filter(Boolean).join(", ")}</span>
    <span class="apply-note">Applying cannot be undone: an accepted record stays accepted, a rejected one stays in history.</span>
    <button type="button" class="btn apply-btn" disabled=${!ready} onClick=${onApply}>
      ${applying ? "Applying…" : checking ? "Checking…" : "Apply verdicts"}
    </button>
  </div>`;
}

export default function Page({ session }) {
  const queue = useResource("/api/queue", { watch: WATCH });
  const [marks, setMarks] = useState(held.marks);
  const [preview, setPreview] = useState({ key: null, revision: null, outcomes: null, error: null });
  const [retry, setRetry] = useState(0);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState(null);
  const [applyError, setApplyError] = useState(null);
  // A second click lands before the first one's re-render, so the guard cannot be state.
  const sending = useRef(false);
  const reviewing = useReviewing(session.reviewing_until);

  const sections = SECTIONS.map(([key, type]) => ({ type, items: queue.data?.[key] ?? [] }));
  const entries = sections.flatMap(({ type, items }) => items.map((item) => ({ type, item, id: recordId(item.record) })));
  const accept = entries.filter(({ id }) => marks[id] === "accept").map(({ id }) => id);
  const reject = entries.filter(({ id }) => marks[id] === "reject").map(({ id }) => id);
  const key = JSON.stringify([accept, reject]);
  // What the preview was computed over: the marks and the queue under them.
  const revision = useMemo(() => JSON.stringify(queue.data), [queue.data]);
  // What Apply has sent is not restored by a return to this page: the request is still out.
  const page = useRef({ marks, sent: {} });
  page.current.marks = marks;
  useEffect(
    () => () => {
      held.marks = Object.fromEntries(
        Object.entries(page.current.marks).filter(([id, verdict]) => page.current.sent[id] !== verdict),
      );
    },
    [],
  );
  // What each id is called on screen, including nested evidence: previews and results name items by it.
  const labels = {};
  for (const { item, id } of entries) {
    labels[id] = item.record.title ?? item.record.statement;
    for (const fact of item.evidence ?? []) labels[fact.id] = fact.statement;
  }

  // A mark for an item that left the queue (decided elsewhere, a branch switch) goes with it.
  useEffect(() => {
    if (!queue.data) return;
    const present = new Set(entries.map(({ id }) => id));
    setMarks((previous) => {
      const kept = Object.entries(previous).filter(([id]) => present.has(id));
      return kept.length === Object.keys(previous).length ? previous : Object.fromEntries(kept);
    });
  }, [queue.data]);

  // The preview is asked again on every change of the marks, and of the queue under them. Only a
  // mark click or a retry is the person's own request; a refresh of the queue is not activity.
  const gesture = useRef(false);
  useEffect(() => {
    const none = { key: null, revision: null, outcomes: null, error: null };
    if (!accept.length && !reject.length) {
      setPreview(none);
      return undefined;
    }
    const user = gesture.current;
    gesture.current = false;
    const abort = new AbortController();
    setPreview(none);
    post("/api/queue/preview", { accept, reject }, { signal: abort.signal, user }).then(
      (data) => setPreview({ key, revision, outcomes: data.outcomes, error: null }),
      (error) => error.name !== "AbortError" && setPreview({ ...none, error }),
    );
    return () => abort.abort();
  }, [key, revision, retry]);

  const toggle = (id, verdict) => {
    gesture.current = true;
    setMarks((previous) => {
      const { [id]: current, ...rest } = previous;
      return current === verdict ? rest : { ...rest, [id]: verdict };
    });
  };

  const apply = async () => {
    if (sending.current) return;
    sending.current = true;
    setApplying(true);
    setApplyError(null);
    const sent = { ...Object.fromEntries(accept.map((id) => [id, "accept"])), ...Object.fromEntries(reject.map((id) => [id, "reject"])) };
    page.current.sent = sent;
    try {
      const { outcomes } = await post("/api/queue/apply", { accept, reject });
      setResult({ outcomes, labels });
      // What was sent and answered is settled; a busy store leaves its mark to try again, and a
      // mark made or changed while the request ran was never sent.
      setMarks((previous) =>
        Object.fromEntries(
          Object.entries(previous).filter(([id, verdict]) => sent[id] !== verdict || outcomes[id]?.outcome === "busy"),
        ),
      );
      queue.reload();
    } catch (error) {
      setApplyError(error);
    } finally {
      sending.current = false;
      page.current.sent = {};
      setApplying(false);
    }
  };

  const total = entries.length;
  const advised = entries.filter(({ item }) => item.advice).length;
  // The preview is shown while it matches the marks and the queue; Apply also waits out a refresh.
  const current = preview.key === key && preview.revision === revision;
  return html`<section class="page queue-page">
    <div class="queue-head">
      <h1>Waiting for ratification</h1>
      ${queue.data && html`<span class="muted">${total === 0
        ? "nothing waiting"
        : `${total} waiting · ${sections.filter(({ items }) => items.length).map(({ items, type }) => plural(items.length, type)).join(" · ")}`}</span>`}
    </div>
    ${queue.error && html`<${ErrorNotice} error=${queue.error} onRetry=${queue.reload} />`}
    ${!queue.data && !queue.error && html`<p class="muted">Loading…</p>`}
    ${queue.data && total === 0 && html`<p class="empty">Nothing is waiting for ratification.</p>`}
    ${total > 0 &&
    html`<${Banner}>
      <strong>Agent recommendations: ${advised} of ${total} ready.</strong>
      ${advised < total && reviewing && html`<span>The chat agent may still be reviewing; its cards update on their own.</span>`}
      <span>Recommendations are advice kept on this machine. Your click is the decision.</span>
    <//>`}
    ${result && html`<${ApplyResult} outcomes=${result.outcomes} labels=${result.labels} onDismiss=${() => setResult(null)} />`}
    ${applyError && html`<${ErrorNotice} error=${applyError} />`}
    ${preview.error && html`<${ErrorNotice} error=${preview.error} onRetry=${() => { gesture.current = true; setRetry(retry + 1); }} />`}
    <div class="queue-list">
      ${entries.map(
        ({ type, item, id }) => html`<${QueueItem}
          key=${id}
          type=${type}
          item=${item}
          marks=${marks}
          onMark=${toggle}
          preview=${current ? preview.outcomes : null}
          labels=${labels}
          reviewing=${reviewing}
        />`,
      )}
    </div>
    ${accept.length + reject.length > 0 &&
    html`<${ApplyBar}
      accept=${accept}
      reject=${reject}
      ready=${current && !queue.loading && !queue.error && !applying}
      applying=${applying}
      checking=${(!current || queue.loading) && !preview.error}
      onApply=${apply}
    />`}
  </section>`;
}
