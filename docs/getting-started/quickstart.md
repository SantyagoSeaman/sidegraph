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

In a terminal, `sidegraph-init` also asks whether to keep the code graph fresh. Yes installs a
helper and a marked block in the `post-commit`, `post-merge` and `post-checkout` git hooks, which
rebuild `graphify-out/graph.json` in the background. It never replaces a hook you already have.
`sidegraph-init --hooks` answers yes without asking, `--no-hooks` answers no and stops the
reminder, and `--remove-hooks` takes the hook out. Without a terminal it does not ask. See
[keeping the graph fresh](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks).

If the repository already contains ADRs or supported flow specs, you can now switch to the
[preview-first bootstrap](bootstrap.md). Otherwise continue here.

## 2. Connect your host

The plugin is the recommended route for both Claude Code and Codex. In Claude Code:

```text
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

In Codex, from a terminal:

```bash
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

Start a new interactive session in the repository and approve the hook trust prompt. For a
manual or source-checkout setup, use the host-specific page:
[Claude Code](claude-code-setup.md) or [Codex](codex-setup.md).

**Scope.** `/plugin install` offers three scopes: user, local and project. From a terminal the
flag is `claude plugin install sidegraph@sidegraph --scope user|project|local`, and the default
is user. Enable the plugin at user scope, or in the repository root's
`.claude/settings.local.json`, and it covers sessions started in any subdirectory. Project scope
(`.claude/settings.json`) covers only the directory it was installed from: Claude Code reads a
launch directory's own project settings and no parent's, so a plugin installed at the repository
root leaves sessions started below it without it. `sidegraph-doctor` reports the gap as
[`plugin-off-in-subdirectories`](../guides/troubleshooting.md#plugin-off-in-subdirectories).

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

Commit the whole `.sidegraph/` directory (`git add .sidegraph`). Its own `.gitignore` already
leaves out `index.db` (the derived index, with its SQLite sidecar files) and `*.tmp` files, so what
gets staged is what should travel: the `format` marker, `stamping_live_since`, `.gitignore` and the
record directories. A store file left uncommitted for a day is reported by
[`store-uncommitted`](../guides/troubleshooting.md#store-uncommitted).

The loop is now working. Next, run the short
[verification checklist](../guides/verifying-your-setup.md), then
[name domains](../guides/naming-your-domains.md) if you want a human-readable SessionStart
table of contents.
