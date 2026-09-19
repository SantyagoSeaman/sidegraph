# Quickstart

This is one complete loop: build the graph, create the store, connect one host, record one
anchored gotcha, and retrieve it in a new session. It assumes the package and Graphify are
installed as described in [Installation](installation.md).

Run the shell commands from the repository that Sidegraph should remember.

## 1. Build the graph and store

```bash
graphify update .
sidegraph-init
```

`graphify update .` creates `graphify-out/graph.json`; Sidegraph reads it but never writes to
it. `sidegraph-init` creates the repo-committed `.sidegraph/` record directories and a
gitignored derived `index.db`. Both commands are safe to run again.

If the repository already contains ADRs or supported flow specs, you can now switch to the
[preview-first bootstrap](bootstrap.md). Otherwise continue here.

## 2. Connect your host

The plugin is the recommended route for both Claude Code and Codex:

```text
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Start a new interactive session in the repository and approve the hook trust prompt. For a
manual or source-checkout setup, use the host-specific page:
[Claude Code](claude-code-setup.md) or [Codex](codex-setup.md).

## 3. Record one real gotcha

Choose a real file or symbol in the repository. Tell the agent:

> Record this gotcha against `<real path or symbol>`: caching parser output by file path
> failed because the same path can identify different content across branches; key it by a
> content hash instead.

This human-requested path calls `add_decision`, so the record lands accepted. Confirm that a
new JSON file exists under `.sidegraph/decisions/` and that its binding points at the file or
symbol you named.

## 4. Retrieve it from a fresh session

Start another session in the same repository and ask:

> Before I edit `<the same path or symbol>`, retrieve the Sidegraph task context. What should
> I know?

The agent should call `get_task_context` with that path or entity. The gotcha appears in
**Known mistakes & gotchas**, ahead of accepted ADRs and related memory.

## 5. Commit the durable part

```bash
git status --short
sidegraph-verify
```

Commit `.sidegraph/format`, `.sidegraph/.gitignore`, and the canonical JSON directories.
Do not commit `.sidegraph/index.db`; it is derived and already ignored.

The loop is now working. Next, run the short
[verification checklist](../guides/verifying-your-setup.md), then
[name domains](../guides/naming-your-domains.md) if you want a human-readable SessionStart
table of contents.
