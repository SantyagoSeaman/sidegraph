---
name: heal-anchors
description: Use when sync reports anchor damage after a refactor or graph rebuild — "orphaned", "possibly stale decisions", "ambiguous", "possibly empty domains", "slug conflicts", "path rule too broad", "domains that FAILED to refresh" — or when a decision stopped surfacing where it used to. Also reachable directly as /sidegraph:heal-anchors. Runs the sync_anchors MCP tool (or sidegraph-sync as a CLI fallback) and routes each finding to its correct heal: restore the name, supersede with fresh anchors, re-scope the domain, drop the loser, or fix-and-retry a failed domain refresh. Sidegraph never guesses a rebind — that judgment is this skill's job.
---

# Heal anchors

Code and docs moved; the sync pass re-resolved every tracked entity and reported —
honestly, never guessing — what it could no longer follow. Sidegraph auto-heals everything
it can prove (`rebound`, `moved`, community re-pointing); everything else is routed to a
human, and this skill is how you walk them through it. CLI invocations below use bare
names; in a repo without a Sidegraph checkout, run them as
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main <name>`.

## Get a current report

Rebuild the graph first — sync against a stale graph is noise:

```bash
graphify update .
```

**MCP-first:** from inside a session, call `sync_anchors(force=True)` — the diagnostic/heal
counterpart to `sidegraph-sync`, run as a tool instead of a shell command. `force=True`
re-runs the rebind pass even when `graph_version` already matches the store's last-synced
stamp (e.g. right after `graphify update .`, or after hand-editing a domain's
`path_prefixes`); omit it to respect the version gate on a routine re-check.

`sync_anchors` returns the report as data — `{"synced", "from_version", "to_version",
"counts", "repointed", "outcomes", "stale_decisions", "empty_domains", "overbroad_domains",
"slug_conflicts", "domains_refreshed", "domain_failures"}` (see
[`docs/reference/mcp-tools.md`](../../../../docs/reference/mcp-tools.md#sync_anchors) for
the full shape). `synced: false` with no `force` means the pass was skipped outright
(`graph_version` unchanged) — every other field is then an *empty* default, not a stale
prior report; re-call with `force=True` to get real data. `outcomes` only ever lists
entities worth a human's attention — never the `unchanged`/`rebound` majority:

- **`moved`** — informational, nothing to do: a unique same-name match in a same-suffix
  file, the anchor followed the code.
- **`ambiguous`** — needs judgment (see finding 3 below).
- **`orphaned`** — needs judgment (see findings 1–2 below); cross-check `stale_decisions`
  for whether this took a whole decision down with it.
- **`error`** — the entity raised during rebind; that one entity's `detail` carries the
  exception text, and one bad entity never aborts the rest of the pass.

**CLI fallback** (outside a session, scripted/CI use, or reviewing as plain text):

```bash
sidegraph-sync           # prints the same rebind report; --force to re-run an unchanged graph
```

(Reads also self-heal lazily — `get_task_context`/`SessionStart` run `maybe_sync()` — so
the store is rarely *behind*; `sync_anchors`/`sidegraph-sync` are how you *see* the report.)

Interpretation of per-entity lines, from the
[rebind ladder](../../../../docs/guides/surviving-refactors.md#the-rebind-ladder):
`unchanged`/`rebound` aren't even listed; **`moved:` is informational** (a unique
same-name match in a same-suffix file — the anchor followed the code; nothing to do);
**`ambiguous:`** and **`orphaned:`** are the ones needing judgment, plus the summary
blocks below.

## Findings → heals

**Batch every question to the human into one message** — each finding with its
recommended heal — rather than one interrupt per finding. Every heal below is either a
store write or a source change: confirm before acting.

1. **`possibly stale decisions (all anchors gone — verify):`** — every leaf anchor of the
   listed decision is orphaned. Verify first (read the decision — `get_entity_history` /
   `retrieve_decisions` — and look at the current code); then one of two heals:
   - **Restore the name** — the symbol/heading was renamed or dropped and the decision is
     still true of the code. Restoring it is a **source change**: propose the edit to the
     human, never do it silently. Then `graphify update . && sidegraph-sync` — the anchor
     heals back to `rebound`/`unchanged` on an exact match.
   - **Supersede** — the code it described is genuinely gone. `supersede_decision(...)`
     with **explicit fresh `anchors` pointing at the successor code**. Do NOT omit
     `anchors` here: omitting inherits the predecessor's bindings verbatim — *including
     their orphaned status* — which reproduces exactly the problem you're healing.
2. **A lone `orphaned: <name> (no match)`** whose decision is NOT in the stale block —
   multi-anchor did its job: another anchor still holds the decision live. No action
   needed unless the orphaned one was the load-bearing anchor.
3. **`ambiguous: <name> (...)`** — the name now matches several nodes; the leaf is
   `degraded`, not lost (and community re-pointing still works when all candidates share
   one community). If the decision should point at one specific node, supersede it with a
   precise `{"name", "file_path"}` anchor; if it's genuinely about all of them, leaving it
   degraded is honest and fine.
4. **`possibly empty domains — re-scope or supersede:`** — an accepted domain resolved to
   zero communities and its paths match nothing. Either re-scope:
   `supersede_domain(old_slug_or_id=..., new_slug=..., new_title=..., new_summary=...,
   seed_anchors=[...], path_prefixes=[...])` — **nothing is inherited**; read the old
   rule back first (`list_domains()` returns `path_prefixes` verbatim; for the actual
   `seed_anchors` list read `.sidegraph/domains/<domain_id>.json`) and carry what's still
   right — then `ratify(accept=[<new_id>])`. Or retire it: `ratify(drop=[<domain_id>])`
   if the area is genuinely gone.
5. **`path rule too broad — path contribution dropped (...):`** — a `path_prefixes` rule
   would claim > 20% of all communities, so sync dropped the path's contribution
   (`seed_anchors`, if any, still applied). Heal by narrowing: `supersede_domain` with
   tighter prefixes (carry the `seed_anchors`!), or split the area into finer sub-domains
   — see `sidegraph:manage-domains` and the
   [naming guardrails](../../../../docs/guides/naming-your-domains.md#guardrails-against-an-over-broad-path-rule).
6. **`slug conflicts — drop one (sidegraph-ratify --drop <loser-id>):`** — two live
   domains hold one slug (a cross-branch merge race). The human picks the loser;
   `ratify(drop=[<loser-id>])` — domain drop legitimately works on an *accepted* domain.
7. **`domains that FAILED to refresh (fix, then re-run with --force):`** — an accepted
   domain's community refresh itself raised (a malformed `seed_anchors` descriptor, the
   domain vanishing mid-pass, a lock-contended write). The domain's `communities` mapping
   is left exactly as it was, not zeroed, and isolated — one broken domain never costs any
   other domain its heal. This is **not** a per-pass retry: a completed pass clears the
   volatile-reload gate regardless of whether this domain's own refresh succeeded, so an
   unfixed cause silently stops being reported on the next ordinary sync — that means
   nothing healed, not that it's fine. Fix the named cause (correct the descriptor, resolve
   the concurrent-supersede race), then re-run with `--force`/`sync_anchors(force=True)` to
   confirm it actually cleared.

## Triage decision tree (add_anchors, propose_decisions, or recommend a drop)

For every `orphaned`/`ambiguous` entity and every `stale_decisions` entry, walk this tree
before touching anything — the same tree whether you're triaging interactively or running
headlessly in CI (see below). **Batch every question to the human into one message**, as
above; nothing here changes that.

**(a) Code moved, decision still valid.** The anchor broke but the thing it's about is
still true of the code — it just lives somewhere (or under a name) `sync_anchors`'s
deterministic ladder couldn't follow on its own. Locate the new home first —
`find_entity(name, file_path=...)` (exact match, falling back to a name-only scan) or
`query_structure(files=[...])`/`query_structure(entities=[...])` (the structural-map half
of `get_task_context`, useful when you only know roughly where the code lives now) — then
heal in place:

```python
add_anchors(record_id="<decision or fact id>",
            anchors=[{"name": "<new name>", "file_path": "<new file>"}])
