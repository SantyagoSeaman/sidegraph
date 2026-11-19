// The portal shell: token capture, routing by URL hash, the page registry, header, banners,
// footer. A page is one module under pages/ with a default export `Page({route, session})`,
// registered below; its sub-routes (a record id, an edit form) are its own business and
// arrive as route.rest. The server serves only "/", so routes live in the hash: #/records/<id>.
// see design/superpowers/specs/2026-11-17-portal-design.md (D3, D11, D13)
import { captureToken, isClosed, onClosed } from "./api.js";
import { ErrorNotice, ShellBanners } from "./components/banners.js";
import { ClosedPage, NoTokenPage } from "./components/auth-pages.js";
import { Footer } from "./components/footer.js";
import { Header } from "./components/header.js";
import { useBusy, useLiveState, useResource } from "./components/use-resource.js";
import { start as startLive } from "./live.js";
import { html, render, useEffect, useState } from "./ui.js";

const PAGES = [
  { key: "graph", label: "Graph", load: () => import("./pages/graph.js") },
  { key: "records", label: "Records", load: () => import("./pages/records.js") },
  { key: "queue", label: "Queue", badge: true, load: () => import("./pages/queue.js") },
  { key: "agent-view", label: "Agent view", load: () => import("./pages/agent-view.js") },
  // Reached from the graph panel, a record card and the queue, not from the navigation.
  { key: "edit", label: "Edit", hidden: true, load: () => import("./pages/edit.js") },
];
const EVERY_CHANGE = ["records", "graph", "advice", "sync"];

function parseRoute() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  let segments;
  try {
    segments = path.split("/").filter(Boolean).map(decodeURIComponent);
  } catch {
    // A malformed %-escape is an unknown page, not a blank screen.
    segments = ["?"];
  }
  const [name = PAGES[0].key, ...rest] = segments;
  return {
    page: PAGES.some((page) => page.key === name) ? name : null,
    name,
    rest,
    query: new URLSearchParams(query),
  };
}

// A launch link opened in a tab that already shows the portal (or the open-from-link page):
// reload with the fragment intact, so startup takes the token the way a first load does.
addEventListener("hashchange", (event) => {
  if (!new URLSearchParams(location.hash.slice(1)).has("t")) return;
  event.stopImmediatePropagation();
  location.reload();
});

function useRoute() {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const update = () => setRoute(parseRoute());
    addEventListener("hashchange", update);
    return () => removeEventListener("hashchange", update);
  }, []);
  return route;
}

function PageView({ route, session }) {
  const entry = PAGES.find((page) => page.key === route.page);
  const [Page, setPage] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!entry) return undefined;
    let current = true;
    setPage(null);
    setError(null);
    entry.load().then(
      (module) => current && setPage(() => module.default),
      (failure) => current && setError(failure),
    );
    return () => {
      current = false;
    };
  }, [route.page]);
  if (!entry) {
    return html`<section class="page">
      <h1>No such page</h1>
      <p><a href="#/graph">Back to the graph</a></p>
    </section>`;
  }
  if (error) return html`<section class="page"><${ErrorNotice} error=${error} /></section>`;
  if (!Page) return html`<p class="page muted">Loading…</p>`;
  return html`<${Page} route=${route} session=${session} />`;
}

function Portal() {
  const route = useRoute();
  const [closed, setClosed] = useState(isClosed());
  const session = useResource("/api/session", { watch: EVERY_CHANGE });
  // The queue is asked for only after the session answered, so a refused token costs one request.
  const queue = useResource("/api/queue", { watch: EVERY_CHANGE, enabled: Boolean(session.data) });
  const liveState = useLiveState();
  const busy = useBusy();
  const ready = Boolean(session.data);

  useEffect(() => onClosed(() => setClosed(true)), []);
  useEffect(() => {
    if (ready) startLive(session.data.settings.poll_seconds);
  }, [ready]);
  useEffect(() => {
    const entry = PAGES.find((page) => page.key === route.page);
    document.title = `Sidegraph Portal${entry ? ` — ${entry.label}` : ""}`;
  }, [route.page]);

  if (closed) return html`<${ClosedPage} />`;
  if (!ready) {
    return session.error
      ? html`<main class="auth-page"><${ErrorNotice} error=${session.error} onRetry=${session.reload} /></main>`
      : html`<main class="auth-page">
          <p class="muted">${busy > 0 ? "The store is busy, retrying." : "Loading…"}</p>
        </main>`;
  }
  // A refresh that failed keeps the last counts on screen, so say so.
  const failed = [session, queue].find((resource) => resource.error);
  const retryFailed = () => [session, queue].forEach((resource) => resource.error && resource.reload());
  const count = queue.data
    ? queue.data.decisions.length + queue.data.facts.length + queue.data.domains.length
    : 0;
  // The hash belongs to the router, so the skip link moves focus instead of navigating.
  const skip = (event) => {
    event.preventDefault();
    document.getElementById("main").focus();
  };
  return html`<a class="skip-link" href="#/" onClick=${skip}>Skip to content</a>
    <div class="app">
      <${Header} session=${session.data} pages=${PAGES.filter((page) => !page.hidden)} route=${route.page === "edit" ? { ...route, page: "records" } : route} queueCount=${count} />
      <${ShellBanners} session=${session.data} liveState=${liveState} busy=${busy} />
      ${failed && html`<${ErrorNotice} error=${failed.error} onRetry=${retryFailed} />`}
      <main id="main" class="main" tabindex="-1"><${PageView} route=${route} session=${session.data} /></main>
      <${Footer} storeChanges=${session.data.store_changes} />
    </div>`;
}

render(
  captureToken() ? html`<${Portal} />` : html`<${NoTokenPage} />`,
  document.getElementById("app"),
);
