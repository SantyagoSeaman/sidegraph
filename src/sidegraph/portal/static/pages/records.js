// Records page: the table with search and filter chips, and the record card behind each row.
// Text that is the start of exactly one record id opens that card, as in the header search.
// The card lives at #/records/<id>; the table keeps its filters in the hash query, so the
// browser's Back from a card returns to the same view.
// see design/superpowers/specs/2026-11-17-portal-design.md (D4, D5)
import { get } from "../api.js";
import { ErrorNotice } from "../components/banners.js";
import { KindBadge } from "../components/kind-badge.js";
import { RecordCard } from "../components/record-card.js";
import { open as openRecord } from "../components/search-box.js";
import { StatusWord } from "../components/status-word.js";
import { useResource } from "../components/use-resource.js";
import { html, useEffect, useRef, useState } from "../ui.js";

const OPEN_STATUSES = ["proposed", "accepted"];
const HISTORY_STATUSES = ["superseded", "rejected", "deprecated"];
const KINDS = ["adr", "lesson", "constraint", "gotcha"];
const LAYERS = ["business", "technical"];
const ANCHOR_FILTERS = ["found", "ambiguous", "missing", "unchecked", "none"];
const ANCHOR_WORDS = ["found", "ambiguous", "missing", "unchecked"];
const FILTERS = ["status", "kind", "domain", "layer", "anchors"];
const CHUNK = 200;
const WATCH = ["records", "graph", "sync"];

// The filters live in the address, not in the router's route: a filter change rewrites the
// address without a hashchange, so the route's query would be stale.
function readFilters() {
  const query = new URLSearchParams(location.hash.split("?")[1] ?? "");
  const filters = { q: query.get("q") ?? "", history: query.get("history") === "true" };
  for (const name of FILTERS) filters[name] = query.get(name) ?? "";
  // A status the table does not offer (a terminal one without history, or an old bookmark) is dropped.
  if (!(filters.history ? [...OPEN_STATUSES, ...HISTORY_STATUSES] : OPEN_STATUSES).includes(filters.status)) {
    filters.status = "";
  }
  return filters;
}

function writeFilters(filters) {
  const query = new URLSearchParams();
  if (filters.q) query.set("q", filters.q);
  if (filters.history) query.set("history", "true");
  for (const name of FILTERS) if (filters[name]) query.set(name, filters[name]);
  const text = query.toString();
  // replaceState: a filter change is not a page of its own, and it fires no hashchange.
  history.replaceState(null, "", `#/records${text ? `?${text}` : ""}`);
}

function AnchorWords({ statuses }) {
  if (!statuses.length) return html`<span class="muted">—</span>`;
  return ANCHOR_WORDS.filter((word) => statuses.includes(word)).map((word) => {
    const count = statuses.filter((status) => status === word).length;
    return html`<span key=${word} class="anchor-count">
      ${count > 1 && html`<span class="muted">${count}×</span> `}<${StatusWord} word=${word} />
    </span>`;
  });
}

function FilterSelect({ label, value, options, onChange, anyLabel = "any" }) {
  return html`<label class="filter">
    <span class="muted">${label}</span>
    <select value=${value} onChange=${(event) => onChange(event.target.value)}>
      <option value="">${anyLabel}</option>
      ${options.map((option) => html`<option key=${option} value=${option}>${option}</option>`)}
    </select>
  </label>`;
}

function RecordsTable({ rows }) {
  const [shown, setShown] = useState(CHUNK);
  return html`<div class="table-box">
      <table class="records-table">
        <thead>
          <tr>
            <th scope="col">Kind</th><th scope="col">Title</th><th scope="col">Status</th>
            <th scope="col">Domain</th><th scope="col">Layer</th><th scope="col">Code anchors</th>
          </tr>
        </thead>
        <tbody>
          ${rows.slice(0, shown).map(
            (row) => html`<tr key=${row.id}>
              <td><${KindBadge} row=${row} /></td>
              <td class="title-cell"><a class="record-link" href=${`#/records/${encodeURIComponent(row.id)}`}>${row.title}</a></td>
              <td class=${`status-${row.status}`}>${row.status}</td>
              <td>${row.domains.length ? row.domains.join(", ") : "—"}</td>
              <td>${row.layer ?? "—"}</td>
              <td><${AnchorWords} statuses=${row.anchor_statuses} /></td>
            </tr>`,
          )}
        </tbody>
      </table>
    </div>
    ${rows.length > shown &&
    html`<p><button type="button" class="btn" onClick=${() => setShown(shown + CHUNK)}>
      Show ${Math.min(CHUNK, rows.length - shown)} more of ${rows.length - shown} left
    </button></p>`}`;
}

