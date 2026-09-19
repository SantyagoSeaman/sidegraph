# CI/CD maintenance

Sidegraph's store is a git-native log, so the obvious next step is to check its integrity
the same way you check code: on every pull request, and on a schedule. This guide has three
GitHub Actions recipes — an anchor-health required check, a store-lint on PR, and a
scheduled LLM triage job — plus the two hard rules that make all three safe to run
unattended.

## The two hard rules

1. **CI never ratifies.** Nothing in these recipes calls `ratify`/`sidegraph-ratify --accept`
   (or `--drop`, for that matter). A scheduled job may *propose* a superseding decision
   (`propose_decisions` with `supersedes`) or *recommend* a drop in its summary, but turning
   a proposal into an accepted/rejected terminal state stays a human action, every
   time. See [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md#triage-decision-tree-add_anchors-propose_decisions-or-recommend-a-drop)
   for the triage tree these jobs run. An auto-ratification policy would break this rule
   without any `ratify` call — the transition rides `propose_decisions` itself, which the
   deny list below cannot block. Legacy `SIDEGRAPH_AUTO_ACCEPT=on` bypasses the queue too.
   The scheduled recipe therefore pins `SIDEGRAPH_RATIFY_POLICY=manual` and
   `SIDEGRAPH_AUTO_ACCEPT=off` in the MCP server's `env`. A deployment that does want a
   policy pins it in a committed `.claude/settings.json` `env` block, never in the ambient
   shell.
2. **CI never auto-pushes canonical files to a branch nobody reviewed.** `sidegraph-sync`
   and the triage playbook's tools (`add_anchors`, `propose_decisions`) can **write** — sync
   normally changes derived `index.db` state and may canonically update a confirmed moved
   leaf's descriptor; re-anchoring and proposals write canonical files. That's fine in an
   ephemeral CI checkout (the writes vanish with the runner unless something commits them),
   but nothing here ever pushes those files straight to a
   protected branch. The scheduled recipe below opens a **pull request** with whatever it
   wrote, same as a human's own change — merging that PR is a separate, reviewed step, and
   ratifying any proposed decisions inside it is a separate step again.

## Recipe 1: anchor-health required check

Runs `sidegraph-sync --json --check` on every PR (and on pushes to the default branch) and
fails the job when the report has an attention finding. It needs no explicit comparison
ref: rebuild the graph, sync, then classify the report.

Create a repository variable named `SIDEGRAPH_REF` containing a reviewed Sidegraph commit
SHA. Every recipe reads it as `${{ vars.SIDEGRAPH_REF }}`. See
[mutable development references](../getting-started/installation.md#mutable-development-references)
for why CI should not follow a branch.

```yaml
name: sidegraph-anchor-health

on:
  pull_request:
  push:
    branches: [main]

jobs:
  anchor-health:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write   # the github-script comment step below needs this
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

      - name: Install uv
        uses: astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4 # v10.1.0

      - name: Rebuild the graph
        run: uvx --from graphifyy==0.9.6 graphify update .

      - name: sidegraph-sync --json --check
        id: sync
        continue-on-error: true   # let later steps run even when this exits non-zero
        run: |
          uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@${{ vars.SIDEGRAPH_REF }} \
            sidegraph-sync --json --check > sync-report.json
          cat sync-report.json

      - name: Classify the outcome
        id: classify
        if: steps.sync.outcome == 'failure'
        run: |
          # sidegraph-sync only prints JSON on exit 0/2 (a completed report); exit 1
          # (operational error — graph or store unreadable) prints a plain-text error
          # line instead, so a JSON-parse failure is how you tell the two apart in CI.
          if jq -e . sync-report.json >/dev/null 2>&1; then
            echo "kind=findings" >> "$GITHUB_OUTPUT"
          else
            echo "kind=operational-error" >> "$GITHUB_OUTPUT"
          fi

      - name: Upload the report
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: sidegraph-sync-report
          path: sync-report.json

      - name: Comment findings on the PR
        if: github.event_name == 'pull_request' && steps.classify.outputs.kind == 'findings'
        uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3 # v9.0.0
        with:
          script: |
            const fs = require('fs');
            const report = JSON.parse(fs.readFileSync('sync-report.json', 'utf8'));
            const lines = [];
            for (const o of report.outcomes) {
              if (['orphaned', 'ambiguous', 'error'].includes(o.status)) {
                lines.push(`- **${o.status}**: \`${o.canonical_name}\` — ${o.detail ?? 'no match'}`);
              }
            }
            for (const d of report.stale_decisions) {
              lines.push(`- **stale decision** \`${d.id}\`: ${d.title}`);
            }
            for (const c of report.slug_conflicts) {
              lines.push(`- **slug conflict** \`${c.slug}\` held by ${c.domain_ids.join(', ')}`);
            }
            for (const f of report.domain_failures) {
              lines.push(`- **domain refresh failed** \`${f.slug}\`: ${f.error}`);
            }
            const body = '### Sidegraph anchor-health findings\n\n' + lines.join('\n') +
              '\n\nRun the `heal-anchors` skill (or `sidegraph-sync` locally) to triage.';
            await github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body,
            });

      - name: Fail on findings or an operational error
        if: steps.sync.outcome == 'failure'
        run: |
          echo "::error::sidegraph-sync --check reported: ${{ steps.classify.outputs.kind }}"
          exit 1
