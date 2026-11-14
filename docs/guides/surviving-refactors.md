# Surviving refactors

> **Start here:** Follow the steps below in order for your current repository. Use the linked reference only when a command or result needs more detail. [Glossary](../concepts/glossary.md).

Code and docs change; the store's job is to keep decisions anchored — or to say plainly that
it cannot, never to guess. Rebuild the graph, then sync the record bindings.

## Rebuild, then sync

Rebuild the graph after code or document changes. Sidegraph checks for changed graph mappings
on retrieval and at session start. To inspect the result immediately, choose one path:

- **MCP-first, from inside a session:** call the `sync_anchors` MCP tool —
  `sync_anchors(force=True)` re-runs even when the graph version hasn't changed; omit
  `force` to respect the version gate on a routine re-check. Returns the same report as
  data instead of stdout text (see
  [`reference/mcp-tools.md`](../reference/mcp-tools.md#sync_anchors)) — this is what the
  [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) skill leads with.
- **CLI fallback**, for scripted/CI use or outside a session (commands are shown by name; see [running the CLIs](../reference/cli.md#running-the-clis-beside-the-plugin) for the `uvx` form):

  macOS or Linux:

  ```bash
  export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
  graphify update .            # rebuild graphify-out/graph.json (LLM-free, incremental)
  sidegraph-sync        # re-resolve every tracked entity; prints the report
  ```

  Windows PowerShell:

  ```powershell
  $env:GRAPHIFY_NO_AUTO_REFRESH = "1"
  graphify update .            # rebuild graphify-out/graph.json (LLM-free, incremental)
  sidegraph-sync        # re-resolve every tracked entity; prints the report
  ```

  `--force` for the same unconditional re-run. Like `sidegraph-ratify`, a bare
  `sidegraph-sync` (no `--db`) resolves the store as `$SIDEGRAPH_DIR` if set, else the
  deprecated `$SIDEGRAPH_DB` dispatch, else the default `.sidegraph` (existing or about to
  be created) — pass `--db`/set `SIDEGRAPH_DIR` explicitly if that's not the store you mean
  (see [`reference/cli.md`](../reference/cli.md)). The default graph is the store's own
  project's; to sync a store copied elsewhere against the real checkout, run from the
  checkout and pass `--graph graphify-out/graph.json`.

If the graph has not changed, a routine sync can skip the full pass. After committing a file
move, sync checks pending moves again. Use `--force` when you need an unconditional check.
See [sync triggers](../reference/anchoring-internals.md#sync-triggers) for the version and
reload rules.

## The rebind ladder

An exact match stays live. A verified committed file move can follow its target. An ambiguous name degrades, and a missing target becomes orphaned. Sidegraph keeps the record in every case.

See the [detailed contract](../reference/anchoring-internals.md#the-rebind-ladder).

## Community re-pointing

A rebuild can renumber communities. Sync refreshes these area-level links in the local index.

See the [detailed contract](../reference/anchoring-internals.md#community-re-pointing).

## Fresh-clone recovery

After cloning a repository with its committed store, rebuild the graph and run `sidegraph-sync` to regenerate local mappings.

See the [detailed contract](../reference/anchoring-internals.md#fresh-clone-recovery).

## Reading and healing `stale decisions` warnings

`sidegraph-sync` prints a `possibly stale decisions (all anchors gone — verify):` block for
any non-superseded, non-rejected decision whose Tier-2 (leaf) bindings all exist and are all
`orphaned`. Note the precision here: a decision with **no** leaf bindings at all (e.g. one
anchored only at Tier-0/Tier-1) is never flagged this way — surviving Tier-1/Tier-0 bindings
keep a decision retrievable via community/initiative fallback, but they don't vouch for the
underlying claim; the leaf-only check is what flags "the concrete thing this refers to is
gone, go look."

Choose the action that matches the record and the code. The
[`sidegraph:heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) skill walks
this triage finding by finding:

- **The record still holds, but its target moved or was renamed.** Use
  [`add_anchors`](../reference/mcp-tools.md#add_anchors) with the precise current name and
  file path. This appends bindings and keeps older ones as history. A damaged binding stays
  reported until sync re-resolves its original target. If the old name
  should still exist, restore it, rebuild the graph with `GRAPHIFY_NO_AUTO_REFRESH=1` and
  sync again.
- **An accepted, open decision's content changed.** Write a real successor with explicit fresh
  anchors. A `propose_decisions`
  draft with `supersedes`, or a `supersede_decision` call an agent makes on its own, leaves
  the accepted predecessor active until ratification. `supersede_decision` with
  `source="human"` (the owner asked and worded it) closes it in that call. While a
  replacement waits, `sync` still lists the predecessor as stale and a second replacement
  is refused: tell the owner which record is waiting. Omitting fresh anchors inherits the old bindings, including orphaned ones.
  See the [supersession invariant](../concepts/data-model.md#the-supersession-invariant).
- **The knowledge is obsolete with no replacement.** For an accepted decision, use
  [`retire_decision`](../reference/mcp-tools.md#retire_decision) after checking its version,
  pending-successor and evidence guards. For an obsolete proposal, use
  [`ratify(drop=[...])`](../reference/mcp-tools.md#ratify). A pending proposal is not a live
  accepted head to supersede.

For a proposal whose content is wrong, ask the human to drop it with `ratify(drop=[...])`,
then propose corrected knowledge without superseding the dropped proposal. Keep a closed
record as history. Find the accepted, open head of its chain before proposing a successor.
If there is no live head, restate current knowledge as a fresh anchored record. These
rules also apply to an accepted decision whose `valid_to` is already set.

Keep the historical records. Healing a target and changing a claim are different actions.

## Facts anchor the same way

Sync treats fact anchors like decision anchors. Use `add_anchors` for binding repair. If
the fact's content changed, apply the status checks above and provide fresh anchors for a
real successor.

See the [detailed contract](../reference/anchoring-internals.md#facts-anchor-the-same-way).

## The never-guess promise

Sync follows only moves it can verify. Review an ambiguous or orphaned result and use the heal-anchors skill to repair it.

See the [detailed contract](../reference/anchoring-internals.md#the-never-guess-promise).

### Why `moved` checks the disk

See [the detailed contract](../reference/anchoring-internals.md#why-moved-checks-the-disk).
