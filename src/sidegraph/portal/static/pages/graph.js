// Graph page: the decision graph drawn with vis-network, client-side filters, a side panel for
// the selected node. The graph JSON comes from /api/graph; vis-network is served by the portal
// at VIS_NETWORK_PATH and loaded here on first use. Search does not filter the graph: the
// header dropdown opens the record card instead.
// see design/superpowers/specs/2026-11-17-portal-design.md (D4, D5)
import { describeError } from "../api.js";
import { Banner, ErrorNotice } from "../components/banners.js";
import { setSearchHistory } from "../components/search-box.js";
import { STATUS_WORDS, StatusWord } from "../components/status-word.js";
import { useResource } from "../components/use-resource.js";
import { html, useEffect, useMemo, useRef, useState } from "../ui.js";

const VIS_PATH = "/static/vendor/vis-network.min.js";
const WATCH = ["records", "graph", "sync"];
const KINDS = [
  ["gotcha", "Gotcha"],
  ["lesson", "Lesson"],
  ["constraint", "Constraint"],
  ["adr", "ADR"],
  ["fact", "Fact"],
];
// A binding's internal status as the person sees it (D5); a cold index checked none of them.
const WORD_OF_STATUS = { live: "found", degraded: "ambiguous", orphaned: "missing" };
const HEALTH_WORDS = ["found", "ambiguous", "missing"];
const HEALTH_LABELS = { found: "Found", ambiguous: "Ambiguous", missing: "Missing", unchecked: "Unchecked" };
// Decision boxes are filled with a mid-tone colour in both themes; black text reads on all four.
const BOX_TEXT = "#111214";
// The layout is computed once, off screen, within this many steps; then physics stops.
const LAYOUT_ITERATIONS = 300;
// A live data change pins the placed nodes, so only the new ones need a short pass.
const LIVE_ITERATIONS = 60;

let visLoading = null;

/** Load vis-network once; a failed load can be tried again. */
function loadVis() {
  if (window.vis?.Network) return Promise.resolve(window.vis);
  visLoading ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = VIS_PATH;
    script.onload = () => resolve(window.vis);
    script.onerror = () => {
      visLoading = null;
      script.remove();
      reject(new Error("vis-network did not load"));
    };
    document.head.append(script);
  });
  return visLoading;
}

function useVis() {
  const [state, setState] = useState({ vis: window.vis?.Network ? window.vis : null, failed: false });
  useEffect(() => {
    if (state.vis) return;
    loadVis().then(
      (vis) => setState({ vis, failed: false }),
      () => setState({ vis: null, failed: true }),
    );
  }, []);
  return state;
}

function recordKind(node) {
  return node.type === "fact" ? "fact" : node.detail.kind;
}

function edgeWord(edge, cold) {
  return cold ? "unchecked" : (WORD_OF_STATUS[edge.status] ?? "found");
}

/** What the filters leave of the graph: the visible record nodes, code entities and edges. */
function applyFilters(graph, filters, cold) {
  const domains = graph.record_domains;
  // With some anchor health unticked, only records holding an anchor in a ticked state stay.
  const health = (cold ? ["unchecked"] : HEALTH_WORDS).filter((word) => filters.words[word]);
  const restricted = health.length < (cold ? 1 : HEALTH_WORDS.length);
  const healthy = new Set(
    graph.edges
      .filter((edge) => edge.kind === "anchor" && health.includes(edgeWord(edge, cold)))
      .map((edge) => edge.from),
  );
  const records = new Map();
  for (const node of graph.nodes) {
    if (node.type === "entity") continue;
    const status = node.detail.status;
    const shown =
      filters.kinds[recordKind(node)] !== false &&
      filters.statuses[status] !== false &&
      (!filters.domain || (domains[node.id] ?? []).includes(filters.domain)) &&
      (!restricted || healthy.has(node.id));
    if (shown) records.set(node.id, node);
  }
  const edges = [];
  const anchored = new Set();
  for (const edge of graph.edges) {
    if (edge.kind === "anchor") {
      if (!filters.code || !records.has(edge.from) || !filters.words[edgeWord(edge, cold)]) continue;
      anchored.add(edge.to);
      edges.push(edge);
    } else if (edge.kind !== "domain-parent" && records.has(edge.from) && records.has(edge.to)) {
      edges.push(edge);
    }
  }
  const entities = graph.nodes.filter((node) => node.type === "entity" && anchored.has(node.id));
  const shownIds = new Set([...records.keys(), ...anchored]);
  for (const edge of graph.edges) {
    if (edge.kind === "domain-parent" && shownIds.has(edge.from) && shownIds.has(edge.to)) edges.push(edge);
  }
  return { nodes: [...records.values(), ...entities], edges };
}