```

Only an `error` outcome, stale decision, slug conflict, or domain refresh failure fails the
job. `orphaned`/`ambiguous` outcomes and `empty_domains`/`overbroad_domains` are informational
per [`sidegraph-sync`](../reference/cli.md#sidegraph-sync). They remain in the artifact; the
PR comment includes them only when the same run also has a hard finding.

**Why the `permissions:` block:** GitHub's default `GITHUB_TOKEN` may be read-only. Declaring
`pull-requests: write` is required for the comment step, but it cannot override GitHub's
security downgrade for fork-originated PRs unless repository administrators explicitly allow
write tokens for fork workflows. On those PRs, skip the comment or use a separately reviewed
workflow; the required check and artifact can still run with read access.

**This job writes local derived state** and, on a confirmed `moved` rebind, may rewrite the
affected entity's durable descriptor. Binding status, community mappings, domain communities,
and community re-pointing remain index-only. The job never commits or pushes either kind.

## Recipe 2: store lint on PR

Runs the transition layer — `sidegraph-verify --against <ref>` — to catch a hand-edited or
history-rewritten store file that no MCP tool would ever produce.

**Diff against the merge base, not a moving branch name.** Passing `origin/main` directly can
include legitimate base-branch changes when the checkout is a raw PR head or the fetch is
stale. Use the common ancestor so the transition diff contains only branch changes:

```yaml
name: sidegraph-store-lint

on:
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0   # full history — merge-base needs it

      - name: Install uv
        uses: astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4 # v10.1.0

      - name: Compute the PR's merge base
        id: base
        run: |
          git fetch origin "$GITHUB_BASE_REF"
          echo "ref=$(git merge-base "origin/$GITHUB_BASE_REF" HEAD)" >> "$GITHUB_OUTPUT"

      - name: sidegraph-verify --against <merge-base>
        id: verify
        continue-on-error: true   # let later steps run even when this exits non-zero
        run: |
          uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@${{ vars.SIDEGRAPH_REF }} \
            sidegraph-verify --against "${{ steps.base.outputs.ref }}" --json \
            > verify-report.json
          cat verify-report.json

      - name: Classify the outcome
        id: classify
        if: steps.verify.outcome == 'failure'
        run: |
          # sidegraph-verify only prints JSON on exit 0/2 (a completed report); exit 1
          # (operational error — unreadable store, an unresolvable --against ref) prints a
          # plain-text error line instead — same convention as sidegraph-sync --json.
          if jq -e . verify-report.json >/dev/null 2>&1; then
            echo "kind=violations" >> "$GITHUB_OUTPUT"
          else
            echo "kind=operational-error" >> "$GITHUB_OUTPUT"
          fi

      - name: Upload the report
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: sidegraph-verify-report
          path: verify-report.json

      - name: Fail the job on violations or an operational error
        if: steps.verify.outcome == 'failure'
        run: |
          echo "::error::sidegraph-verify --against reported: ${{ steps.classify.outputs.kind }} — see the uploaded report"
          exit 1
```

`sidegraph-verify` is a pure read: it opens canonical JSON directly, never constructs a
`Store`, and therefore cannot migrate the store or rebuild `index.db`. See
[`reference/cli.md#sidegraph-verify`](../reference/cli.md#sidegraph-verify).

## Recipe 3: scheduled triage

Runs the [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) playbook
headlessly, on a schedule, and opens a PR with whatever it proposed — proposals only,
never a push straight to the default branch, never a `ratify` call.

**Prerequisites — four one-time repo settings:**

