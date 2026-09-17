## ⚠ Known mistakes & gotchas
- [gotcha] Treat graph.json reads as non-atomic — version by built_at_commit, never mtime: GraphifyReader always reads its version from the embedded built_at_commit field, never file mtime, and treats every read as potentially non-atomic: read, then parse, then on a JSONDecodeError or an unexpected built_at_commit change mid-read, re-read with a small bounded retry. (context: Graphify's writer can leave graph.json in a stale or transiently inconsistent state — for example the shrink-guard can refuse an overwrite and leave the file's mtime stale even though nothing changed, or a read can race a concurrent write — so any reader that trusts a single read+parse, or trusts mtime as the freshness signal, risks silently working off a stale or partially-written graph.) (rejected: Trusting file mtime as the graph's freshness signal — rejected because the shrink-guard can refuse an overwrite and leave mtime stale, which would make mtime lie about whether the graph was actually rebuilt.) (consequences: Every GraphifyReader read carries a small bounded-retry cost, but callers (anchoring, sync) can safely assume graph_version() reflects the true last-built commit rather than an artifact of write timing.) (id: 01KX8NNGK9NY3BXCRJFT16NAGZ)
- [constraint] Never write into the engine's graph.json (context: Graphify (the code-graph engine Sidegraph sits on top of) regenerates its graph.json from cache on every commit, so anything written there is erased on the next rebuild. Sidegraph needs decision memory that survives rebuilds.) (consequences: Forces all decision persistence into Sidegraph's own store rather than piggybacking on the engine's output artifact; the engine seam (GraphifyReader) only ever reads graph.json, never writes it.) (id: 01KX8NNGFZB8NCQ9N6X8ME7RFS)
- [gotcha] The engine graph goes stale silently — rebuild before any anchoring or domain work: Run `graphify update .` before naming domains, re-anchoring, or trusting a resolve. It is cheap -- a full rebuild of this repo took 5 seconds and produced 4482 nodes. (context: Nothing compares the graph's `built_at_commit` to the repository's current HEAD. A graph built many commits ago keeps answering queries confidently: it still contains files that have since been deleted, and knows nothing about files added since.) (consequences: Observed 2026-07-25 with a graph 40 commits behind HEAD: `experiments/` and `tests/phase0/` still appeared as naming candidates although both had been evicted to the sandbox, so five candidate communities described deleted code; and an entire wave of new modules under tools/ was invisible, so anchors naming them silently landed orphaned. Both failure modes are silent -- the tools report success…) (id: 01KYD1WCPGVQDSVS3YBJDAB9CP)
- [gotcha] Memoize the adjacency index in GraphifyReader to fix O(rationale x edges) neighbor scans: Build a memoized adjacency index (node_id -> neighbors) once inside GraphifyReader so neighbors() becomes O(degree) instead of O(edges); audit every other O(E)-per-call reader method and index those too. The public interface is unchanged — this is a pure internal-performance change behind the engine seam. (context: GraphifyReader.neighbors() linearly scanned all edges on every call, and rationale_nodes() called it once per rationale node, giving O(rationale x edges) total cost. On a large Python monorepo this reached billions of comparisons in pure Python; sidegraph-import was killed after 90 seconds of CPU time with zero output, and --limit was applied only after the full scan had already run.) (id: 01KX8NNHXJJ6382PZKP33K61AT)
- [gotcha] Use the `graphify update <path>` subcommand — never `graphify <path> --update`: The harness always invokes Graphify via the dedicated `update` subcommand (`graphify update <path>`), the incremental, LLM-free form; the two invocation styles are treated as not interchangeable. (context: The Phase-0 harness needed a reliable, LLM-free way to refresh graph.json incrementally after code-only changes. The default `graphify <path>` command attempts full semantic extraction (including any doc files present) and fails without an LLM key; the flag form `graphify <path> --update` was also tried, during a shrink-guard sanity check, and likewise fails without an LLM key whenever doc files are present in the target repo.) (rejected: `graphify <path> --update` (the default command plus the flag) — rejected because on failure it silently no-ops: it leaves the prior graph.json and built_at_commit untouched rather than raising loudly, which could be mistaken for a shrink-guard refusal rather than a plain invocation failure.) (consequences: A trivial rebuild via the `update` subcommand does rewrite graph.json (built_at_commit changes) with no shrink-guard refusal, confirming the incremental writer works correctly — but only when invoked via the correct subcommand form.) (id: 01KX8NNGJR914CRJKGK1E1WFRM)
- [constraint] Keep the three seams clean — portable core, engine seam, host seam: Keep three seams clean: a portable core (schema, store, retrieval, server) that knows nothing about Graphify or Claude Code and speaks only NodeRef/AbstractRef/descriptor; an engine seam (engine/reader.py's GraphifyReader) as the only… (id: 01KX8NNGGYQM7BGGFBDQXDDVPF)
- [gotcha] An anchor name must match the form the graph emits as a label — module constants are not nodes at all: Write the anchor name in the graph's own label form, verified against `graphify-out/graph.json` before capturing rather than guessed. Measured on this repo (graphify 0.9.6): a method is `.name()` with a LEADING DOT and parentheses… (id: 01KYEWQBK4P6M6MS26B6NFCG4P)
- [gotcha] Guard the sync loose rung against cross-file-type mis-adoption: The loose rung only follows a unique hit whose file suffix matches the old descriptor's suffix: `.py`→`.py` and `.md`→`.md` moves still adopt, but a cross-suffix hit falls through to orphan status instead, where community fallback and the… (id: 01KX8NNH9QYGTT4MT1HXXYD3NF)

## Structural map
- reader.py (code) [src/sidegraph/engine/reader.py:L1]
- Community (code) [src/sidegraph/engine/reader.py:L53]
- GraphifyReader (code) [src/sidegraph/engine/reader.py:L92]
- ._adjacency_index() (code) [src/sidegraph/engine/reader.py:L197]
- .changed_files() (code) [src/sidegraph/engine/reader.py:L339]
- .communities() (code) [src/sidegraph/engine/reader.py:L266]
- .community_labels() (code) [src/sidegraph/engine/reader.py:L281]
- .containing() (code) [src/sidegraph/engine/reader.py:L262]
- ._file_index() (code) [src/sidegraph/engine/reader.py:L150]
- .get_node() (code) [src/sidegraph/engine/reader.py:L147]
- .graph_version() (code) [src/sidegraph/engine/reader.py:L129]
- .__init__() (code) [src/sidegraph/engine/reader.py:L95]
- .list_nodes() (code) [src/sidegraph/engine/reader.py:L144]
- ._load() (code) [src/sidegraph/engine/reader.py:L115]
- .neighbors() (code) [src/sidegraph/engine/reader.py:L218]
- .nodes_in_file() (code) [src/sidegraph/engine/reader.py:L161]
- .rationale_nodes() (code) [src/sidegraph/engine/reader.py:L232]
- ._relation() (code) [src/sidegraph/engine/reader.py:L194]
- .resolve() (code) [src/sidegraph/engine/reader.py:L176]
- ._resolve_index() (code) [src/sidegraph/engine/reader.py:L165]
- .subgraph() (code) [src/sidegraph/engine/reader.py:L308]
- NodeRef (code) [src/sidegraph/engine/reader.py:L43]
- ``GraphifyReader`` — the engine seam: the ONLY module that parses Graphify's gra (rationale) [src/sidegraph/engine/reader.py:L1]
- ``f"{commit}:{content_hash}"`` when the engine stamped a ``built_at_commit``, (rationale) [src/sidegraph/engine/reader.py:L130]
- ``file_path -> anchorable nodes`` in that file, built once from a single pass (rationale) [src/sidegraph/engine/reader.py:L151]
- All anchorable nodes (code symbols / doc headings) whose source_file matches. (rationale) [src/sidegraph/engine/reader.py:L162]
- ``canonicalize(name) -> anchorable nodes`` with that canonical name, built once (rationale) [src/sidegraph/engine/reader.py:L166]
- ``node_id -> [(other_node_id, relation), ...]`` across both edge endpoints, (rationale) [src/sidegraph/engine/reader.py:L198]
- All ``rationale`` nodes (recorded reasoning), with targets resolved.          Th (rationale) [src/sidegraph/engine/reader.py:L233]
- Community id -> human label/summary, from the optional ``.graphify_labels.json`` (rationale) [src/sidegraph/engine/reader.py:L282]
- Repo-relative paths changed in the last commit (git diff HEAD~1 HEAD). (rationale) [src/sidegraph/engine/reader.py:L340]
- Reads ``graphify-out/graph.json`` read-only and answers graph queries locally. (rationale) [src/sidegraph/engine/reader.py:L93]
- RationaleNode (code) [src/sidegraph/engine/reader.py:L71]

If this session's work invalidates a record above, call supersede_decision with its id — do not leave a contradicting record alive.