```

`add_anchors` is **bindings-only** — it never rewrites the decision/fact's own record file
— so it's the *preferred* heal for "just moved," ahead of the older workaround (a
content-free `supersede_decision`/`supersede_fact` carrying fresh anchors), which pollutes
history with a successor that says nothing new about the actual decision.

**(b) The decision's content is actually outdated.** The code changed in a way that makes
the recorded `choice`/`consequences` wrong now — not just relocated. Propose a superseding
record; never write one directly:

```python
propose_decisions(drafts=[{
    "title": "...", "kind": "adr", "context": "...", "choice": "...",
    "supersedes": "<old decision id>",
    "anchors": [{"name": "...", "file_path": "..."}],
}])
```

This lands the successor `status=proposed` — a human still reviews and ratifies its
*content*. Note: under the default `manual` policy `store.add_decision` closes the
*predecessor's own record* (`accepted` → `superseded`) in the very same call (under an auto
policy it stays open until the successor is ratified — automatically, or later by a human) —
that's the ordinary append-only
supersede mechanic every write path shares, not a ratification; only the successor's new text
is gated on a human's `ratify`. Never call `supersede_decision`/`supersede_fact` (the direct,
immediately-`accepted` MCP tools) from an unattended/CI context — unlike `propose_decisions`,
they skip the ratify gate entirely.

**(c) The subject is genuinely gone.** The code (or doc section) the decision was about was
deleted outright, not renamed or moved. There is no tool call for this: recommend a drop in
your summary (`ratify(drop=[<id>])`, or `sidegraph-ratify --drop <id>`, for a *human* to
run) and stop there. **Never drop a decision yourself.** Dropping is append-only-safe, but
it is a ratify-gate action exactly like accepting one — that judgment call belongs to a
human, same as every other write this skill routes to a human instead of performing.

## Running this playbook headlessly (CI)

Everything above except the drop in (c) is a tool call, not a judgment a human has to make
in the moment — `sync_anchors`/`find_entity`/`query_structure`/`add_anchors`/
`propose_decisions` all run fine unattended, and so do the read-only verification tools
(`retrieve_decisions`/`get_entity_history`/`list_facts`/`verify_store`) used to confirm a
stale decision's content before healing it. A scheduled job can run the
triage end to end and hand a human the result to review. Pin
`"SIDEGRAPH_RATIFY_POLICY": "manual"` in `mcp-config.json`'s `env` block (same recipe as
[`ci-cd-maintenance.md`](../../../../docs/guides/ci-cd-maintenance.md)) — an auto-ratification
policy would ratify through `propose_decisions` with no `ratify` call at all, which the
`--disallowedTools` deny list below cannot see, let alone block:

```bash
claude -p 'Run the heal-anchors playbook: call sync_anchors(force=True), then for every
orphaned/ambiguous entity and every stale decision, follow the triage tree — add_anchors
for code that moved, propose_decisions(supersedes=...) for content that is genuinely
outdated, and a plain-text recommendation (never a tool call) for anything to drop. Do not
call ratify. Summarize every action taken and every recommendation at the end.' \
  --model claude-sonnet-5 \
  --mcp-config mcp-config.json --strict-mcp-config \
  --allowedTools "mcp__sidegraph__*" \
  --disallowedTools "mcp__sidegraph__ratify,mcp__sidegraph__ratify_decisions,mcp__sidegraph__add_decision,mcp__sidegraph__supersede_decision,mcp__sidegraph__supersede_fact,mcp__sidegraph__supersede_domain,mcp__sidegraph__add_domain,mcp__sidegraph__add_fact" \
  --output-format stream-json --verbose
