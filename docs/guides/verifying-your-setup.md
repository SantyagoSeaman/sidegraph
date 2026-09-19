# Verify your setup

These four checks prove the useful path without turning installation into a test project.
Run them in the repository whose `.sidegraph/` you configured.

## Case 1 — Host wiring

**Proves:** the MCP server and SessionStart hook point at this repository.

1. Start a fresh interactive agent session in the repository.
2. Confirm that the opening context mentions Sidegraph memory or its domain map.
3. Ask the agent to call `get_task_context` for one existing file.

**Success:** the tool is available and returns either task context or `No context found.` A
missing record is fine; “unknown tool”, a different repository's records, or a silent
SessionStart hook is not. For Codex, confirm hook trust before debugging the JSON.

If this fails, compare the resolved paths with
[Configuration](../reference/configuration.md#store-path-resolution) and the relevant host
page: [Claude Code](../getting-started/claude-code-setup.md) or
[Codex](../getting-started/codex-setup.md).

## Case 2 — Durable capture and retrieval

**Proves:** a record reaches canonical JSON and is retrievable from its anchor.

Ask the agent to record an accepted gotcha against a real file or symbol, then request task
context for the same anchor. Run:

```bash
sidegraph-verify
git status --short
```

**Success:** the gotcha appears under **Known mistakes & gotchas**; `sidegraph-verify` reports
clean; git shows small files under `.sidegraph/` but not `.sidegraph/index.db`.

If retrieval misses it, inspect the write result's `anchors_skipped` and
`anchors_orphaned` fields before adding another record. Ambiguous and unresolved anchors need
healing, not duplicate memory.

## Case 3 — Ratification and evidence cascade

**Proves:** agent-authored drafts stay visibly unreviewed and an attached fact follows its
decision's verdict.

For this check, use `SIDEGRAPH_RATIFY_POLICY=manual` and ensure legacy
`SIDEGRAPH_AUTO_ACCEPT` is unset or `off`. Restart the host after changing its environment.
Ask the agent to propose an ADR with one attached fact, both anchored to a real file.

Before ratification, `list_proposed` should show the fact nested under the decision and
`get_task_context` should place proposed content only under **Unratified proposals**. Then
ratify the decision.

**Success:** the ratify result includes `accepted (evidence of <decision-id>)` for the fact.
The next task-context call shows the accepted ADR and an indented `evidence:` line; neither
record remains in the proposal block.

## Case 4 — Sync and repository hygiene

**Proves:** the graph/store relationship is healthy and derived state stays local.

```bash
graphify update .
sidegraph-sync --check
sidegraph-doctor
git status --short
```

**Success:** sync reports no actionable degraded/orphaned anchors, doctor reports no strict
violations, and `.sidegraph/index.db` remains absent from git output. Graphify may update its
own output. Sidegraph canonical files should stay unchanged during an ordinary sync; the
documented exception is a confirmed leaf-file move, which updates that entity's durable
descriptor.

Advisory doctor findings are work items, not corruption. Use
[`sidegraph:heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) for anchor
damage and [Surviving refactors](surviving-refactors.md) for the rebind rules.
