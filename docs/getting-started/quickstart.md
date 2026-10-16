# Quickstart

This is one complete loop: build the graph, create the store, connect one host, record one
anchored gotcha, and retrieve it in a new session. It assumes Graphify is installed as described in
[Installation](installation.md). Commands are written with the `uvx` prefix the plugin uses, so
no Sidegraph install is needed. With the optional CLI installed and kept updated
([Option A](installation.md#option-a--the-cli-on-your-path)), the bare `sidegraph-*` names
work too, and the text below uses them. (`@main` is a mutable ref; to pin it see
[mutable development references](installation.md#mutable-development-references).)

On Windows, run them in PowerShell and use the line marked for it: in Windows PowerShell 5.1,
`>>` writes UTF-16 and git would ignore the `.gitignore` entry.
Run the shell commands from the repository that Sidegraph should remember.

For an existing store, follow the [0.14 migration guide](../guides/migrating-to-0.14.md)
before upgrading Graphify; update every Sidegraph client first.

## 1. Build the graph and store

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"      # Windows PowerShell: use this line instead
graphify update .
echo "graphify-out/" >> .gitignore          # macOS, Linux
Add-Content .gitignore "graphify-out/"      # Windows PowerShell: use this line instead, not >>
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-init
```

`graphify update .` creates `graphify-out/graph.json`; Sidegraph reads it but never writes to
it. The graph is rebuilt from your code, so keep `graphify-out/` out of git. `sidegraph-init`
creates the repo-committed `.sidegraph/` record directories and a gitignored derived
`index.db`. Both commands are safe to run again.

Run `sidegraph-init` from the repository root before starting your host session. On Claude Code and Codex, without a
store, SessionStart shows setup guidance and Stop stays silent; neither creates one.
OpenCode and Muse use explicit MCP calls instead of lifecycle hooks. If an
older hook already left an empty store, init identifies it as empty and prints setup guidance.

In a terminal, `sidegraph-init` asks two questions. Both default to yes, so pressing Enter twice
accepts them.

- *Keep the code graph fresh?* Yes installs a helper and a marked block in the `post-commit`,
  `post-merge` and `post-checkout` git hooks, which rebuild `graphify-out/graph.json` in the
  background. It never replaces a hook you already have. `sidegraph-init --hooks` answers yes
  without asking, `--no-hooks` answers no and stops the reminder, and `--remove-hooks` takes the
  hook out. See
  [keeping the graph fresh](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks).
- *Auto-ratify low-risk records?* Yes means the lessons, gotchas and facts the agent proposes
  are accepted without review; ADRs, constraints and domains still wait for a person. The
  answer is saved in `.claude/settings.json`, which you commit. Sidegraph applies it in all four MCP integrations unless an explicit
  `SIDEGRAPH_RATIFY_POLICY` overrides it, so teammates use the same project policy. See
  [`sidegraph-init`](../reference/cli.md#sidegraph-init).

Without a terminal it asks neither question.

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

Start a new interactive session in the repository and review its approval prompts; Codex
hooks require explicit trust through the startup warning or `/hooks`. For OpenCode V2, use
the [native Git installation guide](../../.opencode/INSTALL.md); for experimental MCP/skills-only
Muse, use the [Muse guide](../integrations/muse.md) and start its process in this project.
The [README host recipes](../../README.md#claude-code) describe installation and updates. For a
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
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-verify
```

Commit the whole `.sidegraph/` directory (`git add .sidegraph`), the root `.gitignore`, and
`.claude/settings.json` if step 1 created or changed it. The store's own `.gitignore` already
leaves out `index.db` (the derived index, with its SQLite sidecar files) and `*.tmp` files, so what
gets staged is what should travel: the `format` marker, `stamping_live_since`, `.gitignore` and the
record directories. A store file left uncommitted for a day is reported by
[`store-uncommitted`](../guides/troubleshooting.md#store-uncommitted).

The loop is now working. After cloning, each teammate builds the graph, runs `sidegraph-init`
and installs the plugin. In a clone, `sidegraph-init` leaves the committed records and settings
alone and only installs the graph hook.

Next, run the short
[verification checklist](../guides/verifying-your-setup.md), then
[name domains](../guides/naming-your-domains.md) if you want a table of contents of your memory. Claude Code and Codex also inject it at
SessionStart; OpenCode and Muse retrieve it explicitly through MCP.