function tooltip(node) {
  const detail = node.detail;
  const name = detail.title ?? detail.statement ?? detail.canonical_name ?? node.label;
  return `${String(name).slice(0, 240)}\n${node.title}`;
}

function toVisNode(node) {
  const proposed = node.detail.status === "proposed";
  const visNode = {
    id: node.id,
    label: node.label,
    shape: node.shape,
    size: node.size,
    color: node.color,
    opacity: node.opacity,
    title: tooltip(node),
    borderWidth: proposed || node.problem ? 3 : 1,
    shapeProperties: { borderDashes: proposed ? [6, 4] : false },
  };
  if (node.shape === "box") visNode.font = { color: BOX_TEXT };
  return visNode;
}

function toVisEdge(edge) {
  return {
    id: `${edge.from}>${edge.to}:${edge.kind}`,
    from: edge.from,
    to: edge.to,
    color: edge.color,
    width: edge.width,
    label: edge.label,
    dashes: edge.dashes,
    arrows: edge.arrows,
  };
}

function sync(set, items) {
  const keep = new Set(items.map((item) => item.id));
  const gone = set.getIds().filter((id) => !keep.has(id));
  if (gone.length) set.remove(gone);
  set.update(items);
}

function inkColor() {
  return getComputedStyle(document.documentElement).getPropertyValue("--ink").trim() || "#ecece7";
}

