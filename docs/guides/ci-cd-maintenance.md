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
   a proposal into `accepted` — or a decision into `dropped` — stays a human action, every
   time. See [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md#triage-decision-tree-add_anchors-propose_decisions-or-recommend-a-drop)
   for the triage tree these jobs run. An auto-ratification policy would break this rule
   without any `ratify` call — the transition rides `propose_decisions` itself, which the
   deny list below cannot block — so every recipe here pins `SIDEGRAPH_RATIFY_POLICY` to
   `manual` in the MCP server's `env` (a deployment that does want a policy pins it instead
   in a committed `.claude/settings.json` `env` block, never left to the ambient shell).
2. **CI never auto-pushes canonical files to a branch nobody reviewed.** `sidegraph-sync`
   and the triage playbook's tools (`add_anchors`, `propose_decisions`) all **write** —
   re-pointed bindings, a moved entity's refreshed `descriptor`, a newly proposed decision's
   record file. That's fine in an ephemeral CI checkout (the writes vanish with the runner
   unless something commits them), but nothing here ever pushes those files straight to a
   protected branch. The scheduled recipe below opens a **pull request** with whatever it
   wrote, same as a human's own change — merging that PR is a separate, reviewed step, and
   ratifying any proposed decisions inside it is a separate step again.

## Recipe 1: anchor-health required check

Runs `sidegraph-sync --json --check` on every PR (and on pushes to the default branch) and
fails the job when the report has an attention finding. This is the cheapest of the three —
no git-history plumbing, just a rebuild-and-check.

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

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
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Rebuild the graph
        run: uvx --from graphifyy graphify update .

      - name: sidegraph-sync --json --check
        id: sync
        continue-on-error: true   # let later steps run even when this exits non-zero
        run: |
          uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main \
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
        uses: actions/upload-artifact@v4
        with:
          name: sidegraph-sync-report
          path: sync-report.json

      - name: Comment findings on the PR
        if: github.event_name == 'pull_request' && steps.classify.outputs.kind == 'findings'
        uses: actions/github-script@v7
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

