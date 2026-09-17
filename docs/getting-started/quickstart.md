# Quickstart

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

The shortest path to a first captured-and-retrieved decision, on a repo you already have.
Assumes Graphify is installed (see [installation](installation.md)). Install Sidegraph once —
`pip install sidegraph` (or `uv tool install sidegraph`) — so the `sidegraph-*` commands below
are on your PATH. (Prefer not to install? Prefix each with `uvx --from sidegraph` for the PyPI
package, or `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main` for the
latest git build. Working from a source checkout — e.g. as a contributor? Substitute
`uv run --project /ABSOLUTE/PATH/TO/sidegraph <command>` — see
["from source" in installation](installation.md#option-d--from-source-contributors).)

Throughout, replace `/ABSOLUTE/PATH/TO/your-repo` with the repo you want memory over.

> Already have the plugin installed? Say **"set up sidegraph"** (or run `/sidegraph:setup`)
> in a session started from your repo and the agent walks these same steps for you —
> engine, graph, store, wiring check, then the hand-offs in steps 4–6 below.

> Existing ADRs or supported flow specifications? After step 1, run
> `sidegraph-bootstrap --host claude-code` and follow the review. It uses production retrieval
> for the proof and does not require an account or model API. The 10–15 minute path is an
> unmeasured launch target; the [Bootstrap guide](bootstrap.md) documents the six profiles,
> host support, recovery statuses, and demo-branch maintenance loop.

1. **Build the graph** over your repo (code and/or docs — both work):

   ```bash
   cd /ABSOLUTE/PATH/TO/your-repo
   graphify update .
   ```

   This is the LLM-free build: no API key needed. It produces `graphify-out/graph.json`,
   which Sidegraph reads read-only. Re-run it after edits, or install the git hook once
   (see [`integrations/graphify.md`](../integrations/graphify.md)).

2. **Bootstrap the store**, in the same repo:

   ```bash
   sidegraph-init
   ```

   Creates `.sidegraph/` — the repo-committed sidecar, one small JSON file per decision/domain/
   entity, plus a `.gitignore` that keeps the local, derived `index.db` out of git — reports
   whether it found the graph from step 1, and prints the plugin install command (and a
   no-plugin `claude mcp add` alternative) for the next step. Safe to re-run: it says
   `already initialized` and exits `0` if the store is already there.

3. **Register Sidegraph** (MCP server + hooks) in this repo. Easiest: the Claude Code plugin
   — `/plugin marketplace add SantyagoSeaman/sidegraph` then `/plugin install
   sidegraph@sidegraph` — installs the MCP server and all three hooks in one step. Or paste
   the no-plugin snippet `sidegraph-init` just printed, or follow
   [Claude Code setup](claude-code-setup.md) or [Codex setup](codex-setup.md) for the full
   manual walkthrough.

4. **Record a gotcha**, in an agent session started from this repo:

   > Record a gotcha: we tried caching the parser output keyed by file path, but it broke
   > because two files can share a path across branches; use a content hash instead.

   The agent calls `add_decision(kind="gotcha", ...)`. It lands in the append-only store,
   committed alongside your code.

5. **Name your domains**, once, so the next `SessionStart` answers from a real table of
   contents instead of a nameless community listing. Tell the agent, in this same session:

   > Name my domains

   (or run `/sidegraph:name-domains`). It calls the read-only `list_domain_candidates` tool,
   studies the result, and presents 2–3 ready-made domain sets of different granularity as a
   compact table — you pick one (or ask it to merge/rename/drop entries first) and it proposes
   and ratifies that set. No long list to hand-curate. The moment at least one domain is
   accepted, the `SessionStart` TOC comes alive with it — no sync needed first.

   **Scripted/CI alternative** — the same proposal machinery from the command line, ratified
   by hand:

   ```bash
   sidegraph-domains bootstrap --dry-run
   sidegraph-domains bootstrap
   sidegraph-ratify
   ```

   Full walkthrough, including how to tune the proposal volume on a large repo and the CLI
   flags: [naming your domains](../guides/naming-your-domains.md).

6. **Start a new session** in the same repo and ask about the area you anchored it to:

   > I'm about to touch the parser cache — what should I know first?

   The agent calls `get_task_context` and the gotcha you just recorded comes back **first**,
   under mistakes — ranked ahead of ADRs and constraints, exactly as intended by
   mistakes-first retrieval.

That's the full loop: capture once, retrieve automatically in every later session. For the
deeper mechanics — anchoring, sync on refactor, ratification — see the integration docs
linked above.