/** Owns one vis Network; the page hands it the filtered nodes and edges. */
function GraphCanvas({ vis, nodes, edges, selectedId, onSelect, fit, label, layoutKey }) {
  const host = useRef(null);
  const handle = useRef(null);
  const laidOut = useRef("");
  const laidKey = useRef("");
  const frameNext = useRef(true);
  const [laying, setLaying] = useState(true);
  const choose = useRef(onSelect);
  choose.current = onSelect;

  useEffect(() => {
    const nodeSet = new vis.DataSet();
    const edgeSet = new vis.DataSet();
    const network = new vis.Network(
      host.current,
      { nodes: nodeSet, edges: edgeSet },
      {
        physics: {
          solver: "forceAtlas2Based",
          stabilization: { iterations: LAYOUT_ITERATIONS, updateInterval: 25, fit: false },
          minVelocity: 2,
          forceAtlas2Based: {
            theta: 0.6,
            gravitationalConstant: -300,
            centralGravity: 0.003,
            springLength: 300,
            springConstant: 0.08,
            damping: 0.4,
            avoidOverlap: 1,
          },
        },
        interaction: { hover: true, tooltipDelay: 120 },
        nodes: { font: { color: inkColor(), size: 13 }, widthConstraint: { maximum: 180 } },
        edges: { smooth: { type: "continuous" } },
      },
    );
    network.on("click", (event) => choose.current(event.nodes[0] ?? null));
    // The library makes its frame a tab stop; the canvas has no keyboard use ("Inspect" has).
    host.current.querySelector(".vis-network")?.setAttribute("tabindex", "-1");
    // The layout runs once behind the "Laying out" notice, and then physics stops: the graph
    // stays still, and dragging a node moves only that node. The view is framed after the
    // first layout and after a filter change, never after a live data change.
    network.on("stabilizationIterationsDone", () => {
      network.setOptions({ physics: false });
      setLaying(false);
      if (frameNext.current) network.fit();
      frameNext.current = false;
      const pinned = nodeSet.get({ filter: (node) => node.fixed });
      if (pinned.length) nodeSet.update(pinned.map((node) => ({ id: node.id, fixed: false })));
    });
    handle.current = { network, nodeSet, edgeSet };
    // The theme toggle changes the class on <html>; the label colour follows it.
    const watcher = new MutationObserver(() =>
      network.setOptions({ nodes: { font: { color: inkColor() } } }),
    );
    watcher.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      watcher.disconnect();
      network.destroy();
      handle.current = null;
    };
  }, [vis]);

  useEffect(() => {
    const { network, nodeSet, edgeSet } = handle.current;
    // A new set of nodes or edges (a filter change) is laid out again; the same data is not.
    const shape = [
      ...nodes.map((node) => node.id).sort(),
      "|",
      ...edges.map((edge) => toVisEdge(edge).id).sort(),
    ];
    const relayout = shape.join() !== laidOut.current;
    laidOut.current = shape.join();
    // The filters are remembered even when they leave the same nodes, so a later live
    // change is not taken for the filter's own.
    const refiltered = layoutKey !== laidKey.current;
    laidKey.current = layoutKey;
    // A live change keeps the nodes already placed where they are; only new ones find a spot.
    const placed = relayout && !refiltered ? nodeSet.getIds() : [];
    if (relayout) {
      if (refiltered) frameNext.current = true;
      setLaying(true);
      network.setOptions({ physics: true });
    }
    sync(nodeSet, nodes.map(toVisNode));
    sync(edgeSet, edges.map(toVisEdge));
    nodeSet.update(placed.filter((id) => nodeSet.get(id)).map((id) => ({ id, fixed: true })));
    // The library lays out by itself only for the data a network is built with.
    if (relayout) network.stabilize(placed.length ? LIVE_ITERATIONS : LAYOUT_ITERATIONS);
  }, [nodes, edges]);

  useEffect(() => {
    const { network, nodeSet } = handle.current;
    network.selectNodes(selectedId && nodeSet.get(selectedId) ? [selectedId] : []);
  }, [selectedId, nodes]);

  useEffect(() => {
    if (fit) handle.current.network.fit();
  }, [fit]);

  return html`<div class="graph-stage">
    <div class="graph-canvas" ref=${host} role="img" aria-label=${label}></div>
    ${laying && html`<p class="graph-laying" role="status">Laying out the graph…</p>`}
  </div>`;
}

/** `tip` is shown while the box is focused or hovered, and read out as its description. */
function Check({ checked, onChange, children, tip, tipId }) {
  return html`<label class="check">
    <input
      type="checkbox"
      checked=${checked}
      aria-describedby=${tip ? tipId : undefined}
      onChange=${(event) => onChange(event.target.checked)}
    />
    <span>${children}</span>
    ${tip && html`<span class="check-tip small muted" id=${tipId}>${tip}</span>`}
  </label>`;
}

