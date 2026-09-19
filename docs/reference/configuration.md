# Configuration reference

Sidegraph has thirteen environment variables. Everything else that varies (retrieval budgets) is
a per-call parameter, not an environment/config setting.

## Environment variables

| Variable | Default | Read by |
|---|---|---|
| `SIDEGRAPH_DIR` | unset | The primary store knob. `server.py`, `host/hooks.py`, every `cli.py` subcommand, and `sidegraph-bootstrap` resolve the store path through the shared `config.resolve_store_path` (see [path resolution](#store-path-resolution) below); when `SIDEGRAPH_DIR` is set, it wins over everything except an explicit `--db`. |
| `SIDEGRAPH_DB` | — | **Deprecated.** Kept for back-compat only — honored when `SIDEGRAPH_DIR` is unset; see [SIDEGRAPH_DB (deprecated)](#sidegraph_db-deprecated) below for the exact dispatch rules. |
| `SIDEGRAPH_GRAPH` | `graphify-out/graph.json` | `server.py` (`_load_reader`), `cli.py` (`sync_main`'s, `import_main`'s, `init_main`'s, and `sidegraph-domains bootstrap`'s `--graph` default), `bootstrap/cli.py` (`sidegraph-bootstrap`'s `--graph` default), and `host/hooks.py` (`session_start` only — it builds a `GraphifyReader` from this path; `stop` and `pre_tool_use` do not read it, neither ever touches the graph) |
| `SIDEGRAPH_GREP_NUDGE` | unset | `host/hooks.py` (`pre_tool_use` only). Set to `off` to disable the `PreToolUse` Read/Grep redirect nudge entirely — no other value has any effect. See [`reference/hooks.md`](hooks.md#sidegraph-pre-tool-use). |
| `SIDEGRAPH_CAPTURE_NUDGE` | unset | `host/hooks.py` (`stop` only). Set to `off` to disable the `Stop` block-to-distill nudge entirely — no other value has any effect. See [`reference/hooks.md`](hooks.md#sidegraph-stop). |
| `SIDEGRAPH_RATIFY_NUDGE` | unset | `host/hooks.py` (`session_start` only). Set to `off` to disable the pending-ratification line entirely — no other value has any effect. See [`reference/hooks.md`](hooks.md#sidegraph-session-start). |
| `SIDEGRAPH_DRIFT_NUDGE` | unset | `host/hooks.py` (`session_start` and `stop`). Set to `off` to suppress the two drift PROSE surfaces — the SessionStart "N record(s) are anchored to code that changed after their capture" line and the Stop-nudge drift clause. **The code-drift cache refresh still runs either way** (it is what keeps the `[drifted]` retrieval markers fresh — gating it would freeze markers at their last pre-`off` state), and the `[drifted]` tags in `get_task_context`/`query_decisions`/`drill_down` output are retrieval content, not gated by this switch. No other value has any effect. See [`reference/hooks.md`](hooks.md#sidegraph-session-start). |
| `SIDEGRAPH_AUTO_ACCEPT` | unset | `server.py` (`propose_decisions`'s tool shell, read at point of use via `_auto_accept()`). Set to `on` to land agent-proposed decisions and facts as `status="accepted"` directly, bypassing the ratification queue — no other value has any effect, and `propose_domains` is never affected by this flag (domains always land `proposed` under it; only `SIDEGRAPH_RATIFY_POLICY=auto-all` can ratify an eligible domain, with a stamp). Detectable after the fact: `sidegraph-doctor`'s `unratified-accept` finding reports accepted agent-sourced records carrying no ratifier stamp, so a shared store can assert the gate in CI rather than trust it. See [`guides/capturing-decisions.md#4-auto-accept-opt-in`](../guides/capturing-decisions.md#4-auto-accept-opt-in). |
| `SIDEGRAPH_RATIFY_POLICY` | unset (`"manual"`) | `server.py` (`_ratify_policy()`, read at point of use inside the `propose_decisions` and `propose_domains` tool shells) and `cli.py` (each of the doc-import, rationale-import, and domain-bootstrap commands, read once immediately before its batch call). Parsed by the pure `capture.parse_ratify_policy`, which never reads the environment itself — every reader above resolves the raw value and threads the parsed policy down as a keyword, so one batch samples a single value throughout. `manual` (default) introduces no new transition and leaves every existing write path's semantics exactly as they are — it does not mean every write lands `proposed`; some paths already land `accepted` under their own pre-existing flags (e.g. `importer.py`'s `propose=False`), unaffected by this knob. `auto-low-risk` lets eligible `lesson`/`gotcha` decisions and standalone facts self-ratify at write time (never `adr`/`constraint` decisions or domains). `auto-all` additionally admits `adr`/`constraint` decisions and domains. Eligibility is deterministic and conjunctive: the policy admits the shape (under `auto-low-risk` a draft that `supersedes` another is never eligible); the record has at least one live Tier-1/Tier-2 anchor binding (a domain instead needs a graph reader, a clean `path_prefixes` lint, and a resolving seed anchor or non-empty prefixes); the write was clean and not a dry run; provenance is present. An attached fact never ratifies itself — it rides its decision's cascade, and one ineligible attached fact keeps the decision proposed. Scope: `propose_decisions` (decisions and standalone facts), `propose_domains` (`auto-all` only), `sidegraph-import` rationale mode only with `--propose`, `sidegraph-import --docs` only for `--propose` writes that land as a new `proposed` record (status-derived drafts, `superseded` re-imports and `status: rejected` documents never auto-ratify), and `sidegraph-domains bootstrap` (`auto-all` only; a candidate whose derived `path_prefixes` is empty stays proposed); `--dry-run` never transitions, and `add_domain`, `sidegraph-domains add`, `supersede_domain` successors and `sidegraph-bootstrap` are never affected. Under either auto policy a superseding proposal's predecessor stays open until the successor is ratified — automatically, or later by a human. Outcomes are always reported: `ratified_by`/`auto_ratify_error` on each propose result; under a non-`manual` policy the three CLI batch summaries append `, auto-ratified N` and print each failure on stderr as `auto-ratify failure: <id>: <reason>`; `sidegraph-doctor` adds two informational lines (see [`cli.md`](cli.md#sidegraph-doctor)). Matching is exact after trimming surrounding whitespace — `AUTO-ALL` falls back to `manual`. `sidegraph-init` never writes this value silently: in an interactive terminal it asks one question, defaulting to `auto-low-risk` on a bare Enter, and writes whichever answer the person gave (`auto-low-risk` or `manual`) into the project's `.claude/settings.json` `env` block explicitly; outside a terminal (CI, a script, an agent-driven session) it asks nothing and writes nothing, printing the line to add by hand instead; either way it never overwrites an existing value there. `--ratify-policy VALUE` sets it with no prompt, and `--no-settings` skips the whole step (see [`cli.md`](cli.md#sidegraph-init)). `manual` remains the library's own default when the variable is unset with no committed override, and is also what a freshly initialized project gets unless a person answers yes to the prompt (or passes `--ratify-policy auto-low-risk`/`auto-all`); `adr`/`constraint` decisions and domains still wait for a human under both `manual` and `auto-low-risk`. For a committed, CI-checkable setting, pin it in the repository's `.claude/settings.json` `env` block. Any unknown or empty value falls back to `manual` — fail-safe, never fail-open, the `SIDEGRAPH_PROPOSAL_WINDOW_DAYS` precedent. Auto-ratified records are stamped `ratified_by="auto:<policy>"`, distinguishing them from human review. When both knobs are set, legacy `SIDEGRAPH_AUTO_ACCEPT=on` wins over this policy — its direct-`ACCEPTED`, stamp-less write lands before any auto-ratify block runs, and `sidegraph-doctor`'s `unratified-accept` finding keeps flagging it regardless. |
| `SIDEGRAPH_UNRATIFIED` | unset (`"on"`) | `retrieval.py` (`proposal_surfaces`, read at point of use — the single proposal-surfacing policy every render surface funnels through). Set to `off` for **regulated mode**: `proposed` records never surface as content — not in `get_task_context`, drill-down, the SessionStart TOC's unratified block, or the PreToolUse nudge titles — regardless of age. The SessionStart pending-ratification **counter stays** (it is queue metadata, not record content). No other value has any effect. For a committed, CI-checkable setting, pin it in the repository's `.claude/settings.json` `env` block. See [`hooks.md`](hooks.md#sidegraph-session-start). |
| `SIDEGRAPH_PROPOSAL_WINDOW_DAYS` | unset (`30`) | `retrieval.py` (`proposal_surfaces`, read at point of use). The proposal **surfacing window**: a `proposed` record older than N days (by `valid_from`) stops surfacing as content while remaining in the store, in `sidegraph-ratify`, in the queue counter, and fully ratifiable later — expiry is derived at read time, nothing is written. `0` disables the window (pre-2026-08-04 behavior). A malformed value falls back to the default 30 — fail-safe, never fail-open. Matches `sidegraph-doctor --stale-days`'s default. |
| `SIDEGRAPH_TELEMETRY` | unset (`"on"`) | `server.py` (every retrieval-facing tool impl, read at point of use via `_telemetry_enabled()`) and `host/hooks.py` (`pre_tool_use`'s `_record_touch_event`, and `session_start`'s prune call, via the shared `config.telemetry_enabled()`). Set to `off` to stop recording retrieval telemetry (`index.db`'s `retrieval_shows`/`retrieval_seeds` — see [store-format.md#retrieval-telemetry](store-format.md#retrieval-telemetry)) entirely — no other value has any effect, and the retrieval itself is unaffected either way. Feeds `sidegraph-doctor`'s `never-surfaced` check; see [`reference/cli.md`](cli.md#sidegraph-doctor). A showing made while it is off is in no counter, so `sidegraph-stats` counts that record under `no recorded showing`; see [`reference/cli.md`](cli.md#sidegraph-stats). Also silences touch-event recording in the PreToolUse hook. The flag removes the recording, not the hook invocation itself. Retention is **not** affected: the 30-day prune of `retrieval_events` runs at `SessionStart` regardless of this flag, so opting out strictly reduces what is retained — it stops new rows arriving while existing ones keep ageing out. (Until 2026-08-04 the same check gated both, which meant opting out froze retention; a practitioner review caught that inversion.) |
| `SIDEGRAPH_TRUST_DIRTY_TREE` | unset | `sync.py` (`_trust_dirty_tree()`, read at point of use inside `rebind_entity`'s "moved" rung). Set to `on` to make the moved rung trust WORKING-TREE evidence alone again (unique same-suffix name-only hit + old path gone from disk) instead of also requiring that same move to be confirmed by committed `HEAD` history — the dirty-tree guard's off-by-default escape hatch, for someone who has verified their own tree and doesn't want to wait for a commit before syncing. No other value has any effect. See [`guides/surviving-refactors.md`](../guides/surviving-refactors.md#why-moved-checks-the-disk). |

Bootstrap adds no environment variable. Its repository, profile, host, explicit source,
candidate, task, report, and recovery choices are command flags; see
[`sidegraph-bootstrap`](cli.md#sidegraph-bootstrap). The optional Markdown report is local,
contains aggregate onboarding facts rather than source content, and is never uploaded.
`SIDEGRAPH_TELEMETRY=off` remains the single opt-out for local production retrieval and
touch diagnostics during the later agent-session maintenance loop.

`SIDEGRAPH_GRAPH` is read once at the point of use (`os.environ.get(NAME, default)`).
`SIDEGRAPH_GREP_NUDGE`, `SIDEGRAPH_CAPTURE_NUDGE`, and `SIDEGRAPH_RATIFY_NUDGE` disable only
on the literal string `"off"`. `SIDEGRAPH_TELEMETRY` is more forgiving: it trims whitespace
and compares case-insensitively, so `OFF` also disables recording.
`SIDEGRAPH_AUTO_ACCEPT` is the inverse convention — off unless it's exactly `"on"` — since an
opt-in that removes a safety gate should default closed, not open. `SIDEGRAPH_RATIFY_POLICY` is
neither: a three-value knob matched exactly (after trimming), failing safe to `manual`.

## Store path resolution

`SIDEGRAPH_DIR` and `SIDEGRAPH_DB` both feed one shared resolver
(`config.resolve_store_path`, [`src/sidegraph/config.py`](../../src/sidegraph/config.py)) —
`server.py`, `host/hooks.py`'s three hooks, and every `cli.py` subcommand (`sidegraph-ratify`,
`sidegraph-sync`, `sidegraph-import`, `sidegraph-domains`, `sidegraph-compact`, and
`sidegraph-init`) all call it, so they agree on precedence exactly:

```
explicit --db  >  $SIDEGRAPH_DIR  >  $SIDEGRAPH_DB (deprecated)  >  existing .sidegraph/  >  default .sidegraph
```

An explicit `--db` flag wins outright — nothing below it is even consulted. Absent that,
`$SIDEGRAPH_DIR` (when set to a non-empty value) wins next. Only when *both* are absent does
`$SIDEGRAPH_DB` come into play at all; when nothing resolves, an existing `.sidegraph/`
directory is preferred over creating a new one, and a brand-new `.sidegraph` is the final
fallback (with a one-line stderr warning that a new, empty store is being created — unless the
caller is `sidegraph-init`, which announces the same fact on stdout instead).

### `SIDEGRAPH_DB` (deprecated)

Your old `SIDEGRAPH_DB=.sidegraph/decisions.db` config keeps working — it resolves to
`.sidegraph/`. The exact dispatch rule, applied only when `SIDEGRAPH_DIR` is unset (this is
also the moment a one-line deprecation notice prints to stderr, at most once per process):

- **An existing legacy `*.db` FILE** (e.g. a pre-0.4.0 `decisions.db`) → passed through
  unchanged; `Store` migrates it to the canonical directory layout on open (see
  [`reference/store-format.md`](store-format.md#migration-to-040-from-02x-and-03x)).
- **An existing DIRECTORY** (canonical layout or not) → used directly.
- **A nonexistent path that looks like a file sitting inside a directory** (e.g. the classic
  `SIDEGRAPH_DB=.sidegraph/decisions.db` from before this wave) → rescued to its **parent**
  directory, so this old snippet keeps pointing at `.sidegraph/` rather than trying to create a
  fresh canonical layout literally named `decisions.db`.
- **A nonexistent bare filename with no directory component** (e.g. the historic default
  `SIDEGRAPH_DB=sidegraph.db`) → falls through to the default `.sidegraph` instead of being
  "rescued" to the current directory, which would make the entire repo root the store.

`sidegraph-init`'s `--db` resolves through the exact same precedence as every other command now
(it used to default unconditionally to `.sidegraph/decisions.db`) — see
[`reference/cli.md`](cli.md#sidegraph-init).

## Path-resolution semantics

Both defaults, and any value read from the env vars, are relative paths resolved against the
process's **current working directory at the moment it starts** — plain `pathlib.Path`
behavior, nothing Sidegraph-specific. There is no repo-root detection or upward search (the
Claude Code hooks are the one exception: they anchor a relative result to
`$CLAUDE_PROJECT_DIR` when it's set — see `resolve_store_path`'s `root` parameter).

This matters because the MCP server and the three hooks are typically launched as subprocesses
by Claude Code, and different launch mechanisms handle cwd differently:

- **`uv run --project /path/to/sidegraph <script>`** runs the script using the Sidegraph
  project's dependencies/venv, but does **not** change the process's working directory — the
  relative `SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` paths still resolve against wherever the command
  was invoked from (normally the corpus/project root Claude Code was started in). This is the
  form to use.
- **`uv run --directory /path/to/sidegraph <script>`** does change the cwd to that directory
  first — this silently breaks relative `SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` paths meant to point
  at the corpus repo, since they'd now resolve inside the Sidegraph checkout instead. Avoid
  it for this reason; always prefer `--project`.

If a hook or MCP server ever appears to be reading/writing an unexpected `.sidegraph/` or
missing an expected `graphify-out/graph.json`, check the launching command's cwd handling
first — this is the most common cause. When in doubt, set both env vars to absolute paths.

## Retrieval budget defaults

`retrieval.RetrievalBudget` (a plain dataclass, not env-configured):

```python
structure_chars: int = 4000    # cap on the "## Structural map" block
memory_chars: int = 6000       # cap on mistakes + decisions + related, combined
```

These are **not** environment variables — there is no `SIDEGRAPH_STRUCTURE_BUDGET` or
similar. The only way to override them is per call, via the `get_task_context` MCP tool's
`structure_budget`/`memory_budget` parameters (same defaults, 4000/6000), which construct the
`RetrievalBudget` for that one call. `sidegraph-session-start`'s `top_tier_map()` render has no
budget parameter at all — it caps global mistakes to the 10 most recent instead of a
character budget.

`memory_chars` was raised 2000 -> 6000 alongside the tiered decision-line render (see
[retrieval: tiered rendering](../concepts/retrieval.md#tiered-rendering-direct-entries-get-depth-related-entries-stay-a-pointer)):
a decision bound directly to a task's own seed now renders `context`/`choice` up to ~1200
characters each and `rejected`/`consequences` up to ~400 each, and a single real-world,
ADR-scale decision at those caps can run ~2400-2900 characters on its own — comfortably more
than the old 2000-char total, which was dropping that decision whole rather than showing it.

Budgets are char-based (`len(text)` against `*_chars`), not token-based — no tokenizer
dependency; a rough rule of thumb used elsewhere in the project is ~4 characters per token.

## Graph version semantics

`GraphifyReader.graph_version()` (in `engine/reader.py`) is what `sync` gates on and what
gets stamped into `Decision.provenance.graph_version`:

- If `graph.json` has a `built_at_commit` field (Graphify stamps this when the corpus is a
  git repo), the version is `f"{built_at_commit}:{sha256(text)[:12]}"` — the commit with a
  12-character hex content-hash suffix always folded in, computed at load time.
- If `built_at_commit` is absent or falsy, it falls back to `f"content:{sha256(text)[:12]}"`
  — the same 12-character hash, prefixed `content:` instead of a commit.

The content hash is folded into *both* forms, not just the fallback: Graphify rewrites
`graph.json` without bumping `built_at_commit` whenever the working tree is dirty (e.g. an
uncommitted rename triggers a rebuild), so a bare commit would make sync report "up to date"
while serving stale anchors. Folding in the hash means any byte-level change to `graph.json`
trips the version-change gate on every rebuild, git repo or not.

Upgrade note: a store written before this change stamped `last_synced_graph_version` in the
old bare-commit format, so the first sync after upgrading always sees a differing version
string and reruns once — self-healing, and intended, not a bug.

Either form is an opaque string as far as the rest of Sidegraph is concerned — nothing
parses or compares it beyond string equality (`to_version == from_version`).

`graph.json` serialization is not byte-deterministic across rebuilds (e.g. NetworkX
node/link ordering can vary run to run with no content change), so the content-hash suffix
can occasionally change even when nothing meaningful did. That costs one harmless extra
sync pass — it self-heals on the next read, same as any other version bump.

## Domain sync and the TOC cache

`store.meta` (the local `index.db` key/value table `schema_version` and
`last_synced_graph_version` live in — see
[store format](store-format.md#committed-vs-derived); it's derived, never committed) also
carries `toc_cache`: the JSON-serialized `build_toc()` output the `SessionStart` hook reads to
render the real, domain-named table of contents (`render_toc`) instead of the legacy
nameless-community fallback (`top_tier_map`) — see
[retrieval: SessionStart TOC](../concepts/retrieval.md#sessionstart-toc). It's written by every
completed `sidegraph-sync` pass; by a *skipped* (graph-unchanged) `sidegraph-sync` pass too,
whenever at least one accepted domain exists (a content-only change never moves
`graph_version`, so the cache would otherwise only heal on the next real graph rebuild); and
immediately by `ratify`/`sidegraph-ratify` whenever that call actually accepted or dropped >= 1
domain. There is no environment variable for it — like the retrieval budgets, it's not
something you configure, only something written and read.

**Only `Domain.communities` is ever refreshed by sync — `Domain.path_prefixes` is not.**
`sidegraph-sync` recomputes each accepted domain's `communities` field from the current graph
(using `path_prefixes` as one of the inputs to that computation, when present — see
[mind model](../concepts/mind-model.md#how-domains-relate-to-engine-communities)), but
`path_prefixes` itself is set once at authoring time (`add_domain`/`sidegraph-domains add`
`--path`, or `propose_domains`'s draft field) and never rewritten afterward by any code path.
If your `path_prefixes` stabilizer rule goes stale (a directory gets renamed, say), sync will
silently stop finding anything under it — it won't drift on its own, but it also won't heal
without a human editing it via a new authoring call (there is no CLI/MCP "update
path_prefixes" operation short of superseding the domain via `supersede_domain` — see the
[naming guide](../guides/naming-your-domains.md#recovering-from-a-mass-drop) for that same
mechanism used toward a different end).
