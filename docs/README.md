# Sidegraph documentation

Durable, repo-committed decision memory for AI coding agents. This page routes by what
you came to do; [`llms.txt`](llms.txt) is the flat annotated index of every page (point
your agent at it).

## Deciding whether to adopt

Start with the engineering whitepaper — `whitepaper/index.md` in the published
documentation — for what decision-provenance memory is, what the measurement programme
established *and failed to establish*, and which repositories it fits. Then:

- [concepts/decision-memory.md](concepts/decision-memory.md) — the core idea in one page:
  what counts as a decision, why mistakes rank first.
- [pilot-kit/README.md](pilot-kit/README.md) — run the paper's adoption gates on your own
  repository, with a kill rule and default stop conditions.
- [reference/stability.md](reference/stability.md) — what is contract, what is
  experimental, what may change under you.

## Setting up

In order:

1. [getting-started/installation.md](getting-started/installation.md) — Sidegraph and its
   Graphify dependency.
2. [getting-started/quickstart.md](getting-started/quickstart.md) — first decision
   captured and retrieved, end to end.
3. [getting-started/claude-code-setup.md](getting-started/claude-code-setup.md) or
   [getting-started/codex-setup.md](getting-started/codex-setup.md) — wire the MCP server
   and hooks into your host.
4. [guides/verifying-your-setup.md](guides/verifying-your-setup.md) — the eight-case
   checklist that proves the wiring actually works.

Already have ADRs, specs, or design docs? [getting-started/bootstrap.md](getting-started/bootstrap.md)
is the preview-first onboarding over an existing corpus;
[guides/semantic-docs.md](guides/semantic-docs.md) covers the deeper doc-import workflow.

## Using it day to day

- [guides/capturing-decisions.md](guides/capturing-decisions.md) — **the full write loop
  in plain language**: recording mid-session, the Stop-hook propose → ratify pipeline,
  facts as the evidence layer, and what makes a good record.
- [guides/retrieval-in-sessions.md](guides/retrieval-in-sessions.md) — the read side:
  what SessionStart injects, seeding task questions, reading the output blocks.
- [guides/naming-your-domains.md](guides/naming-your-domains.md) — giving the codebase a
  human-named table of contents.
- [guides/team-workflow.md](guides/team-workflow.md) — the store in a team: committing
  `.sidegraph/`, ratifying in PR review, merges and disputes, onboarding.

## Keeping it healthy

- [guides/surviving-refactors.md](guides/surviving-refactors.md) — graph rebuilds, anchor
  sync, healing stale decisions.
- [guides/ci-cd-maintenance.md](guides/ci-cd-maintenance.md) — CI recipes, and the two
  hard rules (CI never ratifies; CI never auto-pushes canonical).
- [reference/operations.md](reference/operations.md) — day-2 operations reference.
- [reference/cli.md](reference/cli.md) — every command, including `sidegraph-doctor`.
- [reference/releasing.md](reference/releasing.md) — how a release is cut: version bumps,
  the pre-release checklist, the tag-driven PyPI publish flow, and post-release checks.

## Building against it

- [reference/mcp-tools.md](reference/mcp-tools.md) — exact signatures and return shapes
  of every MCP tool.
- [reference/store-format.md](reference/store-format.md) — the file-per-record store
  format (the public contract).
- [reference/hooks.md](reference/hooks.md) and
  [reference/configuration.md](reference/configuration.md) — host wiring and every knob.
- [reference/git-bindings.md](reference/git-bindings.md) — commit/branch anchoring.
- [integrations/graphify.md](integrations/graphify.md) — the engine seam, if you wonder
  where the code graph comes from.

## Concepts, when you want the why

[concepts/](concepts/) explains the design: [decision-memory](concepts/decision-memory.md),
[mind-model](concepts/mind-model.md) (domains as a table of contents),
[data-model](concepts/data-model.md), [anchoring](concepts/anchoring.md), and
[retrieval](concepts/retrieval.md) (budget-bounded, mistakes first).
