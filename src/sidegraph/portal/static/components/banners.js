// Notices: the shell's banner strip (unchecked anchors, newer graph, busy store, lost stream)
// and the Banner and ErrorNotice building blocks pages reuse.
// see design/superpowers/specs/2026-11-17-portal-design.md (D5, D11)
import { ApiError, describeError } from "../api.js";
import { html } from "../ui.js";
import { useLiveState } from "./use-resource.js";

const STOPPED =
  "The portal has stopped. Open it again from your agent (open_portal) or run sidegraph-portal.";

/** `tone` is info | warn. */
export function Banner({ tone = "info", children }) {
  return html`<div class=${`banner banner-${tone}`} role="status">${children}</div>`;
}

/** A failed request, as text, with an optional retry. */
export function ErrorNotice({ error, onRetry }) {
  // A lost or stopped portal is already said once, in the shell; each failed request would
  // repeat it, and so would a page module or a fetch that fails (a TypeError, not an ApiError).
  const gone = ["reconnecting", "stopped"].includes(useLiveState());
  if (gone && (error?.code === "network" || !(error instanceof ApiError))) return null;
  return html`<div class="banner banner-warn" role="alert">
    <span>${describeError(error)}</span>
    ${onRetry && html`<button type="button" class="btn" onClick=${onRetry}>Try again</button>`}
  </div>`;
}

export function ShellBanners({ session, liveState, busy }) {
  if (liveState === "stopped") {
    return html`<div class="banners"><${Banner} tone="warn">${STOPPED}<//></div>`;
  }
  const notices = [];
  if (busy > 0) notices.push(["warn", "The store is busy, retrying."]);
  if (liveState === "stale") notices.push(["warn", "Could not refresh; retrying."]);
  if (liveState === "reconnecting") {
    notices.push(["warn", "The connection to the portal was lost. Reconnecting."]);
  }
  // A cold index shows every badge as unchecked, so none is "from the last sync": the
  // newer-graph notice stands alone only for a synced index.
  if (session.index_cold) {
    const rebuilt = session.graph_newer_than_sync ? ", and the graph was rebuilt since the last sync" : "";
    notices.push([
      "info",
      `Anchor status not checked since the index was rebuilt${rebuilt}. It is updated by the next sync (a commit, a new session, or sidegraph-sync).`,
    ]);
  } else if (session.graph_newer_than_sync) {
    notices.push([
      "info",
      "The graph was rebuilt since the last sync. Anchor status is from the last sync.",
    ]);
  }
  if (!notices.length) return null;
  return html`<div class="banners">
    ${notices.map(([tone, text]) => html`<${Banner} key=${text} tone=${tone}>${text}<//>`)}
  </div>`;
}