Only an `error` outcome, a stale decision, a slug conflict, or a domain refresh failure fails
`steps.sync.outcome`. `orphaned`/`ambiguous` outcomes and `empty_domains`/`overbroad_domains`
never do — they're
informational-only per `sync.report_has_findings` — see
[`reference/cli.md#sidegraph-sync`](../reference/cli.md#sidegraph-sync) — so a rename+heal
that leaves a renamed-away entity's leaf orphaned for good turns this check green instead of
staying red forever. They're still in the uploaded JSON artifact — and, when the run also
has a hard finding, in the PR comment above — for a human to look at, just never a reason to
fail the job (a comment only posts on failure, so an informational-only run is green with
the details in the artifact).

**Why the `permissions:` block:** GitHub's default `GITHUB_TOKEN` is read-only on many repos
(org-level defaults, or any workflow triggered from a forked PR) — without `pull-requests:
write` declared explicitly, the `github-script` comment step 403s instead of posting.
`contents: read` is enough for `actions/checkout`; this job never writes to the repo itself.

**This job writes to the checkout's `.sidegraph/`** — `sidegraph-sync` re-points bindings
and, on a "moved" rebind, rewrites the affected entity's `descriptor`. That's expected (hard
rule 2 above): the job never commits or pushes those files; they exist only for the
duration of the report.

## Recipe 2: store lint on PR

Runs the transition layer — `sidegraph-verify --against <ref>` — to catch a hand-edited or
history-rewritten store file that no MCP tool would ever produce.

**Diff against the merge base, not a moving branch name.** `sidegraph-verify --against <ref>`
runs `git diff <ref>` internally, which compares `<ref>`'s snapshot against the *current
working tree* — not two fixed points in history. `git merge-base` is what makes that safe
regardless of checkout shape: it always resolves to the real common ancestor between the
base ref and whatever `HEAD` is, so the diff only ever covers what the current branch itself
changed. Passing a moving branch name directly (`--against origin/main`) skips that and
risks the exact failure mode this recipe exists to avoid: if `HEAD` doesn't already include
everything `origin/main`'s current tip does (a plain checkout of the PR branch's own tip
commit, a rebase workflow, a local run against a stale fetch), the diff also picks up every
legitimate transition that landed on the base branch after `HEAD` diverged from it — those
look like the base's own records reversing status, and the transition layer correctly, but
uselessly, flags them as illegal. (One wrinkle worth knowing: on GitHub's default
`pull_request` trigger, `actions/checkout`'s default ref is actually a *synthetic merge
commit* — `refs/pull/<pr>/merge`, merging the PR branch onto the base's current tip — so
`git merge-base origin/$GITHUB_BASE_REF HEAD` resolves to that current base tip itself, not
an older fork-point commit; the diff is still exactly the PR's own edits either way. The
point of computing it explicitly is that this stays true even if the checkout ref changes —
e.g. to the PR's raw head SHA — later.) Compute the merge base explicitly:

```yaml
name: sidegraph-store-lint

on:
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # full history — merge-base needs it

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Compute the PR's merge base
        id: base
        run: |
          git fetch origin "$GITHUB_BASE_REF"
          echo "ref=$(git merge-base "origin/$GITHUB_BASE_REF" HEAD)" >> "$GITHUB_OUTPUT"

      - name: sidegraph-verify --against <merge-base>
        id: verify
        continue-on-error: true   # let later steps run even when this exits non-zero
        run: |
          uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main \
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
        uses: actions/upload-artifact@v4
        with:
          name: sidegraph-verify-report
          path: verify-report.json

      - name: Fail the job on violations or an operational error
        if: steps.verify.outcome == 'failure'
        run: |
          echo "::error::sidegraph-verify --against reported: ${{ steps.classify.outputs.kind }} — see the uploaded report"
          exit 1
```

`sidegraph-verify` is read-only in both layers (see
[`reference/cli.md#sidegraph-verify`](../reference/cli.md#sidegraph-verify)) — this job
writes nothing, so it needs no PR/push step at all, unlike Recipes 1 and 3.

## Recipe 3: scheduled triage

Runs the [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) playbook
headlessly, on a schedule, and opens a PR with whatever it proposed — proposals only,
never a push straight to the default branch, never a `ratify` call.

**Prerequisites — three one-time repo settings, verified live:**

1. **`ANTHROPIC_API_KEY` repo (or org) secret** — Settings → Secrets and variables →
   Actions → New repository secret; the workflow reads it via `secrets.ANTHROPIC_API_KEY`
   below. Without it the `claude -p` step fails outright.
2. **"Allow GitHub Actions to create and approve pull requests"** — Settings → Actions →
   General → Workflow permissions. This is off by default on a new repo; without it the
   `create-pull-request` step below 403s (measured live). Toggle it in the UI, or set it
   from the CLI:
   ```bash
   gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow \
     -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
   ```
3. **Bot-authored PRs wait for a maintainer's approval to run checks.** A PR opened by
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
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code

      - name: Rebuild the graph
        run: uvx --from graphifyy graphify update .

      - name: Write the MCP config
        run: |
          cat > mcp-config.json <<'EOF'
          {
            "mcpServers": {
              "sidegraph": {
                "command": "uvx",
                "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@main", "sidegraph-mcp"],
                "env": {
                  "SIDEGRAPH_DIR": ".sidegraph",
                  "SIDEGRAPH_GRAPH": "graphify-out/graph.json",
                  "SIDEGRAPH_RATIFY_POLICY": "manual"
                }
              }
            }
          }
          EOF

      - name: Run the heal-anchors triage — proposals only
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          claude -p 'Run the heal-anchors playbook: call sync_anchors(force=True), then for
          every orphaned/ambiguous entity and every stale decision, follow the triage tree —
          add_anchors for code that moved, propose_decisions(supersedes=...) for content
          that is genuinely outdated, and a plain-text recommendation (never a tool call)
          for anything to drop. Summarize every action taken and every recommendation at
          the end.' \
            --model claude-sonnet-5 \
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
        uses: actions/upload-artifact@v4
        with:
          name: sidegraph-triage-transcript
          path: triage.jsonl

      - name: Open a PR with whatever the triage proposed
        uses: peter-evans/create-pull-request@v7
        with:
          commit-message: "chore(sidegraph): scheduled anchor triage"
          branch: sidegraph/triage-${{ github.run_id }}
          title: "Sidegraph anchor triage — proposed re-anchors and supersessions"
          body-path: triage-summary.md
          add-paths: .sidegraph
          labels: sidegraph-triage
```

See **Prerequisites** above for the `ANTHROPIC_API_KEY` secret, the PR-creation toggle, and
the bot-approval gate this workflow depends on.

**Model.** The `--model claude-sonnet-5` flag pins the triage model explicitly rather than
riding the CLI default. Triage is mechanical — `sync_anchors` plus a walk down the
`heal-anchors` decision tree — so a mid-tier model is the right cost/quality point; bump it
only if your corpus needs deeper judgment to tell "moved" from "genuinely outdated". Prefer
the flag over an `ANTHROPIC_MODEL` env var: it keeps the model visible in the command.

**No `--permission-mode bypassPermissions` here either**, for the same reason as the
`heal-anchors` skill's own headless example: bypass skips the permission system outright, so
neither `--allowedTools` nor `--disallowedTools` would bind anything under it and the run
could call any tool, `ratify` included. **The `--disallowedTools` deny list is the
load-bearing enforcement of hard rule 1** — it blocks the write-gate tools no matter what
the ambient permission configuration says. The `--allowedTools` list is scoping, not
enforcement: on a pristine CI runner a headless `-p` run auto-denies *MCP* tool calls
outside it (no terminal to prompt), but any permissive permission configuration on the
machine — a user- or project-level `settings.json` with a broad allow policy, exactly what a
developer laptop or a shared self-hosted runner may carry — silently overrides
allowlist-only narrowing (measured live: a ratify call went through a narrowed allowlist on
a permissively-configured machine, and only the deny list stopped it). Always ship both, as
above; never rely on the allowlist alone. Separately, read-only built-in tools (`Read`, a
simple read-only `Bash` invocation) are auto-permitted in headless runs regardless of
`--allowedTools` — that flag only scopes `mcp__sidegraph__*` calls; see the Troubleshooting
note below for what this does and doesn't mean for what the run can touch.

The list names the exact triage tools — `sync_anchors`, `find_entity`, `query_structure`,
`query_decisions`, `add_anchors`, `propose_decisions`, `list_proposed`, plus the read-only
`retrieve_decisions`, `get_entity_history`, `list_facts`, `verify_store` — narrower than the
wildcard (`mcp__sidegraph__*`) the
[`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md#running-this-playbook-headlessly-ci)
skill uses for its own illustrative example; both shapes carry the same deny list. The four
read-only tools are worth naming explicitly rather than leaving to an implicit wildcard: a
live run without them on the allowlist still completed the triage, but by falling back to
raw `Read`/`Bash` reads of the committed store JSON instead of the MCP tools built for it.

`create-pull-request` stages only `.sidegraph/` (`add-paths`), commits it to a **new**
branch, and opens a PR — it never touches the default branch directly, satisfying hard rule
2 (this is what the job's `contents: write`/`pull-requests: write` permissions above are
for — GitHub's default token can't push a branch or open a PR without them). Merging that PR
lands the sync-healed bindings and any `propose_decisions` output (still `status=proposed`)
in the store; a human still runs `ratify`/`sidegraph-ratify` afterward for anything the
triage proposed to actually take effect — under the `manual` policy this recipe pins.
Nothing this job wrote is live memory until both of those human steps happen.

## Troubleshooting

- **Archive-duplicate false alarm.** `sidegraph-verify` flags a duplicate ULID across two
  archive segments only when their payloads actually *differ*. Two segments carrying
  byte-**identical** copies of the same record (an independent `sidegraph-compact` run on
  each of two branches, later merged) are the store's own sanctioned shape and never flagged
  — see [`reference/store-format.md#archive-segments-sidegraph-compact`](../reference/store-format.md#archive-segments-sidegraph-compact).
  If Recipe 2 does flag a `duplicate-ulid`, the segments differ in content, which is a real
  problem, not this exemption misfiring.
- **Exit codes, all three CLIs used above:** `0` clean (or, for `sidegraph-sync`, a
  version-skip), `1` an *operational* error (unreadable graph/store, an unresolvable
  `--against` ref, no git repository), `2` findings/violations actually present. Recipes 1
  and 2 fail the job on either `1` or `2` (`continue-on-error` + a manual `exit 1` at the
  end) but classify which one first, since an operational error usually means the runner's
  environment is broken (wrong `SIDEGRAPH_DIR`, `graphify update .` never ran, a shallow
  checkout with no merge base) rather than a real integrity problem in the store.
- **`sidegraph-sync` writes locally — that's expected, not a bug.** In an ephemeral CI
  checkout, re-pointed bindings and a moved entity's refreshed `descriptor` are real writes
  to tracked files under `.sidegraph/` (`index.db` itself stays gitignored and never
  appears). Recipe 1 never commits them; if you want those routine, non-controversial
  rebind fixes to land on the default branch, commit and push `.sidegraph/` yourself after
  a local `sidegraph-sync` run, the same way you'd commit any other change — CI's job here
  is to *detect* drift, not to fix it silently.
- **`fetch-depth: 0` missing → merge-base fails.** `actions/checkout`'s default
  (`fetch-depth: 1`) only fetches the tip commit, so `git merge-base` in Recipe 2 has
  nothing to search and errors out (an operational error, exit `1`, not a false-clean
  result) — the workflow above sets `fetch-depth: 0` for exactly this reason.
- **"Node.js 20 actions are deprecated" warning.** `actions/checkout@v4`,
  `actions/upload-artifact@v4`, `astral-sh/setup-uv@v5`, and `create-pull-request@v7` all
  still target the Node 20 runtime; GitHub's runners now warn on every step that uses one
  (and transparently run it under Node 24 anyway). This is a platform-wide deprecation
  cycle, not a problem with these recipes — the warning is harmless and safe to ignore.
  Bump each action to its next major once that major ships a node24 target; don't pin an
  untested newer major in these recipes preemptively.
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