1. **`ANTHROPIC_API_KEY` repo (or org) secret** — Settings → Secrets and variables →
   Actions → New repository secret; the workflow reads it via `secrets.ANTHROPIC_API_KEY`
   below. Without it the `claude -p` step fails outright.
2. **`SIDEGRAPH_TRIAGE_MODEL` repository variable** — set it to a model ID supported by the
   installed Claude Code version. Keeping the value outside this guide prevents a model
   rename from turning the example into stale copy-paste configuration.
3. **"Allow GitHub Actions to create and approve pull requests"** — Settings → Actions →
   General → Workflow permissions. This is off by default on a new repo; without it the
   `create-pull-request` step below 403s (measured live). Toggle it in the UI, or set it
   from the CLI:
   ```bash
   gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow \
     -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
   ```
4. **Bot-authored PRs wait for a maintainer's approval to run checks.** A PR opened by
   `github-actions[bot]` via the default `GITHUB_TOKEN` sits with its checks in "action
   required" until a maintainer clicks "Approve and run" on the PR (measured live) — this
   is **expected behavior, not a misconfiguration**: a human gate before CI executes
   anything a bot proposed, on top of the human `ratify` gate the two hard rules already
   require. Teams that want checks to run automatically can swap `GITHUB_TOKEN` for a PAT
   with `contents`/`pull-requests` write scope belonging to a real user, which skips that
   gate — mentioned here, not recommended.

```yaml
name: sidegraph-triage

on:
  schedule:
    - cron: '0 6 * * 1'   # every Monday
  workflow_dispatch: {}

jobs:
  triage:
    runs-on: ubuntu-latest
    permissions:
      contents: write        # create-pull-request needs to push its new branch
      pull-requests: write   # ...and open the PR itself
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

      - name: Install uv
        uses: astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4 # v10.1.0

      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code

      - name: Rebuild the graph
        run: uvx --from graphifyy==0.9.6 graphify update .

      - name: Write the MCP config
        run: |
          cat > mcp-config.json <<'EOF'
          {
            "mcpServers": {
              "sidegraph": {
                "command": "uvx",
                "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@${{ vars.SIDEGRAPH_REF }}", "sidegraph-mcp"],
                "env": {
                  "SIDEGRAPH_DIR": ".sidegraph",
                  "SIDEGRAPH_GRAPH": "graphify-out/graph.json",
                  "SIDEGRAPH_RATIFY_POLICY": "manual",
                  "SIDEGRAPH_AUTO_ACCEPT": "off"
                }
              }
            }
          }
          EOF

      - name: Run the heal-anchors triage — proposals only
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SIDEGRAPH_TRIAGE_MODEL: ${{ vars.SIDEGRAPH_TRIAGE_MODEL }}
        run: |
          claude -p 'Run the heal-anchors playbook: call sync_anchors(force=True), then for
          every orphaned/ambiguous entity and every stale decision, follow the triage tree —
          add_anchors for code that moved, propose_decisions(supersedes=...) for content
          that is genuinely outdated, and a plain-text recommendation (never a tool call)
          for anything to drop. Summarize every action taken and every recommendation at
          the end.' \
            --model "$SIDEGRAPH_TRIAGE_MODEL" \
            --mcp-config mcp-config.json --strict-mcp-config \
            --allowedTools "mcp__sidegraph__sync_anchors mcp__sidegraph__find_entity mcp__sidegraph__query_structure mcp__sidegraph__query_decisions mcp__sidegraph__add_anchors mcp__sidegraph__propose_decisions mcp__sidegraph__list_proposed mcp__sidegraph__retrieve_decisions mcp__sidegraph__get_entity_history mcp__sidegraph__list_facts mcp__sidegraph__verify_store" \
            --disallowedTools "mcp__sidegraph__ratify,mcp__sidegraph__ratify_decisions,mcp__sidegraph__add_decision,mcp__sidegraph__supersede_decision,mcp__sidegraph__supersede_fact,mcp__sidegraph__supersede_domain,mcp__sidegraph__add_domain,mcp__sidegraph__add_fact" \
            --output-format stream-json --verbose \
            > triage.jsonl

      - name: Extract the run's final summary
        run: |
          jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' \
            triage.jsonl | tail -n 60 > triage-summary.md
          cat triage-summary.md

      - name: Upload the raw transcript
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: sidegraph-triage-transcript
          path: triage.jsonl

      - name: Open a PR with whatever the triage proposed
        uses: peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1 # v8.1.1
        with:
          commit-message: "chore(sidegraph): scheduled anchor triage"
          branch: sidegraph/triage-${{ github.run_id }}
          title: "Sidegraph anchor triage — proposed re-anchors and supersessions"
          body-path: triage-summary.md
          add-paths: .sidegraph
          labels: sidegraph-triage
```