```

**No `--permission-mode bypassPermissions` here, deliberately.** Bypass skips Claude Code's
permission system outright — `--allowedTools`/`--disallowedTools` are allow/deny lists that
system itself consults, so under bypass they'd bind nothing at all, and the run would be
able to call every tool including `ratify`. **The `--disallowedTools` deny list is the
load-bearing guard**: it names the exact write-gate tools this playbook must never call
unattended — `ratify`/`ratify_decisions`, `add_decision`/`supersede_decision`,
`add_fact`/`supersede_fact`, `add_domain`/`supersede_domain` — and blocks them regardless
of the machine's ambient permission configuration. The `--allowedTools` wildcard is scoping,
not enforcement: on a pristine machine a headless `-p` run auto-denies *MCP* tool calls
outside it (no terminal to prompt), but a permissive user- or project-level `settings.json`
silently overrides allowlist-only narrowing (measured live — a narrowed allowlist alone did
not stop a ratify call on a permissively-configured machine; the deny list did). Always ship
both; the enforcement never rests on the prompt instruction or the allowlist alone.

**Read-only built-in tools stay reachable — that's expected, not a hole.** `Read` and a
plain read-only `Bash` invocation are auto-permitted in headless runs independently of
`--allowedTools`/`--disallowedTools`, which here name only `mcp__sidegraph__*` tools (measured
live: a triage run's transcript shows several `Bash` reads and a `Read` running, plus one
`Bash` call the same static command classification denied — its shape didn't read as safely
read-only). The flags' guarantee is narrower than "no Bash/Edit/Write reachable" — a claim
that overstates what they do: the run cannot **write** anything except through the
allowlisted MCP tools, and the deny list blocks every write-gate tool outright, but the repo
stays readable to the run's own judgment. This is also why the `--allowedTools` wildcard
above should still be trusted to cover the read-only decision tools (`retrieve_decisions`,
`get_entity_history`, `list_facts`, `verify_store`) rather than narrowed past them: a live
run whose allowlist omitted them still completed the triage, but by falling back to raw
`Read`/`Bash` reads of the committed store JSON instead of the MCP tools built for that job
— Recipe 3 in the CI guide linked below now names them explicitly for exactly this reason.

See [`docs/guides/ci-cd-maintenance.md`](../../../../docs/guides/ci-cd-maintenance.md) for the
full scheduled-workflow recipe (Recipe 3; same flag shape, narrowed further to the exact
triage tools) and the two hard rules it states plainly: **CI never ratifies; CI never
auto-pushes canonical to a branch nobody reviewed.**

## The never-guess promise (why this skill exists)

Sync never silently re-anchors a decision to a different entity that merely looks
plausible — `moved` fires only on a *unique* same-name, same-file-type match; everything
else degrades honestly (`ambiguous`/`orphaned`) and waits for judgment. A false rebind is
worse than an honest orphan: it would surface a decision against the wrong code with no
way to tell. So don't "fix" a report by guessing either — verify against the actual code,
then heal.

## See also

- [`docs/guides/surviving-refactors.md`](../../../../docs/guides/surviving-refactors.md) —
  the full ladder, community re-pointing, and both healing paths in prose.
- [`docs/reference/cli.md`](../../../../docs/reference/cli.md#sidegraph-sync) — every
  report line's exact shape and exit codes.
- [`docs/reference/mcp-tools.md`](../../../../docs/reference/mcp-tools.md#add_anchors) —
  `add_anchors`'s full parameter/return shape.
- [`docs/guides/ci-cd-maintenance.md`](../../../../docs/guides/ci-cd-maintenance.md) —
  running this playbook as a scheduled GitHub Action, proposals-only.
- `sidegraph:manage-domains` — domain re-scoping beyond a single heal.