function RecordsList() {
  const [filters, setFilters] = useState(readFilters);
  const [text, setText] = useState(filters.q);
  const [domains, setDomains] = useState([]);
  const records = useResource("/api/records", { params: filters, watch: WATCH });

  // Following a link to the table (the nav's own Records link) starts from its address.
  const address = location.hash;
  useEffect(() => {
    const next = readFilters();
    setFilters(next);
    setText(next.q);
  }, [address]);
  // The text typed so far: an answer about an older text must not move the page.
  const typed = useRef(text);
  typed.current = text;
  // A unique id prefix opens the card, as in the header search; the server decides what counts as
  // one (D4). Other text has just filtered the table, so a failed lookup has nothing to report.
  const lookup = useRef(null);
  const lookupTimer = useRef(null);
  useEffect(
    () => () => {
      clearTimeout(lookupTimer.current);
      lookup.current?.abort();
    },
    [],
  );
  const findRecord = (q) => {
    lookup.current?.abort();
    // A record id prefix has at least 8 characters; shorter text only filters the table.
    if (q.trim().length < 8) return;
    const abort = new AbortController();
    lookup.current = abort;
    get("/api/search", { q: q.trim(), limit: 1 }, { signal: abort.signal }).then(
      (found) => {
        // Only while this is still the table (a card, another page or a tab change is not) and the text is the one asked about.
        if (!found.open_id || typed.current !== q || location.hash.split("?")[0] !== "#/records") return;
        // The table's own write may still be waiting: Back from the card returns to this text.
        writeFilters({ ...readFilters(), q });
        openRecord(found.open_id);
      },
      () => {},
    );
  };
  // Only typing looks for a record: a filter change or a return to the page must not.
  const type = (value) => {
    setText(value);
    clearTimeout(lookupTimer.current);
    lookupTimer.current = setTimeout(() => findRecord(value), 250);
  };
  // The search box applies after a pause; every other control applies at once.
  useEffect(() => {
    if (text === filters.q) return undefined;
    const timer = setTimeout(() => change({ q: text }), 250);
    return () => clearTimeout(timer);
  }, [text, filters]);
  // Domain options accumulate from what the page has seen, so filtering by one domain
  // does not remove the others from the list.
  useEffect(() => {
    if (!records.data) return;
    const seen = new Set([...domains, ...records.data.rows.flatMap((row) => row.domains)]);
    if (filters.domain) seen.add(filters.domain);
    if (seen.size !== domains.length) setDomains([...seen].sort());
  }, [records.data]);

  const change = (patch) => {
    // Text typed inside the pause is applied with the change, not erased by it.
    const next = { ...filters, q: text, ...patch };
    // A terminal status only exists while history is searched.
    if (!next.history && HISTORY_STATUSES.includes(next.status)) next.status = "";
    setFilters(next);
    writeFilters(next);
  };
  const filtered = FILTERS.some((name) => filters[name]) || filters.q || filters.history;
  // Rows belong to the query that fetched them: while a changed query is loading they stay,
  // but a failed one must not leave the previous query's rows under its own filters.
  const query = JSON.stringify(filters);
  const fetched = useRef({ data: null, query });
  if (records.data !== fetched.current.data) fetched.current = { data: records.data, query };
  const rows = records.error && fetched.current.query !== query ? undefined : records.data?.rows;
  const statuses = filters.history ? [...OPEN_STATUSES, ...HISTORY_STATUSES] : OPEN_STATUSES;
  return html`<section class="page records-page">
    <h1>Records</h1>
    <div class="search-row">
      <label class="search-box">
        <span class="visually-hidden">Search records</span>
        <input type="search" value=${text} placeholder="Search records" onInput=${(event) => type(event.target.value)} />
      </label>
      <span class="muted">Searches titles, text, and the names and paths of anchored code</span>
    </div>
    <div class="filters">
      <${FilterSelect} label="Status" value=${filters.status} options=${statuses} onChange=${(status) => change({ status })} />
      <${FilterSelect} label="Kind" value=${filters.kind} options=${KINDS} onChange=${(kind) => change({ kind })} />
      <${FilterSelect} label="Domain" value=${filters.domain} options=${domains} onChange=${(domain) => change({ domain })} />
      <${FilterSelect} label="Layer" value=${filters.layer} options=${LAYERS} onChange=${(layer) => change({ layer })} />
      <${FilterSelect} label="Anchors" value=${filters.anchors} options=${ANCHOR_FILTERS} onChange=${(anchors) => change({ anchors })} />
      <label class="filter">
        <input type="checkbox" checked=${filters.history} onChange=${(event) => change({ history: event.target.checked })} />
        Search history too
      </label>
      ${rows && html`<span class="muted result-count">${rows.length} ${rows.length === 1 ? "record" : "records"}</span>`}
    </div>
    ${records.error && html`<${ErrorNotice} error=${records.error} onRetry=${records.reload} />`}
    ${!rows && !records.error && html`<p class="muted">Loading…</p>`}
    ${rows && !rows.length && html`<p class="empty">${filtered ? "No record matches this search and these filters." : "The store holds no records yet."}</p>`}
    ${rows && rows.length > 0 && html`<${RecordsTable} key=${query} rows=${rows} />`}
  </section>`;
}

export default function Page({ route, session }) {
  const [id] = route.rest;
  // Keyed by id: moving to another card must not show the previous record while it loads.
  if (id) return html`<${RecordCard} key=${id} id=${id} session=${session} />`;
  return html`<${RecordsList} />`;
}