See **Prerequisites** above for the API key, pinned model, PR-creation toggle, and bot-approval
gate. The command passes the repository variable explicitly instead of inheriting a changing
CLI default.

Do not add `--permission-mode bypassPermissions`: it disables both tool lists. The
`--disallowedTools` list is the load-bearing ratification guard; the allowlist only narrows
normal MCP access and can be widened by ambient host settings. Keep both. Read-only built-in
tools can still inspect the checkout in a headless run; the troubleshooting note below states
that boundary explicitly. The matching
[`heal-anchors` headless example](../../plugin/sidegraph/skills/heal-anchors/SKILL.md#running-this-playbook-headlessly-ci)
uses the same deny-list rule.

`create-pull-request` stages only `.sidegraph/`, pushes a new branch, and opens a PR. Merging
it lands re-anchors and adds drafts to the unratified queue; a human still ratifies those
drafts. Proposed content stays below accepted memory and may be hidden by regulated mode.

## Troubleshooting

- **Archive-duplicate false alarm.** `sidegraph-verify` flags a duplicate ULID across two
  archive segments only when their payloads actually *differ*. Two segments carrying
  byte-**identical** copies of the same record (an independent `sidegraph-compact` run on
  each of two branches, later merged) are the store's own sanctioned shape and never flagged
  — see [`reference/store-format.md#archive-segments-sidegraph-compact`](../reference/store-format.md#archive-segments-sidegraph-compact).
  If Recipe 2 does flag a `duplicate-ulid`, the segments differ in content, which is a real
  problem, not this exemption misfiring.
- **Exit codes, all three CLIs used above:** `0` clean (or, for `sidegraph-sync`, a
  version-skip), `1` an *operational* error (unreadable graph/store; for verification's
  transition layer, an unresolvable `--against` ref or no git repository), `2`
  findings/violations actually present. Recipes 1
  and 2 fail the job on either `1` or `2` (`continue-on-error` + a manual `exit 1` at the
  end) but classify which one first, since an operational error usually means the runner's
  environment is broken (wrong `SIDEGRAPH_DIR`, `graphify update .` never ran, a shallow
  checkout with no merge base) rather than a real integrity problem in the store.
- **`sidegraph-sync` writes locally — mostly to derived state.** Binding status, community
  bindings, domain communities, and the TOC cache stay in gitignored `index.db`. A confirmed
  leaf-file move is the exception: sync updates that entity's committed descriptor. Recipe 1
  never commits either result; CI's job is to detect drift, not silently publish it.
- **`fetch-depth: 0` missing → merge-base fails.** `actions/checkout`'s default
  (`fetch-depth: 1`) only fetches the tip commit, so `git merge-base` in Recipe 2 has
  nothing to search and errors out (an operational error, exit `1`, not a false-clean
  result) — the workflow above sets `fetch-depth: 0` for exactly this reason.
- **A headless `claude -p` run can still read the repo — that's by design, not a leak.**
  Read-only built-in tools (`Read`, a plain read-only `Bash` invocation) are auto-permitted
  in headless runs independently of `--allowedTools`/`--disallowedTools`, which here name
  only `mcp__sidegraph__*` tools (measured live: the transcript shows several `Bash` reads and a
  `Read` running, alongside one `Bash` call the same static command classification denied —
  its shape didn't read as safely read-only). What the two flags together guarantee is
  narrower and still sufficient for hard rule 1: the run cannot **write** anything except
  through the allowlisted MCP tools, and the deny list blocks every write-gate tool
  (`ratify`, `add_decision`, `supersede_*`, ...) outright. If you see "no Bash/Edit/Write
  reachable" claimed anywhere for this setup, that's wrong — treat repo contents as
  readable by the run and scope secrets accordingly.

## See also

- [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) — the triage tree
  these recipes run, and the interactive/in-session version of Recipe 3.
- [`reference/cli.md`](../reference/cli.md#sidegraph-sync) — `sidegraph-sync`'s and
  `sidegraph-verify`'s full flag/exit-code reference.
- [`reference/mcp-tools.md`](../reference/mcp-tools.md#verify_store) — `verify_store` and
  `add_anchors`'s MCP shapes.
- [`guides/team-workflow.md`](team-workflow.md) — committing `.sidegraph/` as an ordinary
  part of the repo, outside of CI.
