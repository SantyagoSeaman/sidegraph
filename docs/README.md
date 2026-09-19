# Sidegraph documentation

Sidegraph stores durable decisions and non-derivable facts beside the repository they
describe. Start with the task you have now; [`llms.txt`](llms.txt) is the flat index for an
agent.

## Install and prove it works

1. [Install Sidegraph and Graphify](getting-started/installation.md).
2. Follow the [end-to-end quickstart](getting-started/quickstart.md).
3. Use the recommended host path for [Claude Code](getting-started/claude-code-setup.md) or
   [Codex](getting-started/codex-setup.md).
4. Run the [four-case verification checklist](guides/verifying-your-setup.md).

If the repository already has ADRs or specs, use the
[preview-first bootstrap](getting-started/bootstrap.md) after installation.

## Use it during normal work

- [Retrieve context before editing](guides/retrieval-in-sessions.md).
- [Capture decisions and supporting facts](guides/capturing-decisions.md).
- [Name domains for the SessionStart table of contents](guides/naming-your-domains.md).
- [Share and review the store as a team](guides/team-workflow.md).

## Operate and maintain it

- [Survive refactors and heal anchors](guides/surviving-refactors.md).
- [Run CI and scheduled maintenance](guides/ci-cd-maintenance.md).
- [Import decision-shaped documents](guides/semantic-docs.md).
- [See runtime cost and operational boundaries](reference/operations.md).
- [Cut a Sidegraph release](reference/releasing.md) (maintainers).

The [pilot kit](pilot-kit/README.md) is an evaluation protocol for teams deciding whether to
adopt Sidegraph. The engineering whitepaper is generated in the published snapshot at
[`docs/whitepaper/index.md`](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/whitepaper/index.md);
it is optional background on evidence, limits, and repository fit.

## Understand the model

- [Decision memory](concepts/decision-memory.md) — what belongs in the store.
- [Mind model](concepts/mind-model.md) — domains, TOC, and drill-down.
- [Data model](concepts/data-model.md) — persisted record shapes.
- [Anchoring](concepts/anchoring.md) — how records stay attached to code and docs.
- [Retrieval](concepts/retrieval.md) — ranking, trust quarantine, and budgets.

## Look up an exact contract

- [MCP tools](reference/mcp-tools.md)
- [CLI](reference/cli.md)
- [Hooks](reference/hooks.md)
- [Configuration](reference/configuration.md)
- [Store format](reference/store-format.md)
- [Stability levels](reference/stability.md)
- [Git bindings](reference/git-bindings.md)
- Integrations: [Claude Code](integrations/claude-code.md),
  [Codex](integrations/codex.md), [Graphify](integrations/graphify.md)