function Filters({ filters, setFilters, counts, domains, certified, cold, history, setHistory }) {
  const set = (group, key) => (value) =>
    setFilters((previous) => ({ ...previous, [group]: { ...previous[group], [key]: value } }));
  const words = cold ? ["unchecked"] : HEALTH_WORDS;
  // On a phone the filters start folded, so the graph is the first thing on screen.
  return html`<details class="graph-filters" open=${!matchMedia("(max-width: 800px)").matches}>
    <summary>Filters</summary>
    <fieldset>
      <legend>Kind</legend>
      ${KINDS.map(
        ([key, label]) => html`<${Check} key=${key} checked=${filters.kinds[key]} onChange=${set("kinds", key)}>
          ${label} <span class="muted small">${counts.kinds[key] ?? 0}</span>
        <//>`,
      )}
    </fieldset>
    <fieldset>
      <legend>Status</legend>
      <${Check} checked=${filters.statuses.accepted} onChange=${set("statuses", "accepted")}>Accepted<//>
      <${Check} checked=${filters.statuses.proposed} onChange=${set("statuses", "proposed")}>
        Proposed <span class="muted small">${counts.proposed}</span>
      <//>
      <${Check} checked=${history} onChange=${setHistory}>Superseded / history<//>
    </fieldset>
    <div class="field">
      <label for="graph-domain">Domain</label>
      <select
        id="graph-domain"
        value=${filters.domain}
        disabled=${!domains.length}
        onChange=${(event) => setFilters((previous) => ({ ...previous, domain: event.target.value }))}
      >
        <option value="">${domains.length ? "All domains" : "No domains"}</option>
        ${domains.map((slug) => html`<option key=${slug} value=${slug}>${slug}</option>`)}
      </select>
      ${!certified &&
      html`<p class="small muted">
        Domain membership is not certified: it is what the last sync stored, so a domain may miss
        some records.
      </p>`}
    </div>
    <fieldset>
      <legend>Anchor health</legend>
      ${words.map(
        (word) => html`<${Check} key=${word} checked=${filters.words[word]} onChange=${set("words", word)} tip=${STATUS_WORDS[word].tip} tipId=${`health-tip-${word}`}>
          ${HEALTH_LABELS[word]}
        <//>`,
      )}
    </fieldset>
    <${Check} checked=${filters.code} onChange=${(value) => setFilters((previous) => ({ ...previous, code: value }))}>
      Show code entities
    <//>
  </details>`;
}

function RecordLink({ id }) {
  return html`<a class="mono" href=${`#/records/${encodeURIComponent(id)}`}>${id.slice(0, 8)}</a>`;
}

function AnchorList({ id }) {
  const card = useResource(`/api/records/${id}`, { watch: WATCH });
  if (card.error && !card.data) return html`<p class="small muted" role="alert">${describeError(card.error)}</p>`;
  if (!card.data) return html`<p class="small muted">Loading anchors…</p>`;
  const anchors = card.data.anchors;
  return html`<div class="panel-anchors">
    <span class="panel-label">Bound to code · ${anchors.length}</span>
    ${anchors.map(
      (anchor) => html`<div key=${anchor.entity_id} class="panel-anchor">
        <div class="panel-anchor-head">
          <span class="mono">${anchor.name}</span>
          <${StatusWord} word=${anchor.status_word} />
        </div>
        <span class="small muted mono">
          ${anchor.file ? `${anchor.file}${anchor.start ? `:${anchor.start}` : ""}` : (anchor.community ?? `tier ${anchor.tier}`)}
        </span>
      </div>`,
    )}
    ${!anchors.length && html`<p class="small muted">No anchors: this record is not bound to code.</p>`}
  </div>`;
}

function RecordPanel({ node, graph }) {
  const detail = node.detail;
  const incoming = (kind) => graph.edges.filter((edge) => edge.kind === kind && edge.to === node.id);
  const supersedes = graph.edges.filter((edge) => edge.kind === "supersedes" && edge.from === node.id);
  const supersededBy = incoming("supersedes");
  const support = incoming("supports");
  const body = detail.choice ?? detail.statement ?? detail.context ?? "";
  return html`<div class="panel-record">
    <div class="panel-chips">
      <span class=${`kind-chip kind-${recordKind(node)}`}>${recordKind(node).toUpperCase()}</span>
      <span class="small muted">${detail.status}</span>
      <${RecordLink} id=${node.id} />
    </div>
    <h2>${detail.title ?? detail.statement}</h2>
    ${body && body !== detail.title && html`<p class="panel-body">${body}</p>`}
    <${AnchorList} id=${node.id} key=${node.id} />
    ${(supersedes.length > 0 || supersededBy.length > 0 || support.length > 0) &&
    html`<p class="small muted panel-links">
      ${supersedes.map((edge) => html`Supersedes <${RecordLink} id=${edge.to} /> `)}
      ${supersededBy.map((edge) => html`Superseded by <${RecordLink} id=${edge.from} /> `)}
      ${support.length > 0 && `${support.length} ${support.length === 1 ? "fact supports" : "facts support"} it`}
    </p>`}
    <div class="panel-actions">
      <a class="btn btn-primary" href=${`#/records/${encodeURIComponent(node.id)}`}>Open record</a>
      ${detail.status === "accepted" &&
      html`<a class="btn" href=${`#/edit/${encodeURIComponent(node.id)}`}>Edit as new version</a>`}
    </div>
  </div>`;
}

function EntityPanel({ node, graph }) {
  const detail = node.detail;
  const records = graph.edges
    .filter((edge) => edge.kind === "anchor" && edge.to === node.id)
    .map((edge) => graph.nodes.find((other) => other.id === edge.from))
    .filter(Boolean);
  return html`<div class="panel-record">
    <div class="panel-chips">
      <span class="kind-chip kind-entity">CODE</span>
      <span class="small muted">${detail.kind}</span>
    </div>
    <h2 class="mono">${node.label}</h2>
    ${detail.file_path && html`<p class="small muted mono">${detail.file_path}</p>`}
    ${detail.last_seen_community && html`<p class="small muted">Community: ${detail.last_seen_community}</p>`}
    <span class="panel-label">Anchors ${records.length} ${records.length === 1 ? "record" : "records"}</span>
    <ul class="panel-list">
      ${records.map(
        (record) => html`<li key=${record.id}>
          <a href=${`#/records/${encodeURIComponent(record.id)}`}>${record.detail.title ?? record.detail.statement}</a>
        </li>`,
      )}
    </ul>
  </div>`;
}

function Legend() {
  return html`<p class="graph-legend small muted">
    <span>▭ decision</span><span>◆ fact</span><span>● code entity (from the graph engine)</span>
    <span>dashed border = proposed</span><span>faded = superseded</span><span>red border = problem</span>
  </p>`;
}

const DEFAULT_FILTERS = {
  kinds: Object.fromEntries(KINDS.map(([key]) => [key, true])),
  statuses: { accepted: true, proposed: true },
  domain: "",
  words: { found: true, ambiguous: true, missing: true, unchecked: true },
  code: true,
};

export default function Page({ session }) {
  const [history, setHistory] = useState(false);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [selectedId, setSelectedId] = useState(null);
  const [fit, setFit] = useState(0);
  const panel = useRef(null);
  const graph = useResource("/api/graph", { params: { history }, watch: WATCH });
  // The header search on this page follows the same "Superseded / history" choice.
  useEffect(() => setSearchHistory(history), [history]);
  useEffect(() => () => setSearchHistory(false), []);
  const { vis, failed } = useVis();
  const data = graph.data;
  const cold = session.index_cold;

  const domains = useMemo(
    () => (data ? [...new Set(Object.values(data.record_domains).flat())].sort() : []),
    [data],
  );
  const counts = useMemo(() => {
    const found = { kinds: {}, proposed: 0 };
    for (const node of data?.nodes ?? []) {
      if (node.type === "entity") continue;
      found.kinds[recordKind(node)] = (found.kinds[recordKind(node)] ?? 0) + 1;
      if (node.detail.status === "proposed") found.proposed += 1;
    }
    return found;
  }, [data]);
  // A domain that vanished in a refetch no longer filters anything.
  const active = { ...filters, domain: domains.includes(filters.domain) ? filters.domain : "" };
  const shown = useMemo(() => (data ? applyFilters(data, active, cold) : null), [data, active.domain, filters, cold]);
  const selected = shown?.nodes.find((node) => node.id === selectedId) ?? null;

  // On a phone the panel sits below the graph: bring a fresh selection into view.
  useEffect(() => {
    if (selected && matchMedia("(max-width: 800px)").matches) {
      panel.current?.scrollIntoView({ block: "nearest" });
    }
  }, [selected?.id]);

  if (!data) {
    return html`<section class="page graph-page">
      <h1 class="graph-sr">Graph</h1>
      ${graph.error ? html`<${ErrorNotice} error=${graph.error} onRetry=${graph.reload} />` : html`<p class="muted">Loading…</p>`}
    </section>`;
  }

  const records = shown.nodes.filter((node) => node.type !== "entity");
  const entities = shown.nodes.length - records.length;
  const proposed = records.filter((node) => node.detail.status === "proposed").length;
  const scope = [
    active.domain && `Domain: ${active.domain}`,
    `${records.length} ${records.length === 1 ? "record" : "records"}`,
    `${entities} code ${entities === 1 ? "entity" : "entities"}`,
    `${proposed} proposed`,
  ].filter(Boolean);
  const empty = data.nodes.length === 0;

  return html`<section class="page graph-page">
    <h1 class="graph-sr">Graph</h1>
    ${graph.error && html`<${ErrorNotice} error=${graph.error} onRetry=${graph.reload} />`}
    ${data.stats.truncated > 0 &&
    html`<${Banner}>
      ${`The graph draws the first ${data.nodes.length} nodes, records with problems first; ${data.stats.truncated} more are not drawn. The Records page lists every record.`}
    <//>`}
    <div class="graph-layout">
      <${Filters}
        filters=${active}
        setFilters=${setFilters}
        counts=${counts}
        domains=${domains}
        certified=${data.membership_certified}
        cold=${cold}
        history=${history}
        setHistory=${setHistory}
      />
      <div class="graph-main">
        <div class="graph-toolbar">
          <span class="small">${scope.join(" · ")}</span>
          <span class="header-spacer"></span>
          <label class="graph-inspect small">
            Inspect
            <select
              value=${selected?.id ?? ""}
              disabled=${!shown.nodes.length}
              onChange=${(event) => setSelectedId(event.target.value || null)}
            >
              <option value="">Choose a node</option>
              ${shown.nodes.map(
                (node) => html`<option key=${node.id} value=${node.id}>
                  ${node.type === "entity" ? "code" : recordKind(node)} · ${node.label}
                </option>`,
              )}
            </select>
          </label>
          <button type="button" class="btn" onClick=${() => setFit((count) => count + 1)} disabled=${!shown.nodes.length}>Fit</button>
        </div>
        ${empty
          ? html`<div class="graph-empty">
              <h2>No records yet</h2>
              <p class="muted">
                The store holds no decisions or facts for this branch. Records the agent proposes or
                you add appear here as soon as they exist.
              </p>
            </div>`
          : !shown.nodes.length
            ? html`<div class="graph-empty"><h2>Nothing matches these filters</h2><p class="muted">Loosen a filter on the left to see records.</p></div>`
            : failed
              ? html`<div class="graph-empty" role="alert"><h2>The graph could not be drawn</h2><p class="muted">The drawing library did not load. Reload the page to try again; the Records page works without it.</p></div>`
              : !vis
                ? html`<p class="muted graph-empty">Loading the graph…</p>`
                : html`<${GraphCanvas}
                    vis=${vis}
                    nodes=${shown.nodes}
                    edges=${shown.edges}
                    selectedId=${selected?.id ?? null}
                    onSelect=${setSelectedId}
                    fit=${fit}
                    layoutKey=${JSON.stringify([active, history])}
                    label=${`Graph of ${records.length} records and ${entities} code entities. Use “Inspect” above to choose a node from the keyboard.`}
                  />`}
        <${Legend} />
      </div>
      <aside class="graph-panel" aria-label="Selected node" aria-live="polite" ref=${panel}>
        ${!selected
          ? html`<p class="muted">Click a node to see it here.</p>`
          : selected.type === "entity"
            ? html`<${EntityPanel} node=${selected} graph=${data} />`
            : html`<${RecordPanel} node=${selected} graph=${data} />`}
      </aside>
    </div>
  </section>`;
}
