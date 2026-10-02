# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) (pre-1.0: minor bumps may break interfaces). Which
interfaces, exactly, and what each one promises: [`docs/reference/stability.md`](docs/reference/stability.md).

## [Unreleased]

## [0.7.0] — 2026-10-02

### Changed

- **The hooks and the MCP server look a missing relative store up inside the repository.** The
  precedence between `--db`, `SIDEGRAPH_DIR`, `SIDEGRAPH_DB` and the default is unchanged, and a
  store that exists where its relative path is anchored still wins. What is new: when a relative
  `SIDEGRAPH_DIR` (or the default `.sidegraph`) does not exist there, the three hooks and the MCP
  server visit the parent directories, nearest first, up to and including the repository root (the
  nearest directory holding a `.git` entry, a directory or a file), and use the first store found.
  The lookup never leaves the repository (a `.git` entry at your home directory or above it is not
  a repository root, so a dotfiles repository does not turn `~/.sidegraph` into every project's
  store) and does not run for `--db`, an absolute `SIDEGRAPH_DIR`, `SIDEGRAPH_DB`, a value with a
  `..` part, or an anchor that is a symlink. The CLI, `sidegraph-init`
  and `sidegraph-bootstrap` are unchanged: from a subdirectory they still need `--db`. To keep the
  old outcome on a host surface, pass an absolute `SIDEGRAPH_DIR`.

### Fixed

- **A SessionStart hook registered twice on one host no longer injects the map twice.** Two
  copies firing for one session at once both read the old duplicate-check ledger and both emitted;
  the check now decides and stamps inside one write transaction, and a duplicate still answers
  without waiting for a lock. The ledger keeps one row, so a repeat is still let through when
  another session starts in between, or when two sessions with doubled hooks start together.
- **Sync's moved rung adopts only a real move.** When an anchor's file is gone, sync looks the
  name up across the whole graph and follows a unique hit to its new file. That lookup ignores
  case and decoration, and the check behind it (the old path is out of `HEAD`, the new one is in
  it) did not ask whether the new path was there before. A type deleted together with its file
  could therefore be re-anchored onto an unrelated file that held an old member of the same name:
  a deleted `Priority` followed `.priority` into another file, silently rewriting the entity's
  committed `descriptor.file_path`. Two rules now close it. The hit must be the same name once
  the call decoration (`()`, a leading `.`) is stripped from both sides, with case kept. And git
  must show the new path arriving in the change that removed the old one: the mainline commit that
  deleted the old path (a branch merged with `--no-ff` counts as one change) must have a first
  parent without the new path and must hold it itself. With `SIDEGRAPH_TRUST_DIRTY_TREE=on` and the
  old path still in `HEAD`, the new path must be absent from `HEAD`. Every git failure refuses, as
  does a history with no deletion in it, so a move out of a path git never held (a gitignored
  directory) and a move that a shallow clone cannot prove now end `orphaned`, which
  `sidegraph:heal-anchors` repairs, instead of being adopted. An uncommitted delete onto a path
  that is already in `HEAD` is `orphaned` too, not remembered as a move waiting for a commit: no
  commit can make it one. The search matches the old path literally (a path such as
  `app/[id]/view.tsx` is not a glob) and ignores `log.showSignature`.

  Some real moves are refused as well, because git's history alone does not show them as one
  change. Each ends `orphaned` and `sidegraph:heal-anchors` re-anchors it:
  - the new file added in one commit and the old one deleted in another on the first-parent line,
    including a branch merged fast-forward or rebase-merged;
  - a second move before any sync ran (`A` to `B` to `C` while the descriptor still names `A`);
  - an old path that was re-added and then deleted again.

  To check what an earlier release adopted, list the descriptor rewrites:
  `git log -p --diff-filter=M -- .sidegraph/entities/ | grep -E '^(commit |[-+]\s+"file_path")'`.
  A rewrite is a real move only if the new path was added by the commit that deleted the old one
  (`git log --first-parent -m -1 --diff-filter=D -- <old path>` finds that commit; the new path
  must be missing from its first parent and present in it). Re-anchor a wrong one by superseding
  the record with fresh anchors.

- **The `SessionStart` instruction names a call the tool accepts, and a domain's mistake count
  counts what the domain holds.** The standing line told the agent to call
  `get_task_context(seeds)`, but the tool takes `files` and `entities` and rejects anything else,
  so an agent that followed it made a call that failed validation. It now says
  `get_task_context(files=[…])` with repo-relative paths, points at the check-plan skill before a
  non-trivial change when it is available, and says to load the tool first if the host lists it
  only by name. Separately, the per-domain count in the `SessionStart` map read "0 mistake(s)"
  over a domain whose gotchas were anchored to its own code, because it counted only decisions
  tagged to the domain itself. It now counts the accepted `gotcha`, `lesson` and `constraint`
  decisions that `drill_down` serves for the domain, so the two show one number. The map's cache
  records the store's digest, so a retrieval call on an unchanged store no longer rebuilds it, and
  `SessionStart` reads the unratified list without building the whole map. The bindings table gains
  an index by entity (an existing store gets it on its next open), which takes a rebuild of the map
  from about 0.1 s to under 0.03 s on a real store, so the cost of a session start is what it was
  before the count changed, also right after a store change and in a linked worktree. A worktree
  builds its map with the main checkout's graph reader, so it counts the same decisions the main
  checkout does. A map written by a path that has no graph reader (ratify, capture) leaves out
  decisions anchored to a whole document until the next sync.

- **A Claude Code subagent gets its own read nudges, and its touches are told apart.** A
  subagent's hook payload carries its parent's session id plus an `agent_id` of its own, and
  the `PreToolUse` nudge keys were per session: once the main agent had been nudged, every
  subagent it started read anchored files blind. Each agent, the session's own and every
  subagent, now gets one generic and one path-specific nudge. The
  keys are claimed in a single statement, so a subagent's parallel opening reads cannot both
  nudge, and `SessionStart` expires keys older than 30 days. The `retrieval_events` journal
  gains a nullable `agent` column, added to an existing index on its next open, that records
  which subagent made a touch; the row stays under the parent's session, so `sidegraph-stats`
  counts the same sessions as before. Only `agent_id` counts, never `agent_type`: a main
  session started with `claude --agent <name>` is still the session's own agent.

- **An anchor written `Type.member` now resolves to the member.** Graphify labels a method
  `.playClip()` and links it to its type, an agent writes `AudioPlayback.playClip`, and the two
  never met: the anchor was born orphaned, and a seed naming it never reached the record, which
  surfaced only through its community, as an unratified proposal. With a `file_path`, a name
  with no exact match now falls back to a member of the named type in that file. It is for code
  identifiers only (prose, a numbered heading or a hyphenated file name never reads as a
  member). The name is split at the last `.`, `::` or `#` and the member is compared with its
  case kept. A candidate that an edge ties to a different type is refused, and the answer stays
  orphaned, never guessed, when several remain or when the file holds a case-only twin of the
  member (a struct `Message` beside a method `.message()`, which the store treats as one
  anchor). Without a file, and for a path-qualified name (`src/pkg/mod.py`), nothing changes.
  On the read path a seed that names a stored anchor, and every node a seed resolves to, now
  map to their store entities by the engine mapping, in the neighbour walk as well, so such a
  record ranks as a mistake or decision again. The first `sidegraph-sync` after the upgrade
  reruns once on its own (the sync stamp now carries a resolver revision, which a report does
  not show) and heals the orphans this left behind. The skills and guides no longer say
  `Type.member` names never resolve; the bare name with its file stays the preferred form.

- **A hook command that cannot start no longer blocks or loops the host.** On `Stop`, both hosts
  feed a hook's stderr back when the hook exits 2: Codex continues the turn, and Claude Code
  continues the conversation, and Claude Code also blocks the tool call on `PreToolUse`. `uv`
  exits 2 on some of its own errors (a project it cannot find, a cache it cannot write) and `dash`
  exits 2 when the `cd` fails, so a hook command that never reached Python could do exactly that,
  over and over in a headless `codex exec` run. Every shipped hook command, in both plugins and in
  the manual recipes in the docs, now ends with a guard: `Stop` and `PreToolUse` answer `{}`, and
  `SessionStart` answers a one-line `systemMessage` that says the hook could not start and to run
  the hook command in a terminal to see the error. A command that starts Python behaves as
  before. **Codex users: approve the two hooks again once after updating.** Codex records trust
  per hook definition and the definition includes its command, so the changed commands do not run
  until you approve them in `/hooks` or at the next interactive session's trust prompt. **If you
  wired the hooks by hand**, append the guard to your own commands in `.claude/settings.json` or
  `.codex/hooks.json`; the recipes in the docs show it. The Codex page also now says how to run a
  headless review without Sidegraph's hooks.
- **The capture nudge now arms on Codex.** The Stop hook's substance gate understood only Claude
  Code transcripts, so on a Codex rollout it counted zero prompts and never nudged: every Codex
  session ended without the capture reminder. The gate now reads a rollout too. A person's prompt
  is a user message that is not text Codex wrote itself (the project instructions and environment
  blocks, plugin and skill blocks, an aborted-turn note, a Stop hook's own block reason fed back);
  nor does an answer to the agent's question, which arrives mid-turn, count as one. Each prompt
  counts once although Codex mirrors it as an event. A thread arms after two real prompts, never
  on tool calls. Subagent threads, Codex's automatic reviewer and headless `codex exec` runs, including
  ones started through the Codex TypeScript SDK, never arm, so a review panel's `-o` output is
  never replaced by a continuation. The nudge text, the once-per-session ledger and the hook
  definitions are unchanged, so Codex's trust in the installed hooks stays valid. Claude Code
  sessions are counted exactly as before.
- **A session started in a subdirectory no longer opens a second, empty store.** Claude Code sets
  `CLAUDE_PROJECT_DIR` to the directory it was launched in, so a launch in `repo/pkg` with the
  plugin active created `repo/pkg/.sidegraph` beside the repository's own store, and the session
  saw no memory. The hooks and the MCP server now use `repo/.sidegraph`, record touches relative
  to it (`pkg/a.py`, so they join the anchors) and keep the nested-store case (`.config/sidegraph`)
  repo-relative. The hooks also read the graph the store's project holds, as the CLI and the MCP
  server do, so a store in a nested directory finds its own `graphify-out/graph.json`; a relative
  `SIDEGRAPH_GRAPH` is no longer looked up under `$CLAUDE_PROJECT_DIR`. A store the old behaviour
  already created is not touched: when the store a session uses is empty and the repository's
  store above it holds records, `SessionStart` adds one line naming both. Remove the empty
  `<subdirectory>/.sidegraph` to use the repository's. A stray store that holds any record (one
  proposed decision from an old capture is enough) is not reported and keeps hiding the
  repository's: review it with `sidegraph-ratify --db <subdirectory>/.sidegraph`, record what
  matters again in the repository's store, then remove the stray directory.

- **A code graph that never caught up with `HEAD` now says so.** Nothing compared the commit
  `graph.json` was built at (`built_at_commit`) with `HEAD`, so a graph stuck at an old commit
  looked up to date for good, and every `get_task_context` call seeded with a file added since
  returned a bare `No context found.` The comparison now runs in the engine reader and counts a
  graph as stale only when a file it should hold changed or appeared since the build (a commit
  touching only file types the graph does not hold does not, and a graph built from a dirty tree
  and committed afterwards does not). It surfaces in four places:
  - `SessionStart` adds one line when the graph is stale, with the rebuild command
    (`graphify update .` from the repository root);
  - `get_task_context` appends a `## Not in the code graph` block after its answer when a seed is
    not a file the graph holds, saying whether the graph is stale, shows no committed change since
    its build (the file may be newer than the build, or one the engine skips) or could not be
    compared, or whether the path is a directory, missing or not a normalized repo-relative path
    (an absolute path, `./x`). Seeds the graph holds leave the answer byte-identical and cost no git call;
  - `sidegraph-doctor` reports a new advisory finding, `graph-stale`. **`sidegraph-doctor --check`
    now exits `2` on a stale graph**; a CI job that rebuilds the graph before running doctor is
    unaffected;
  - `sidegraph-stats` adds a `stale:` continuation line under the GRAPH counts, and its `--json`
    gains `graph.freshness`, `built_at`, `commits_behind` and `stale_files`.

  The docs no longer call a missed refresh "self-healing": the lazy check re-syncs a graph that
  was rebuilt, and only a rebuild fixes one that never was.

- **A linked worktree reads the main checkout's code graph, and a missing graph is said.**
  `graphify-out/` is gitignored, so a `git worktree add` checkout had the tracked store and no
  graph, and `get_task_context` resolved nothing there. When the store's own graph is missing and
  its repository is a linked worktree of a main checkout, the read tools (`get_task_context`,
  `query_structure`, `query_decisions`, `drill_down`) and `SessionStart` now open the main
  checkout's graph, found from the worktree's `.git` file alone (no git call). That graph is synced
  index-only: a worktree's index starts cold, and without the derived state (domain communities,
  community bindings, engine mappings) `drill_down` lost most of a domain's decisions and
  `get_task_context` its `## Related` section. The sync never rewrites a tracked file (its moved
  rung, the one step that can, abstains), so the worktree's store stays as git checked it out.
  `SessionStart` builds the domain map in memory from the store, adds a line saying whose graph it
  reads, and names the main checkout in its stale-graph line. `get_task_context` tells a file that
  exists only on the branch apart from one the main checkout's graph is stale for. A file the
  branch changed that the main checkout also has is described as it is in the main checkout (its
  symbols and the edges between them), and nothing warns about it.
  `sync_anchors`, `list_domain_candidates`, every write tool and every CLI default keep reading the
  store's own graph. A bare repository (also one cloned into `x/.git`), a git directory that is
  separate and not itself named `.git`, a submodule and a plain `git clone` have no main checkout
  to borrow from.
- **`get_task_context` says when there is no code graph at all.** With no reader and at least one
  seed, the answer ends with a `## No code graph` block naming the path that was looked at and
  how to build it, instead of an answer that looked like a search with no hits. A graph that is
  there and cannot be read is reported as not readable, not as missing.

## [0.6.0] — 2026-10-01

### Added

- **`sidegraph-verify` reports `unsafe-record-id`.** A record file, `bindings/` file or
  archive line whose id is not a single safe path segment is now a finding, and a
  non-string id in a record or archive file no longer crashes the run with `unhashable type`.
- **`sidegraph-verify` reports a symlinked store entry as `symlinked-store-entry`.** One
  violation per store-owned directory or file that is a symlink, so a committed link fails the
  CI gate; `sidegraph-doctor` inherits it.

### Changed

- **A `--graph` typed as a relative path resolves against the shell in `sidegraph-ratify`
  and `sidegraph-stats`.** It used to resolve against the store's project. The default and
  `$SIDEGRAPH_GRAPH` still follow the store. The same holds in every command that reads a
  graph, so the copy-and-sync workflow works with `--graph graphify-out/graph.json` typed from
  the repository.
- **A store in a nested directory needs an absolute `SIDEGRAPH_GRAPH`.** With
  `SIDEGRAPH_DIR=.config/sidegraph` and a relative `SIDEGRAPH_GRAPH`, the CLI now looks for the
  graph under `.config/` (the store's project) instead of using the shell's copy. A command
  that needs the graph fails with a hint; `init`, `ratify`, `doctor` and `stats`, for which it
  is optional, continue and print the hint (`stats` reports its graph metrics as unavailable). Set an absolute `SIDEGRAPH_GRAPH` or pass `--graph`. The MCP server now
  follows the same rule (next entry); the hooks are unaffected.
- **The MCP server reads the graph that belongs to its store.** A relative `SIDEGRAPH_GRAPH`
  (or the default `graphify-out/graph.json`) used to resolve against the server's working
  directory, so a store reached by an absolute `SIDEGRAPH_DIR`, or a store in a nested directory,
  could be synced against one project's graph by the CLI and another's by `sync_anchors` and the
  retrieval tools, and the store's graph version flipped back and forth. It now resolves against
  the store's project, the same rule the CLI uses, and an empty `SIDEGRAPH_GRAPH` counts as unset.
  With the default layout (`.sidegraph` in the project, the server started there) nothing changes.
  A nested store needs an absolute `SIDEGRAPH_GRAPH`; `sync_anchors` reports `graph not readable`
  with the path and names the graph it found beside the working directory, when there is one.
- **A document is imported again once, if its `provenance.ref` changes.** A document
  imported before this release from a subdirectory or by an absolute path is imported
  again under its repository path. The old record stays. A leftover that is still `proposed` can be dropped with
  `sidegraph-ratify --drop <id>`. An `accepted` leftover has no one-step retirement: leave
  it, or supersede it by hand with `supersede_decision`, which writes a new record.
- **`sidegraph-import --path` (and domain `--paths`) match on directory boundaries.**
  `--path payments` no longer imports `payments_v2/b.py`, and a partial-name prefix such as
  `--path docs/adr/00` matches nothing; a prefix matches a file under that directory or
  that exact file. `--path ""` still matches everything, and mixed with a real prefix it is
  ignored. The domain `--paths` help already behaved this way and now says so.
- **The Stop hook skips the transcript of a session that is already captured.** It now peeks
  at the capture ledger read-only before parsing the transcript, so every Stop after the
  capturing one no longer re-reads the whole file.

### Fixed

- **The derived initiative names the store's repository branch.** It used to come from the
  branch of whatever directory the MCP server or CLI ran in. An inherited `GIT_DIR` or
  `GIT_WORK_TREE` no longer redirects either the derived initiative or the captured commit.
- **A timestamp in the future no longer counts as fresh.** The capture session-id fallback and
  the session attribution of retrieval telemetry treated a marker stamped ahead of the clock
  as current; they now ignore it. The SessionStart dedupe still treats a same-session stamp
  less than 60 seconds ahead as a duplicate (a parallel hook can write one), but a stamp
  further ahead no longer suppresses the map until the clock catches up.
- **Every CLI command that takes a store and a `--graph` pairs the store with its own
  project's graph by default.** `sidegraph-sync`, `sidegraph-import` (both modes),
  `sidegraph-domains bootstrap`, `sidegraph-doctor` and `sidegraph-init` (whose found/missing
  line used the shell's path) read the default graph relative to the
  shell, so `--db A/.sidegraph` run from project B stamped A's store with B's graph version
  and could rewrite A's anchors, import decisions, or bootstrap domains from B's graph. The
  default and `$SIDEGRAPH_GRAPH` (an empty value counts as unset) now resolve against the
  store's project, as `sidegraph-ratify` and `sidegraph-stats` already did; a store reached
  through a symlinked `.sidegraph` keeps the link's project. When the store's project has no
  graph but the same path exists beside the shell (a store copied elsewhere), one stderr hint
  names `--graph <path>`; when both exist, a note says which was used. Error lines name the
  resolved path.
- **`SIDEGRAPH_DB` naming a missing path no longer scaffolds its parent.** A missing
  `SIDEGRAPH_DB=/proj/.sidegraph`, or a dangling link, resolved to `/proj` and a store was
  created in the project root. Only a path ending in `.db` is rescued to its parent now; any
  other missing path is used as given, and a dangling link fails to open without creating
  anything.
- **`sidegraph-sync --check` documents domain-refresh failures.** The help and the module
  docstring omitted the finding that already makes `--check` exit 2.
- **A move left `moved_uncommitted` is now re-verified after you commit it, even when the
  graph was not rebuilt.** Committing a rename does not change `graph.json`, so the version
  gate skipped every later sync and the leaf kept its old path until `--force`. Sync now
  remembers such entities with the git `HEAD` it saw, and the first sync after `HEAD` moves
  re-verifies just them (a narrow pass that leaves domain membership alone). The comment that
  promised the next sync would heal it was wrong, and is corrected.
- **A decision with two leaves in one community no longer loses that community's binding when
  one leaf leaves it.** Both leaves share one Tier-1 `community:*` row, and a renumber that
  moved one leaf orphaned the row the other still held, so the decision stopped surfacing for
  that community. Sync now settles a record's community rows after every leaf has been
  rebound, and a new row carries the moved leaf's relation instead of `affects`. Damage from
  earlier syncs is repaired when a later repoint touches the record, or when the index is
  next rebuilt from the committed files.
- **Secret redaction covers quoted values and quoted keys.** `password: "one two"`,
  `` token: `abc def` `` and `password = 'a b c'` were cut at the first space, leaving the rest
  of the secret in the store, and `{"password": "hunter2 two"}` was not matched at all. A
  quoted value is now taken whole, with any text glued to its closing quote, and a quoted
  key matches too. JSON keys such as `"token_count"` now redact like the unquoted
  `token_count:` already did. An escaped quote (`"alpha\" beta"`) does not end a quoted
  value, and a quoted key stops at the next JSON field, so `{"password":"x y","reason":"z"}`
  keeps `reason`.
- **A decision whose attached fact failed is left proposed under an auto-ratify policy,** and
  an auto-accepted fact's write-guard message points to `add_anchors` instead of a drop that
  the store refuses.
- **An initiative name is redacted before it becomes an entity, on every write path.** A
  secret in `initiative`, or in a branch name the propose path derives one from, used to land
  in the canonical `initiative:<name>` entity file. A blank or all-secret initiative now binds
  nothing, and a padded name is trimmed (`"  proj  "` keys as `initiative:proj`).
- **`add_decision` binds its initiative with no graph or no anchors.** The Tier-0 binding was
  made only inside the per-anchor loop, so a call without a reader or without an anchor
  dropped it silently.
- **`add_domain`, `supersede_domain` and `sidegraph-domains add` redact the title and
  summary.** Only `propose_domains` and the bootstrap did. The MCP results now carry
  `redactions`, and the CLI prints a count line when it redacted something.
- **`propose_decisions` dedups a decision on its redacted title.** A title that held a secret
  was compared raw against the stored, redacted one, so proposing it twice wrote it twice.
- **One failing draft no longer aborts a propose batch.** A step after the write (anchors,
  initiative, tags, attached facts, auto-ratify) now leaves the result `written` with a
  `reason`; an error before the write makes that draft `rejected`; each attached fact and each
  domain is isolated the same way, and a failed TOC rebuild after `propose_domains` is
  reported as a `toc:` warning. `KeyboardInterrupt` still propagates.
- **A record id can no longer become a path outside the store.** Every canonical writer built
  `<dir>/<id>.json` from the id as given, so an id like `../../x` wrote outside the store.
  The store now refuses to write an id that is not a single path segment, and the OKF export
  skips such a decision, fact or entity with a warning.
- **A canonical file whose JSON id does not match its filename is skipped on reload, with a
  warning on every open.** A file with a crafted id was indexed under that id, so one later
  ratify could write outside the store. Duplicate ids let the later file silently win, and a
  file with no id got a fresh ULID on every rebuild. Such files are now left out of the index
  and named in a warning until they are fixed or removed. An existing index reloads once to
  apply the rule. An archive line with an unsafe or non-string id is skipped instead of
  making `Store()` fail.
- **An interrupted legacy migration no longer loses its staged work.** The legacy file is
  renamed to its backup only after the staged directories have moved in, so an interruption
  leaves it in place and the next open migrates again. A bare-file migration, which has to
  rename first, keeps its staging directory and leaves a sentinel so the next open says what
  to do. A sentinel younger than 60 seconds means a migration is running now: the open
  refuses with "retry in a minute" and touches nothing, and deleting that sentinel early is
  unsafe. A legacy row with an unsafe id fails the migration before anything is written, and
  a row whose JSON omits its id takes its SQL primary key instead of a fresh one per retry.
- **The bootstrap catalog applies the same identity rule.** A record file with no id was
  given a fresh ULID on every load, so the catalog fingerprint changed between two loads of
  one store. Such files are now skipped with a warning.
- **The store refuses a symlinked directory or store file inside itself.** A symlinked
  `decisions/` sent every record write, from the MCP server, the hooks and bootstrap alike, to
  the link's target, and a symlinked `index.db` made SQLite write there. `Store()` now raises
  before any migration or write, naming the entry, and `sidegraph-bootstrap` refuses before
  the review prompts. A symlinked store root stays allowed. Bootstrap's action summary now
  names the store path and any symlink between the repository root and it.
- **`sidegraph-bootstrap --report` no longer overwrites a file it is hard-linked to, and
  protects the Codex hooks file the verifier really reads.** The guard compared resolved
  paths, so a hard link to a host config passed it and the report truncated the shared file.
  It also protected only the legacy `.codex/hooks/hooks.json`, not `.codex/hooks.json`. The
  report is now published by replace, and one list of host-config paths feeds both the
  verifier and the guard. The guard covers every host's config file, whichever `--host` is
  selected.
- **`sidegraph-import --docs` keys a document inside the repository by its repository path,
  whatever directory it runs from.** Run from a subdirectory or given an absolute path, a
  document used to miss its file node in the graph, skip as unanchorable, and get a
  different `provenance.ref` from the same document imported at the root. Profile discovery
  also finds the profile's documents from any directory in the repository, and the
  unanchorable warning now fires only outside a git work tree, where the current directory
  stands in for the root. Build the graph from the repository root: a graph built from a
  subdirectory leaves documents unanchorable.
- **`--docs` refuses a markdown file that is a symlink to a target outside the repository.**
  It used to read the target under `--any-doc`. The document is counted as `refused: a symlink
  pointing outside the repository`, and so is a profile-discovery hit that resolves outside
  the repository (for example through a symlinked directory). A symlinked directory inside
  the repository that points inside it imports under the path you named, and one that points
  outside the repository is refused. A symlink to a file inside the repository imports under
  its link path.
- **A re-import repairs a record that has no anchors.** A document whose record was written
  but whose bindings never landed (a run that stopped in between, leaving no anchor other
  than `tag:` bindings) used to be skipped as existing for good. It is now bound on the next import and reported as `had their anchors
  repaired`.
- **The `import_docs` docstring describes what it does:** the five compared fields, the
  statuses it matches, the repair and the refusal.
- **`sidegraph-init` no longer writes `.claude/settings.json` through a symlink, and no
  longer prints a traceback when the write fails.** A symlinked `.claude/` or `settings.json`
  (live or dangling) is left untouched and the `env` line to add by hand is printed. A merge
  into an existing file is now published by replacing it atomically, so an interruption
  cannot leave a truncated file, and any write failure (a read-only `.claude/`, for example)
  is reported the same way instead of raising. An `env` block that is `null` or not an
  object, and a settings file that changes while init runs, are skipped the same way, not
  raised, and a link swapped in just before the write is refused. A write-protected file is
  respected when init runs as root too.
- **`sidegraph-doctor`, the drift refresh the session hooks run, and `sidegraph-stats` now
  open the index correctly when the store's path contains `#`, `?`, or `%` followed by two
  hex digits.** Their read-only SQLite URI was built from the unquoted path.
  **Doctor reported its index checks as skipped**, blaming a missing index and suggesting
  `sidegraph-sync`, which could not help. **The session drift refresh silently stopped
  excluding orphaned bindings.** **Stats failed.** For `#` and `?`, an empty file was also
  created at the store path cut short at that character, and an unrelated database there
  could be read instead.
- **The session-link gate checks every line that gets published.** Lines starting with `#` in
  a PR description, and in commit messages made with `git commit -m`/`-F`, which git records,
  are now checked. The PR title and the PR's recorded commit messages are checked too. The
  check re-runs when a PR description is edited. The public repository runs the check too:
  the release snapshot ships the checker, and the workflow fails when the checker is missing
  instead of skipping.
- **`sidegraph-export-okf` refuses a symlinked `--out`, and a failed export no longer
  destroys the previous one.** A symlinked output directory used to be written through, or
  cleared, wherever it pointed, and a write that failed midway left a partial bundle after
  the old export was already deleted. The new bundle is now built beside `--out` and
  swapped in only when complete, except for a mount point, a `--out` that is the current
  directory, an unwritable or non-directory parent, or a previous export that cannot be
  renamed aside, which are written in place as before (a write failure there can leave a
  partial export). The in-place write goes to the validated, resolved directory.
- **An unreadable store or graph directory no longer ends in a traceback.** On Python 3.13 a store or
  graph whose directory could not be read (permission denied) made `sidegraph-sync`,
  `sidegraph-import`, `sidegraph-domains bootstrap`, `sidegraph-init`, `sidegraph-stats` and
  `sidegraph-ratify` (on a domain accept) exit with an uncaught `PermissionError`. An unreadable
  store is now reported on the command's error line (exit 1; 2 for `sidegraph-stats`). An
  unreadable graph is treated as a missing one: `sidegraph-sync`, `sidegraph-import` and
  `sidegraph-domains bootstrap` stop with `graph not readable`, and the other commands carry on
  without a graph.
  A working directory that cannot be read skips the graph hint and the `sync_anchors` hint instead of
  raising; other code that resolves a relative path against it may still raise.
- **A retried Tier-1 reconcile that could not run is no longer reported as done.** A
  remembered community reconcile whose record still has a live or degraded Tier-2 anchor
  with no recorded community kept being treated as a success: the key was cleared, the sync
  said `moved`, and the stale row stayed live. It is now kept, and retried silently on every
  sync.
- **Sync, verify and doctor ask git about the repository their own files are in.**
  `sidegraph-sync` (the graph's directory), `sidegraph-verify --against` and
  `sidegraph-doctor` (the store's, and its graph-root check's) no longer inherit an ambient `GIT_DIR` or
  `GIT_WORK_TREE`, which could make sync read another repository's `HEAD` and adopt a rename
  that was never committed. The lazy sync run by `SessionStart` and retrieval, and the
  `SessionStart` drift cache, are fixed too. `sidegraph-init` and `sidegraph-import` resolve
  the repository root from the current directory the same way. A work tree whose repository
  is found only through `GIT_DIR` now needs a `.git` file containing `gitdir: <path>`, and
  `core.bare=false` in that repository.
- **The sync docs list every condition that prevents a skip.** A remembered Tier-1
  reconcile that is repaired or fails again also makes a plain sync report instead of
  skipping.
- **Documentation and help text match what the code does.** The PreToolUse docs name the
  real `Read|Grep|Edit|Write` matcher and the two nudge forms, each at most once per session.
  The `add_decision`, `supersede_decision`, `add_fact`, `supersede_fact` and
  `propose_decisions` tool descriptions list `anchors_orphaned`. `sidegraph-ratify --help`
  says `--all` also accepts standalone facts, and `--help` for `sidegraph-verify`,
  `-doctor` and `-blame` no longer promises a legacy `*.db` migration they never perform.
  `SECURITY.md` states local retrieval telemetry and the output paths commands write to.

## [0.5.0] — 2026-09-23

### Changed

- **The public `main`, which the plugin manifests install from, moves only at releases.**
  The plugin runs whatever `main` holds, so a snapshot pushed between releases used to reach
  every plugin user at once. The release script now pushes `main` only for a release: it
  refuses unless the version it carries is untagged in the public repository and
  `CHANGELOG.md` dates it.

### Fixed

- **`sidegraph-init` now says to restart the Claude Code session after it writes
  `SIDEGRAPH_RATIFY_POLICY`.** The Sidegraph MCP server reads the policy from the
  environment it started with, so a server started before `sidegraph-init` kept proposing
  under the old policy until the session restarted, and nothing said so.
- **`sidegraph-verify --against` no longer flags a ratification of an already-committed
  proposal, or a supersede/drop that touches a record written before a later schema addition
  (`Provenance.commit`, `Domain.seed_anchors`/`path_prefixes`) existed.** Ratifying a
  decision, fact, or domain stamps `ratified_at`/`ratified_by`, and neither field was on the
  transition layer's mutable allow-list, so the stamp alone was reported as an illegal field
  change. Separately, rewriting an old record that predates a field added to its schema
  serializes that field back in as its default, which a plain top-level or whole-list
  comparison also read as a change. The ratifier stamp may now be set once, from a proposed
  record, landing on any state reached through accepted within the diffed range; an absent
  field now counts as the same value as an explicit `null` or an empty list/object, at every
  nesting depth and inside list items. A field whose default is a non-empty value is not
  covered by this.
- **`sidegraph-import --docs` no longer aborts on a file that is not valid UTF-8.** It
  decoded every document as strict UTF-8 with no handler, so one document saved in
  `cp1251` (or carrying a stray non-UTF-8 byte) raised and stopped the whole run, with
  nothing after it imported and no report printed. The file is now a named skip
  (`skipped_undecodable`) instead: the run continues, and both the real run and
  `--dry-run` print the skipped path(s), last.
- **`sidegraph-import --docs` now reads a UTF-8 document that starts with a byte-order mark
  (BOM).** The BOM used to survive into the parsed text, so the H1 and any frontmatter never
  matched, and a real decision document was silently counted `skipped_not_decision`. It is
  now stripped before parsing. `sidegraph-bootstrap` still reads such a file without
  stripping it.

  Re-importing a BOM document that an earlier version did import can change its record,
  once:
  - On the `openspec` profile, a BOM `proposal.md` had imported under a title built from its
    path. The re-import supersedes it with the real H1 title.
  - Frontmatter the BOM hid is now honoured. A document marked `status: superseded` now
    counts `skipped_superseded_frontmatter`, and its earlier live record stays as it is.
  - A document marked as a draft, proposed, pending or under review keeps its earlier
    `accepted` record: a re-import compares content, not status, and the content is
    unchanged.

  Retiring or re-proposing such a record is manual.

### Security

- **The lockfile moves `anyio` from 4.14.1 to 4.15.1**, past three advisories fixed in
  4.14.2: TLS certificate spoofing through IDNA 2003 host-name encoding (critical),
  `run_process` keeping the parent's supplementary groups (high), and process-pool workers
  blocking on undrained stderr (moderate). The published package does not pin `anyio`, so
  this changes development and CI environments and installs made from `uv.lock`; a fresh
  install already resolves a fixed version.

## [0.4.0] — 2026-09-22

### Fixed

- **`supersede_decision` no longer half-applies when an anchor is invalid.** It wrote the
  successor and closed the predecessor before checking the anchors, so an illegal `relation`
  left an accepted successor with none, or only some, of its bindings: wholly or partly
  invisible to task-seeded retrieval, and permanent in an append-only store. An anchor list
  in which no anchor had a `name` reached the same state without any error.
- **All five anchor-taking tools (`add_decision`, `supersede_decision`, `add_fact`,
  `supersede_fact`, `add_anchors`) now validate the whole anchor list before their first
  write.** An illegal `relation` is now rejected on every path; `supersede_decision` used to
  accept it silently when no graph was present or the anchor had no `name`. Two cases are
  newly rejected. The first is a `name` or `file_path` that is not a string, which used to
  half-write on the fact paths and `add_anchors`, and on the decision paths whenever a graph
  was present. The second is a non-empty list in which no anchor has a `name`, which used to
  succeed while binding nothing. On `add_fact` it also bypassed the check that an anchorless
  fact supports a live decision.

## [0.3.1] — 2026-09-19

### Fixed

- **A session is now identified by its transcript, not by the host's `session_id`.** That
  field does not mean the same thing on every host: Claude Code mints one per session (and
  names the transcript after it), while Codex reports the *workspace* session — one id that
  outlives a single session, survives `resume`, and is shared by every thread beneath it. On
  Codex that put a whole store's history in one bucket (measured: 1928 of 1929 recorded events
  in one live store, spanning 27 hours) and, worse, made every per-session guard fire once per
  workspace instead of once per session: the second thread opened within a minute had its
  context map dropped as a duplicate injection, and the first thread to finish spent the
  capture nudge for all of them. All four hook sites now take the session from
  `transcript_path`'s file name, falling back to `session_id` when no transcript is given.
  On Claude Code this is the same string it already recorded — verified against a live store,
  where all 74 recorded session ids are exactly transcript names — so nothing moves there.
  The workspace session is kept beside it in the new `telemetry:session_group` meta key, as
  the only link between sibling threads. See
  [`docs/reference/hooks.md`](docs/reference/hooks.md#which-session-a-hook-is-in).

## [0.3.0] — 2026-09-19

### Added

- **`sidegraph-stats` — one screen of local usage statistics.** Reads the gitignored
  `.sidegraph/index.db` and reports how often memory was asked for, how much of the code
  worked on has memory anchored to it, what the store holds, and anchor health, with activation
  first. It states what was shown, asked and touched, and makes no claim about effect.
  Flags: `--db`, `--window DAYS` (default 30, the journal's retention), `--graph` and
  `--json` (the same report as data, with a figure the text withholds as `null`, never a
  zero). Read-only: it never creates or rebuilds the index,
  exits `2` on a bad `--window`, a missing index, an unreadable one or an unparseable record
  row, and says so in the report when there is too little data to print a ratio, when
  recording is off, when the budget counts were never recorded, or when the index is behind
  the committed records after a `git pull` (it never opens the store, so it states that
  rather than printing the old numbers). A new console script, so a committed surface: see
  [`docs/reference/cli.md`](docs/reference/cli.md) and
  [`docs/reference/stability.md`](docs/reference/stability.md).
- **`/sidegraph:stats` skill**, with its Codex twin (explicit invocation only). It runs
  `sidegraph-stats` and shows the output verbatim, without restating or interpreting the
  numbers.
- **A `render_events` journal and an optional `intent` parameter.** `get_task_context` and
  `query_decisions` now record what each render selected and what survived the budget in a
  new table of the local index (`drill_down` records the decisions it returned there too, with
  no budget figures), and accept an optional `intent` label for what asked. The
  label is recorded for statistics only and never affects what is returned. Both stay local:
  no network calls, `SIDEGRAPH_TELEMETRY=off` stops the recording, and the 30-day pruning
  keeps running when it is off, so opting out only reduces what is kept. See
  [`docs/reference/store-format.md`](docs/reference/store-format.md) and
  [`docs/reference/mcp-tools.md`](docs/reference/mcp-tools.md).

### Changed

- **The README's privacy note describes the local diagnostics as what was shown and
  touched**, rather than which memory is "earning its keep".

## [0.2.0] — 2026-09-18

### Added

- **`sidegraph-init` asks before auto-ratifying, instead of writing the policy silently.**
  In an interactive terminal it now asks one question, ratify low-risk records
  automatically, default answer yes (`auto-low-risk`), and commits whichever answer the
  person gives to the project's `.claude/settings.json` explicitly, so the choice is
  visible and changeable later. Outside a terminal (CI, a script, an agent-driven session)
  it asks nothing and writes nothing, printing the one line to add by hand instead: a
  silent write with nobody to answer is exactly what this avoids. An already-set policy,
  or an unparseable settings file, is reported directly with no prompt, since any write
  would be a no-op regardless of the answer. Two new flags make a scripted setup possible
  with no prompt: `--ratify-policy VALUE` sets an explicit value, and `--no-settings`
  skips the step entirely (the two are mutually exclusive). An existing value is never
  overwritten, other keys and file shape are preserved, and no settings problem can fail
  store creation. The README also drops its `sidegraph-import` mentions: that command's
  rationale-node import is undocumented for now, pending a review of the comment junk it
  currently writes into the store. See
  [`docs/reference/cli.md`](docs/reference/cli.md) and
  [`docs/reference/configuration.md`](docs/reference/configuration.md).
- **`sidegraph-doctor` gains a `graph-root-mismatch` finding and a `--graph` flag.** When
  `graphify update` runs from a subdirectory instead of the repo root, every anchor
  descriptor stops matching the graph's own `source_file` values, and sync used to mass
  orphan the whole store with no explanation. `sidegraph-doctor --graph <path>` samples the
  graph's anchorable paths and, only when a high fraction are missing relative to the repo
  root and one subdirectory resolves them all, reports the mismatch by name instead of
  leaving a person to hunt through false orphans.
- **`.github/secret_scanning.yml`** excludes the redaction regression suite's seeded fake
  secrets from GitHub's own secret scanning, so the fixtures that prove Sidegraph's
  redaction works stop tripping GitHub's scanner on every push.
- **Test suite hermeticity**: `tests/conftest.py` now strips every `SIDEGRAPH_`-prefixed
  environment variable before each test, so a developer's own shell (an exported
  `SIDEGRAPH_RATIFY_POLICY`, `SIDEGRAPH_TRUST_DIRTY_TREE`, and so on) can no longer change
  what the suite reports.
- **Session-link gate**: a `commit-msg`-stage pre-commit hook (`no-session-links`, backed
  by `tools/check_no_session_links.py`) rejects a commit message carrying a
  `Claude-Session:` trailer, a claude.ai/chatgpt.com session URL, or a bare `session_<id>`
  token. `default_install_hook_types` now wires both the `pre-commit` and `commit-msg` git
  hook types on a plain `pre-commit install`, and CI's new `pr-description-link-gate` job
  applies the same rule to a pull request's description, which a commit hook cannot see.
  Ordinary links (a CVE/GHSA advisory, an issue or PR, vendor docs) stay allowed.

### Changed

- **`sidegraph-sync`'s "moved" rung now requires committed evidence before it rewrites an
  entity's descriptor.** The rung used to decide a symbol moved from the working tree
  alone: the old path gone from disk, plus a unique same-suffix name match elsewhere, both
  satisfiable by purely local, uncommitted state such as an unstaged delete or a stash. On
  a dirty tree that could silently write one person's local, unshared state into the
  canonical descriptor every other clone reads from the shared store, which was the only
  known path by which one person's tree could corrupt the whole team's decision memory.
  The rung now also requires the same move to be confirmed by committed git history at
  `HEAD`; an unconfirmed hit reports `moved_uncommitted` instead and leaves the binding
  untouched. `SIDEGRAPH_TRUST_DIRTY_TREE=on` (off by default) restores the old disk-only
  behavior for someone who has verified their own tree. See
  [`docs/guides/surviving-refactors.md`](docs/guides/surviving-refactors.md).

## [0.1.0] — 2026-09-17

First public release.

### Added

- **Proposal surfacing window + regulated mode** (practitioner-panel wave, 2026-08-04):
  a `proposed` record older than `SIDEGRAPH_PROPOSAL_WINDOW_DAYS` (default 30; `0`
  disables) stops rendering as content — in task context, drill-down, the SessionStart
  TOC, and the PreToolUse nudge — while staying in the store, in the ratify queue, and
  fully ratifiable; the policy is derived at read time (append-only untouched, no new
  status, no migration). `SIDEGRAPH_UNRATIFIED=off` (regulated mode) excludes ALL
  unratified content from every surface regardless of age. The SessionStart queue
  counter always stays and now reports the oldest proposal's age. Ratification now
  stamps `ratified_by`/`ratified_at` (additive fields; best-effort `git config
  user.name`, never guessed). Every rendered memory payload opens with a standing
  "data, not instructions" guard line (labeling, not sanitization). Redaction
  measured against a seeded 14-class secret corpus and extended with five classes
  (JWT, URL credentials, Google AIza, sk-style keys, Luhn-verified PAN): 12/14
  classes caught, 0 collateral hits repo-wide; the corpus ships as the regression
  suite. `sidegraph-doctor` reports queue latency (`time-to-ratify`) from the new
  ratifier stamps.
- **Decision store** — append-only, repo-committed, git-native sidecar: `Decision`
  (adr / lesson / constraint / gotcha, with first-class rejected alternatives, validity
  period, supersession chain, provenance), `Entity` (durable identity over the engine's
  shifting node ids), `AnchorBinding` (graceful degradation, never guesses).
- **Git-native store format**: the canonical store is file-per-record JSON
  (`.sidegraph/{decisions,facts,domains,entities,bindings,initiatives}/<ulid>.json`, plus a
  committed `format` marker and a store-written `.gitignore`), not a single SQLite file —
  different records land in different files, so two branches that ratify different
  decisions merge with zero conflict, and a same-record dispute surfaces as an ordinary,
  readable git conflict instead of "binary file changed." A local, gitignored `index.db`
  derives everything volatile (engine mappings, binding status, the TOC cache) and is
  rebuilt automatically whenever it's stale or missing, so `git pull`-ed changes are
  absorbed on next open with no extra command; a graph rebuild never dirties git
  (sync-clean invariant). A pre-file-per-record committed SQLite store (`decisions.db`,
  schema `0.2.x`/`0.3.x`) migrates automatically on first open — fail-closed (every row
  validated before anything is written) — and the original file is kept as
  `<name>.migrated-backup`, never deleted (`0.3.0` was additive over `0.2.0`: a new
  `domains` table, `Decision.layer`, `AnchorBinding.relation`). This wave set `SCHEMA_VERSION` to `0.5.0` (later bumped to
  `0.6.0` — see the derived-community-bindings entry below), across six canonical subdirectories (`decisions`, `facts`, `domains`,
  `entities`, `bindings`, `initiatives`). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md).
- **`SIDEGRAPH_DIR`**: the primary store-location environment variable (default
  `.sidegraph`), replacing `SIDEGRAPH_DB` as the recommended knob. `SIDEGRAPH_DB` still
  works (deprecated, one-line stderr notice) via a back-compat dispatch rule that resolves
  old `SIDEGRAPH_DB=.sidegraph/decisions.db`-style configs straight to `.sidegraph/`. See
  [`docs/reference/configuration.md`](docs/reference/configuration.md).
- **`sidegraph-compact`**: packs terminal-status (superseded/rejected/deprecated
  decisions; superseded/dropped domains) records into immutable, write-once
  `archive/<date>-<seq>-<hash12>.jsonl` segments and removes their now-redundant hot
  files — explicit, human-run maintenance (recommended on the default branch) that keeps
  every record retrievable while trimming the individual-file count for records that can
  never change again.
- **`sidegraph-import --docs`**: a second, deterministic importer (no LLM, no API key) that
  parses decision-shaped markdown files directly — ADR/Nygard and spec-style documents — and
  writes one anchored `Decision` per qualifying document, idempotent per source file
  (an edited doc supersedes its prior import). Complements the existing rationale-node
  importer for teams with an existing ADR/spec corpus.
- **Mind-model layer**: `Domain` — a named, described area of the system (title +
  required WHY-IT-EXISTS summary, optional `parent_id`/subdomains), authored via three
  paths (`sidegraph-domains bootstrap` from graph communities, agent-in-session
  `propose_domains`, manual `add_domain`/`sidegraph-domains add`) through one unified
  ratification gate (`ratify`, covering decisions and domains together; the old
  decisions-only `ratify_decisions` name is kept as a deprecated alias). Cross-cutting
  `tag:<slug>` labels and `Decision.layer`/`AnchorBinding.relation` fields. Domain-aware
  Tier-1 anchoring (an accepted domain covering a community wins over the bare
  `community:<id>` entity). `sidegraph-sync` refreshes each accepted domain's community
  mapping (REPLACE, not union — Leiden ids are recycled across rebuilds) and flags
  domains that recompute to empty.
- **Named table of contents**: `SessionStart` now renders accepted domains (title,
  summary, mistake/subdomain counts) instead of a bare community listing the moment the
  first domain is ratified — precomputed on the sync path, no extra read-time cost.
  `drill_down(domain_slug)` walks one domain's summary, subdomains, member sample, and
  decisions — where `decisions` is the union of the domain's directly-tagged decisions and
  the decisions anchored to any code/doc entity in one of the domain's communities (a
  bounded, deduplicated community-membership join), so imported ADRs and other memory that
  anchored to the code rather than the domain abstraction surface under the area they belong
  to instead of showing zero. Budget-tight `get_task_context` now falls back to domain summary lines
  instead of hard-truncating the structural map. `query_structure`/`query_decisions`
  expose the two halves of `get_task_context` as standalone thin tools.
- **Domain onboarding, agent-curated**: a read-only `list_domain_candidates` MCP tool exposes
  bootstrap's own candidate selection (path-grouped, every Gate-5 guard already applied,
  each candidate carrying a durable `anchor`) without writing anything, and `DraftDomain`
  (the `propose_domains` path) gained a `seed_anchors` field — durable, entity-anchored
  membership (resolved into `communities` by `ratify`/`sidegraph-sync`, never populated
  directly) — so an agent-curated merge with no single shared path prefix can still land
  through the ratify gate, and its membership survives a fresh clone or a graph rebuild
  (an earlier raw community-id seed did not: Leiden renumbers communities every rebuild).
  Two new MCP tools round out the surface: `list_domains(status=None)` — a full listing of
  every domain (any status), with membership/lineage counts (`member_count`,
  `seed_anchor_count`, `parent_slug`/`child_slugs`) — and `supersede_domain`, which exposes
  the existing `Store.supersede_domain` primitive (previously reachable only via direct
  `Store` access) as the lineage-correct rename/re-scope path: closes the old domain, writes
  a `status=proposed` successor with `supersedes` set, and also directly fixes the mass-drop
  community-recovery case the naming guide documents. Two plugin skills build on all of
  this: `sidegraph:name-domains` (primary onboarding — studies the candidates, offers 2–3
  domain sets of different granularity built from `seed_anchors`, and writes only after an
  explicit human pick) and `sidegraph:manage-domains` (the escape hatch: add/rename/drop a
  domain by hand, now via `list_domains`/`supersede_domain` instead of "no tool for this").
  Replaces hand-curating a raw `sidegraph-domains bootstrap` listing as the recommended
  onboarding path; the CLI stays for scripted/CI use. See
  [naming your domains](docs/guides/naming-your-domains.md).
- **Five lifecycle plugin skills**: beyond the two domain skills, the plugin now carries
  the whole memory lifecycle as procedural skills — `sidegraph:setup` (end-to-end first
  run: engine → graph → store → wiring check, then hand-offs to the other skills),
  `sidegraph:record-decision` (the authoring craft: the write-path rule — human-asked
  `add_decision` vs agent-initiated `propose_decisions`, never `add_decision` on the
  agent's own initiative — the kind matrix, `rejected` as the highest-value field, anchor
  discipline), `sidegraph:ratify-decisions` (the in-session human gate over the pending
  queue: present each proposal with a recommendation, wait for explicit verdicts, one
  `ratify` call), `sidegraph:import-adrs` (drives `sidegraph-import` dry-run-first, with
  report triage and a human-gated real run), and `sidegraph:heal-anchors` (routes every
  `sidegraph-sync` finding — stale decisions, ambiguous/orphaned anchors, empty domains,
  overbroad path rules, slug conflicts — to its correct heal; supersede-with-fresh-anchors
  over binding inheritance, never a guessed rebind).
- **Redaction on the direct write paths**: `add_decision` and `supersede_decision` now run
  the same secret redaction as the propose/import pipelines (title/context/choice/rejected/
  consequences, plus tag text before slugification; an all-secret tag is skipped, never
  minted as `tag:redacted`), and their results carry a `redactions` count. Previously the
  direct MCP path committed its text verbatim into the repo-committed store — a gap against
  the redact-first rule, surfaced by the skills-wave blind audit.
- **`GraphifyReader` no longer hangs on large graphs**: `neighbors()`, `resolve()`, and
  `nodes_in_file()` are backed by prebuilt indexes instead of re-scanning every edge/node on
  each call. On an Apache Airflow-scale graph (23K nodes / 233K edges ≈ 5.4B comparisons) the
  old per-call O(E)/O(V) scans made `sidegraph-import` hang indefinitely (killed after 90s of
  CPU with zero output); the indexed reader returns promptly. A correctness/reliability fix
  for any large corpus, not just a speed-up.
- **Scale-aware default `limit` for domain candidates**: on a monorepo-scale corpus (a
  cross-project test found Apache Airflow returning 2,578 candidates, ~276K tokens),
  `list_domain_candidates` and `sidegraph-domains bootstrap` now default `limit`/`--limit`
  to 100 (the measured sweet spot, ~10K tokens) instead of unlimited — `limit=0`/`--limit 0`
  is the "all" convention when you really want everything. The response/output signals a
  truncation explicitly (`"truncated"`, `"total_significant"` on the tool; a stderr note
  naming the full count on the CLI) instead of silently showing a partial list as if it were
  everything. `sidegraph-domains bootstrap` also moved its over-50-domains nag to a
  read-only pre-check printed *before* a real run writes anything, instead of after —
  a large, unbounded run could previously flood the store with thousands of
  proposed-domain records before the nag ever printed.
- **Document templates are never imported as decisions**: `sidegraph-import --docs` detects a
  document TEMPLATE — a filename stem of `template` (`ADR-template.md`, `_ADR-template.md`,
  …), a `type: template` frontmatter field, or a placeholder-dominated Context/Decision body —
  and skips it outright, counted separately as `skipped_template` (`N skipped as template(s)
  (not decisions)` on both a real run and `--dry-run`) instead of importing it as a
  false-positive decision (a blank template's own `**Status:** APPROVED` line used to be
  enough to land it accepted).
- **`sidegraph-import --docs` warns on a likely wrong-cwd absolute path**: when at least half
  of anchor-attempted documents come back unanchorable and an absolute `--docs` path was
  passed, the command now prints a stderr warning naming the actual cause (doc paths resolve
  against the current working directory to match `graph.json`'s root-relative `source_file`
  entries, so an absolute path run from anywhere but the repo root misses every anchor)
  instead of leaving a silent `0 imported, N unanchorable` unexplained.
- **Overbroad `path_prefixes` no longer zeroes a domain's `seed_anchors`**: `sidegraph-sync`'s
  20%-of-communities cap on a domain's `path_prefixes` now applies to that path contribution
  alone — a domain with both an over-broad path rule and precise `seed_anchors` keeps its
  anchor-resolved membership (the path contribution is dropped and flagged, the anchors still
  replace `communities`); only a domain whose overbroad path had no `seed_anchors` to rescue it
  keeps its previous mapping, unchanged. A path candidate that resolves to a single community
  is never capped, regardless of ratio.
- **`drill_down` surfaces imported decisions on doc corpora**: a real-corpus accuracy eval
  found `drill_down` missing 8/9 imported ADR decisions under their covering domains on a
  pure-document corpus, even though the community-membership join (above) already existed.
  Root cause: `sidegraph-import --docs` anchors an ADR decision to the document's OWN
  file-level node, but Graphify clusters ALL doc file-level nodes into one hub community —
  so that entity's community is essentially never among a domain's `communities`, which come
  from the document's HEADING nodes in per-document communities instead. `decisions` is now
  the union of three sources, not two: (a) domain-tagged, (b) the community-membership join,
  and (c) a decision anchored to a whole-document entity whose file_path is covered by one of
  the domain's member nodes (i.e. the domain covers at least one heading from that same
  file). Branch (c) is scoped to whole-document anchors only — a code entity's node is never
  `file_type == "document"` — so a code corpus, where one file spans many communities, still
  can't have a decision anchored to an unrelated function in that file falsely surface.
  Verified against a real 3-domain/9-decision ADR corpus: decisions surfaced per domain went
  0/0/1 → 3/3/2 (8/9 total; the ninth ADR isn't a `seed_anchor` of any of the three domains,
  so it correctly stays unsurfaced everywhere).
- **`PreToolUse` hook**: a non-blocking nudge toward `get_task_context` on a blind
  `Read`/`Grep` of a source file, when the store has memory to offer
  (`SIDEGRAPH_GREP_NUDGE=off` to disable). It **names what memory holds about the path
  being opened** — up to two anchored record titles, mistakes first then newest first,
  clipped, `[unratified]` tagged when still proposed — falling back to domain/decision
  counts when the path carries nothing or a `Grep` has no path. The two forms hold
  SEPARATE once-per-session keys, so the generic form cannot consume the specific one.
  The counting-only form was measured across 168 sessions: it fires and agents read the
  file anyway (`design/testing/2026-07-31-whitepaper-evidence-results.md` §4.4).
- **`SessionStart` standing search instruction**: the injected context now leads with an
  unconditional instruction to call `get_task_context` before any search — bash
  `grep`/`rg`/`find` and MCP structure-query tools included, not just `Read`/`Grep` (the
  `PreToolUse` nudge above stays a just-in-time backstop for those two tools only).
  Prepended once at the hook-assembly level, so `render_toc`/`top_tier_map` themselves stay
  pure content formatters.
- **`Stop` hook, calmer by default**: the block-to-distill nudge now fires only once a
  session has produced >= 2 real user prompts (a substance gate over the transcript,
  distinguishing real prompts from tool-result noise) — it no longer fires at the end of
  a session's very first turn — and the nudge text itself shrank to a compact one-liner
  (`suppressOutput: true` set on the block response); `SIDEGRAPH_CAPTURE_NUDGE=off` to
  disable it entirely.
- **MCP server** (`sidegraph-mcp`, 22 tools as of this bullet — see the CI integrity toolkit
  bullet below for the 22 → 24 that followed later in this same 0.1.0 section):
  `get_task_context`, `query_structure`,
  `query_decisions` (budgeted, mistakes-first), `drill_down`, `list_domain_candidates`,
  `list_domains`, `add_decision`, `supersede_decision` (anchor inheritance), `add_fact`,
  `supersede_fact` (evidence layer — see below), `find_entity`, `get_entity_history`,
  `retrieve_decisions`, `list_facts`, `propose_decisions`, `propose_domains`, `add_domain`,
  `supersede_domain`, `list_proposed`, `ratify`, `ratify_decisions` (deprecated alias),
  `sync_anchors` (see below).
- **Claude Code integration**: SessionStart context map + Stop-hook capture/domain
  nudge + PreToolUse redirect nudge; secret redaction on drafts; human ratification
  gate. Codex CLI supported at configuration level (same hook contract).
- **Refactor-surviving sync** (`sidegraph-sync`): deterministic rebind ladder with
  cross-type suffix guard, community re-pointing, content-hash graph versioning
  (dirty-tree and non-git corpora covered).
- **Documentation corpora**: anchor decisions to markdown headings and files; LLM-free
  graph build; non-git folders supported.
- **Semantic docs layer** (optional, one API key): concept/rationale anchoring over
  Graphify's semantic pass; thematic cross-file retrieval.
- **Bootstrap import** (`sidegraph-import`): seed the store from rationale already in
  your sources (docstrings — no LLM required; document prose after the semantic pass),
  with dry-run, path/limit filters, file-aware idempotency, and an optional
  ratification gate.
- **CLIs**: `sidegraph-init` (bootstrap + wiring snippets, now including the
  `PreToolUse` hook), `sidegraph-ratify` (decisions and domains together),
  `sidegraph-sync`, `sidegraph-import` (rationale nodes and `--docs` markdown),
  `sidegraph-domains` (`bootstrap`/`add`), `sidegraph-compact`; meaningful exit codes.
- **Claude Code plugin + marketplace manifest** — `/plugin marketplace add
  SantyagoSeaman/sidegraph` then `/plugin install sidegraph@sidegraph` installs the MCP
  server and all three hooks in one step, functional today: it builds straight from this
  repository via `uvx --from git+...`, no PyPI publish required.
- **Plugin-first install docs**: README and the getting-started guides now lead with the
  plugin (or a bare `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main
  <entrypoint>` for manual/no-plugin wiring) as the primary install path; the source-checkout
  (`git clone` + `uv sync` + `uv run --project`) form moves to a "from source (contributors)"
  section. `graphifyy` installs without the `[mcp]` extra by default — `[mcp]` is documented
  as an optional extra for Graphify's own MCP server, which coexists with Sidegraph but isn't
  required by it.
- **PyPI package + `pip install sidegraph`**: Sidegraph packages as a pure-Python PyPI
  distribution; `pip install sidegraph` (or `uv tool install sidegraph`) puts `sidegraph-mcp`
  and every `sidegraph-*` CLI on your PATH. The README/getting-started guides lead Quickstart
  with it as the primary install (a PyPI version badge and a green-tests badge sit at the top). A
  tag-triggered `publish.yml` GitHub Actions workflow builds the sdist + wheel
  and publishes via **PyPI Trusted Publishing** (OIDC — no stored token), gated on the full
  test suite (it reuses the `ci.yml` lint+type+test workflow) and a check that the git tag
  matches the `pyproject` version, so a mistyped tag never ships. The plugin and
  `uvx --from git+…@main` paths keep working unchanged for the latest/unreleased build.
- **Facts layer**: a new `Fact` record captures compact, falsifiable, non-derivable
  knowledge that informed a decision — benchmarks, external constraints, trial-learned
  lessons ("the code does X" stays out; that's the engine's job and goes stale with every
  commit). `add_fact`/`supersede_fact` mirror the decision-side direct paths (redaction, a
  `redactions` count, append-only supersession) but fix a no-graph gap rather than repeat
  it: with no reader present, an anchor still binds an ORPHANED Tier-2 leaf instead of being
  silently dropped the way `add_decision`'s anchors are. `propose_decisions` grows a `facts`
  list per draft (attached — inherits the decision's anchors unless it names its own) and a
  top-level `facts` parameter (standalone — needs its own anchor or `supports` id, or the
  draft is rejected as unreachable). Ratification gates facts the same way as decisions, but
  cascades: accepting or dropping a decision accepts or drops every still-proposed fact that
  supports it too (`list_proposed`/`sidegraph-ratify` show these nested as `evidence: ...`
  lines under the decision, one verdict covering both); standalone facts are their own queue
  rows, and `--all`/batch-accept never double-counts a nested one. Retrieval renders a live
  supporting fact inline under its decision (`evidence: <statement> [<source>]`) and
  standalone facts in a new `## Known facts` block placed right after `## Decisions`,
  within the same shared budget — mistakes never lose budget to facts, by construction (a
  two-phase mistakes bucket places every mistake decision line before any evidence line is
  spent). Facts reuse the existing `AnchorBinding` machinery — `decision_id` renamed to
  `record_id` (it now holds a decision OR a fact id), `find_entity` gains `record_type`.
  This wave is what bumps `SCHEMA_VERSION` to `0.5.0` (see the store-format bullet above):
  a store still stamped `0.4.0` reloads its index and re-stamps to `0.5.0` automatically on
  open (no hard failure — the 0.4.0 canonical layout is fully forward-compatible).
  `facts/<ulid>.json` isn't yet covered by `sidegraph-compact` (deferred — facts stay
  entirely hot for now).
- **Derived community bindings**: Tier-1 `community:*` bindings, and the abstract entities
  they point at, are now fully DERIVED — index-only, never written to a canonical file, at
  capture time and at sync time alike (the scope condition is the entity: any abstract entity
  whose `canonical_name` starts with `community:`; `domain:*`/`tag:*`/initiative anchors are
  unaffected and stay canonical). Closes a real sync-clean violation found live: a pure
  `get_task_context` read had rewritten 8 committed `bindings/<record>.json` files and minted
  4 new canonical `community:*` entities on a graph rebuild that only renumbered Leiden
  communities. A fresh clone self-heals via the ordinary first `sidegraph-sync` (no committed
  community baseline is needed, for any decision whose community binding has a Tier-2 leaf to
  regenerate from); a store carrying old-format committed community entries tolerates them on
  reload — the stale *binding* entry sheds lazily, the next time that record's binding file is
  legitimately rewritten for an unrelated reason, but the stale *entity* file itself has no
  decay path and persists until a future cleanup command — no proactive cleanup command in
  this wave.
  Bumps `SCHEMA_VERSION` to `0.6.0`: a `0.5.0` store reloads its index and re-stamps
  automatically on open, same forward-compatible pattern as the `0.4.0` → `0.5.0` step.
  Mixed-version teams: a teammate still on pre-`0.6.0` code will transiently re-mint
  committed community entries until they upgrade (new code tolerates and lazily decays
  them — no data loss, self-healing). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md#community-bindings-are-derived-not-committed).
- **`SessionStart` surfaces the pending-ratification queue**: the injected context now ends
  with one more line, own `try/except` so a count failure never costs the map above it —
  `Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains) — review
  with the ratify MCP tool or sidegraph-ratify.` — whenever the queue is non-empty (nothing
  is appended at zero). The count is a new `Store.pending_ratification_counts()` query:
  proposed decisions, standalone proposed facts (a fact riding a proposed decision's cascade
  is covered by it and never double-counted), and proposed domains. Renders with or without a
  graph — it's store-only information. `SIDEGRAPH_RATIFY_NUDGE=off` disables the line
  entirely, same convention as `SIDEGRAPH_CAPTURE_NUDGE`/`SIDEGRAPH_GREP_NUDGE`. Closes a real
  adoption risk: the proposed→ratify gate is the store's only noise filter, but with no queue
  visibility the realistic failure mode wasn't "users reject the gate," it was "users forget
  it exists."
- **`SIDEGRAPH_AUTO_ACCEPT` — sanctioned opt-out, off by default**: set it to `on` and every
  agent capture through `propose_decisions` — each decision draft, its attached facts, and
  standalone facts passed via the top-level `facts` parameter — lands `status="accepted"`
  directly instead of `"proposed"`, skipping the ratification queue entirely.
  `provenance.source` still stamps `"agent"` regardless, so history never lies about
  authorship, only about whether a human reviewed it. **Domains are always exempt**:
  `propose_domains` never consults this variable, so a domain draft always lands `"proposed"`
  — few in number, high cost of a bad name/scope, and `drill_down`/retrieval hard-gate on
  accepted domains. No TTL/auto-promote path was considered and rejected — it would silently
  legitimize noise. Recommended for solo use; not recommended for team stores, since it
  removes the store's only noise filter. See
  [`docs/guides/capturing-decisions.md#4-auto-accept-opt-in`](docs/guides/capturing-decisions.md#4-auto-accept-opt-in).
- **`list_facts` MCP tool**: the `retrieve_decisions` counterpart for the facts layer —
  previously a fact had no direct read path at all (`retrieve_decisions` returns decisions
  only). Mirrors `retrieve_decisions`'s contract: excludes `superseded`/`rejected` by
  default, `include_superseded=True` to see that history too; sorted newest-first
  (`valid_from` descending, `id` descending tiebreak — facts carry no `kind`, so there is no
  mistakes-first ranking analogue).
- **`get_entity_history` no longer drops facts**: it used to call `get_decision` per binding
  only — a fact-only binding silently vanished from an entity's history with no trace. Now
  tries a decision lookup, then a fact lookup, per binding (an unknown record kind stays
  skipped, as before); every returned dict gains an additive `"record_type": "decision" |
  "fact"` key so a caller can tell them apart, and the merged list stays sorted `valid_from`
  descending.
- **`sync_anchors` MCP tool**: the diagnostic/heal MCP counterpart to `sidegraph-sync` — runs
  the same rebind pass (`force=True` to re-run even when `graph_version` already matches) and
  returns the report as data (`synced`, `from_version`/`to_version`, `counts`, `repointed`,
  `outcomes` filtered to non-`unchanged`/`rebound` entities, `stale_decisions`,
  `empty_domains`, `overbroad_domains`, `slug_conflicts`, `domains_refreshed`) instead of only
  printing it to stdout. A `synced: false` skip (version match, no `force`) returns every
  other field as an empty default, never a stale prior report. An unreadable graph returns
  `{"synced": false, "error": "..."}` instead of crashing — explanatory, matching this tool's
  diagnostic purpose, unlike every other tool's silent best-effort degrade. Closes the last
  MCP coverage gap: anchor-health diagnostics (stale decisions, orphaned anchors, slug
  conflicts) were previously CLI-only, so the `heal-anchors` skill couldn't run MCP-only — it
  now leads with `sync_anchors`, with `sidegraph-sync` kept as the CLI fallback.
- **CI integrity toolkit**: store integrity is now a checkable CI contract, not just a
  human convention.
  - **`sidegraph-sync --json`/`--check`**: `--json` prints the full sync report as one JSON
    object (`sync.report_as_dict` — the same shape `sync_anchors` returns, so a CI script
    and an in-session tool call never disagree about the fields); `--check` exits `2` when
    the report has an attention finding (an `error` outcome, a non-empty `stale_decisions`,
    or a non-empty `slug_conflicts` — `orphaned`/`ambiguous` outcomes and
    `empty_domains`/`overbroad_domains` stay informational, never fail the check). The two
    flags compose; exit `0` still covers a clean report and a version-skip, exit `1` the
    existing operational-error paths.
  - **`sidegraph-verify`** (new CLI + `verify_store` MCP tool): a store-integrity lint with
    two layers. The **snapshot layer** (always runs, pure read — never opens a `Store`,
    never migrates, never touches `index.db`) checks schema validity, `valid_to >=
    valid_from`, supersedes-chain resolution, dangling bindings/fact-supports, ULID
    uniqueness across hot files and archive segments (byte-identical archive-archive
    duplicates from a sanctioned cross-branch `sidegraph-compact` merge are exempt), archive
    segment parsing, and filename/id agreement — 10 pinned violation codes. The
    **transition layer** (`sidegraph-verify --against <git-ref>`, CLI-only — git plumbing
    isn't exposed through MCP) classifies every store file changed vs a git ref against the
    store's OWN write-path rules (derived from `store.py`, not invented): a real
    supersede/ratify/sync/compact stays legal, a hand-edit or history rewrite doesn't.
    `verify_store` is snapshot-only in v1; its docstring points CI users wanting the
    transition layer at the CLI. Exit contract: `0` clean, `1` operational error (unreadable
    store, bad git ref, not a git repo), `2` violations found.
  - **`add_anchors` MCP tool**: append bindings to an *existing* decision or fact —
    generalizes the fact-anchoring resolve-or-orphan ladder to either record kind. This is
    the missing half of triage: "code moved, decision still valid" now heals in place
    (bindings-only — the record file itself is never touched, so it stays legal under
    `sidegraph-verify`'s transition rules) instead of forcing a content-free
    `supersede_decision`/`supersede_fact` that pollutes history with a successor saying
    nothing new. Tool count: 22 → 24.
  - **`heal-anchors` skill gains a triage decision tree**: after `sync_anchors`, walk every
    stale/orphaned finding through (a) code moved, decision still valid →
    `find_entity`/`query_structure` to locate the new home → `add_anchors`; (b) content
    genuinely outdated → `propose_decisions` with `supersedes` (still `status=proposed` — a
    human ratifies); (c) subject genuinely gone → recommend a drop in the summary, never
    perform it. Includes a headless CI prompt example
    (`claude -p --mcp-config ... --allowedTools "mcp__sidegraph__*" ...`).
  - **New guide, [`docs/guides/ci-cd-maintenance.md`](docs/guides/ci-cd-maintenance.md)**:
    three GitHub Actions recipes — an anchor-health required check
    (`graphify update .` + `sidegraph-sync --json --check`), a store lint on PR
    (`sidegraph-verify --against <merge-base> --json` — diffed against the PR's merge base,
    not the base branch's moving tip, to avoid false positives on a branch that's behind),
    and a scheduled triage job (headless Claude + the `heal-anchors` playbook, proposals
    only). States two hard rules plainly: CI never ratifies; CI never auto-pushes canonical
    files to a branch nobody reviewed (the scheduled recipe opens a PR with whatever it
    proposed instead).
  - **Live-experiment refinement: `orphaned`/`ambiguous` no longer fail `--check`.** A live
    GitHub Actions run showed the anchor-health check staying red forever after a
    legitimate rename+heal — triage adds a live anchor to the decision, but the
    renamed-away entity's own leaf has no retirement path in an append-only store, so it
    stays orphaned for good. `sync.report_has_findings`'s failing classes are now exactly
    `error` outcomes, `stale_decisions`, and `slug_conflicts`; `orphaned`/`ambiguous`
    outcomes are informational only (still listed in `outcomes` and the PR comment) — when
    one actually costs reachability, the decision goes stale and `stale_decisions` already
    fires, so the signal isn't lost, just no longer duplicated as a permanent red flag.
- **`sidegraph-viz`**: a read-only CLI that renders the owned decision/fact store as an
  interactive graph — a maintainer's diagnostic view of what is anchored where. Nodes are
  decisions (colored by kind: mistakes warm, ADRs blue), facts (diamonds), and the entities
  they anchor to (by tier); edges are anchor bindings colored by status
  (live / degraded / orphaned), supersede chains, and fact→decision (`supports`) links. A
  footer counts the things you inspect for — orphaned and degraded bindings, and *dangling*
  records (memory with no anchor binding at all). Output is a **self-contained, offline HTML
  file** with the vis-network library vendored inline (no CDN, opens with a double-click),
  plus a machine-readable `{nodes, edges, stats}` JSON sibling (`--json` prints it to stdout).
  `--only-problems` narrows to the degraded/orphaned/dangling subgraph; `--no-superseded`
  hides history (shown dimmed by default); `--max-nodes` caps with reported (never silent)
  truncation; `--open` launches a browser. Read-only and store-only: it never writes a
  record, never touches `graph.json`, and adds no schema fields — it lives entirely in the
  portable core with no engine reader. A `demo` branch, shipping with a later release, will
  carry a rendered graph of Sidegraph's own decision memory, regenerated from `.sidegraph`
  on each release of that branch.
- **Imported `status: rejected` docs land `rejected`**, not `accepted`: a document the
  team turned down was being imported as a decision the team had adopted. It is not
  labelled `proposed` either — that would push a settled no into the ratification queue —
  and not skipped, because a rejected proposal with its reasons is exactly the
  "tried before, abandoned because…" the store exists to keep. `sidegraph-import` reports
  the count (`N landed rejected (source status: rejected)`).
- **`sidegraph-ratify` resolves an accepted domain's membership immediately**, like the
  MCP `ratify` tool already did — the same accept through two doors no longer leaves
  different state (`communities: []` until the next sync). New `--graph` flag; a relative
  path resolves against the store's own project root, and any failure falls back to
  scheduling the heal rather than failing the accept.
- **`sidegraph-doctor`** — one-stop store health: verify's strict checks + advisory
  curation lint (dangling records, decayed bindings, stale proposals, unreferenced
  entities, expired-but-open validity); `--check` escalates advisory findings for CI.
- **Drift→supersede affordance** — doctor's `code-drift` signal (a live, commit-stamped
  decision anchored to files that changed since its capture commit) now surfaces where an
  agent actually acts, instead of only in `sidegraph-doctor` output: a `[drifted]` tag on
  detailed-tier retrieval lines (`get_task_context`/`query_decisions`/`drill_down`, plus a
  one-line legend; `drill_down` returns it as a new optional `"legend"` key), a
  SessionStart map line ("N record(s) are anchored to code that changed after their
  capture…"), and a conditional Stop-nudge clause (inside the pinned 800-char bound).
  Freshness rides a store-meta cache (`code_drift_cache`) refreshed by both hooks via one
  deadline-bounded git scan with per-commit merge (a transient git failure neither erases
  real markers nor freezes the cache). `SIDEGRAPH_DRIFT_NUDGE=off` suppresses the two
  prose surfaces; the refresh and the markers stay on. Records superseded mid-session are
  live-filtered out at read time. See `docs/concepts/retrieval.md` (the `[drifted]`
  marker) and `docs/reference/hooks.md`.
- **Retrieval telemetry — telling dead memory from memory nobody has needed yet**: the read
  path now records what actually surfaced and what was asked about, in two gitignored
  `index.db` tables (`retrieval_shows`, `retrieval_seeds`) that, like `capture_sessions`,
  survive a `git pull`-triggered reload. A read still never produces a git diff. On top of
  it, `sidegraph-doctor` gains a `never-surfaced` curation finding that reports only the
  actionable case — a decision anchored where people keep working that has never once
  reached a render, "check its anchors or the ranking" — and stays silent on
  `0 shows / 0 queries`, which is an absence of occasion, not dead memory. A store nobody
  has read yet is `SKIPPED`, never flagged. `query_structure` records nothing: it returns
  no decision memory, so it never offers an opportunity for one to surface, and counting it
  would inflate the denominator. The data never leaves the machine, and
  `SIDEGRAPH_TELEMETRY=off` disables recording entirely — same convention as the nudge
  knobs.
- **`sidegraph-export-okf`** — one-way projection of the store into an Open Knowledge
  Format (OKF v0.1) bundle: every ratified decision/fact with full supersession history,
  domains, and anchored entities as cross-linked markdown concepts. Unratified `proposed`
  drafts are never published (superseded/rejected/deprecated history is kept). Deterministic
  (byte-identical re-export), safe out-dir handling, no new runtime dependencies.
- **Cross-process store opens no longer destroy each other's state**: two real defects behind
  `tests/test_store_concurrent_open.py`'s ~1-in-5 flakiness, both reproduced rather than
  reasoned about. (1) The index rebuild (`_reload_index_from_canonical`) used to `DROP` and
  recreate the six record tables with bare, autocommitting DDL — a concurrent reader, a
  long-lived MCP server included, could catch the gap and see `OperationalError: no such
  table: domains` (2/1600 opens measured). The rebuild now runs as one explicit transaction
  (`BEGIN IMMEDIATE` … `COMMIT`): a reader sees the pre-rebuild rows for the entire rebuild
  and the new ones only once it commits, and a failure anywhere in the body rolls back instead
  of leaving the index half-dropped. (2) The open-time tmp-debris sweep couldn't tell crash
  debris from another process's in-flight write and deleted live buffers faster than
  `_atomic_write_text_race_tolerant`'s 3-attempt retry could absorb above 3 concurrent openers
  (`FileNotFoundError` on `os.replace`, 1/1000 opens measured). The sweep is now age-gated —
  it only removes a `*.tmp` file older than 60 seconds, old enough that it can never be a live
  buffer — and every atomic write, not just the format marker, gets a per-write-unique tmp
  name, closing the same shared-inode corruption class for the store's own canonical records
  that the marker write was already protected from.
- **Every store write now commits or rolls back**: 16 of 17 committing methods in `store.py`
  had no rollback guard, so a failed write returned control with the SQLite connection still
  holding an uncommitted transaction — a second process then got `OperationalError: database
  is locked (after 2.1s wait)`. Every public write now runs through a shared
  `Store._mutation()` helper (reentrant, so nested writes like `ratify_domains`'s entity mint
  still work) that commits on success and rolls back on any failure, including a failing
  commit itself; `ratify_domains` commits per accept/drop item instead of once for the whole
  batch, so one bad id still can't undo the rest. `sidegraph-doctor` also stopped reporting a
  standalone fact reachable only through a `supports` link to a live decision as dangling —
  the write path has always accepted that shape, and all 9 `dangling-record` findings on this
  repo's own store were exactly this false positive — and the fact write gate (`add_fact`,
  `supersede_fact`, `propose_facts`) now requires an anchor or a `supports` id resolving to a
  LIVE decision, so the product stops minting records that check would go on to flag.
- **Entity get-or-create is now atomic across processes**: `get_or_create_abstract_entity`
  guarded its check-then-create with `self._lock` — a per-instance `RLock`, so an MCP server,
  a hook, and a CLI each holding their own `Store` could race the same lookup, all see
  nothing, and each mint a different id for the same logical entity (latent: 0 duplicates
  across this repo's own entity files, which is why it was worth fixing carefully rather than
  fast). The same shape existed for concrete entities via `find_entity` + `upsert_entity`
  written out longhand at two call sites. Both now run their lookup and their mint inside one
  `BEGIN IMMEDIATE` transaction (`Store._mutation(immediate=True)`, gated on whether a
  transaction is already open — not on nesting depth, since an outer scope that has only read
  holds no lock at all to gate on), collapsed onto one new `Store.get_or_create_entity`. A
  **UNIQUE index on logical identity was considered and rejected**: file-per-record exists so
  two branches merge without a git conflict, and two branches that each mint the same name
  produce two ULIDs — two files — that merge cleanly and leave the canonical store legally
  holding a duplicate; a UNIQUE index's rebuild (inside `Store.__init__`) would then raise
  `UNIQUE constraint failed` on exactly that merge, and the store could never be opened again.
  Lookups (`find_entity`, `find_abstract_entity`, and the get-or-create's own inline check)
  instead resolve any duplicate deterministically — the lowest `entity_id` wins, stable across
  processes and reopens — and `sidegraph-doctor` gains a `duplicate-entity` finding that names
  every id in a group and each one's binding count, so a human can decide which should absorb
  the others.
- **The freshness digest now only certifies what the index actually loaded**: `_touch_digest`
  used to hash the FILESYSTEM — every canonical file it could see, not the ones THIS process
  actually indexed — so a writer that crashed right after publishing a canonical file (before
  its own index write) could have that orphan silently certified as indexed by a different,
  already-open process's next unrelated write. `stored_digest == compute_digest()` then held
  while the record was missing from the index, permanently: unlike ordinary crash debris, a
  matching digest means the next open takes the fast path and never reloads, so nothing ever
  healed it — the only one of four external-review findings whose damage is silent *and*
  permanent. A new derived, gitignored `canonical_stat(subdir, stem, size, mtime_ns)` table
  (in `index.db`, never the canonical store) records every canonical writer's own file stat —
  captured from the tmp file *before* `os.replace`, never after, so a later stat can never pick
  up a DIFFERENT writer's replace — beside its canonical write, in the same transaction, for
  all seven canonical writers (the six that publish via tmp + `os.replace`, plus the archive
  segment writer, which has no `os.replace` at all and publishes via exclusive `os.link`).
  `_touch_digest` now compares its own digest walk against this table and refuses to stamp —
  and CLEARS the existing stamp, rather than merely leaving it in place — on the first file
  with no matching row or a different one: an id-based check was tried first and rejected,
  because it only catches an ABSENT record, not a REWRITTEN one (a ratify status flip, a
  supersede, an entity rename can all die pre-commit after mutating an existing canonical file,
  and an id check would happily certify the stale original). Clearing rather than withholding
  closed a gap found during implementation: two writers rewriting the SAME record with no crash
  at all can invert `os.replace` order against commit order, and if the loser's touch merely
  *withheld* a new stamp, the winner's already-valid (and still disk-matching) stamp would be
  left in place while the index quietly drifted under it — clearing forces the next open to
  reload unconditionally instead of trusting a value comparison that shape can defeat. The
  check stays one-directional (a `canonical_stat` row whose file is now gone — compacted away,
  or a derived `community:*` entity that never had a canonical file to begin with — is normal
  and never blocks a stamp); a cross-process writer lock was considered and rejected, since it
  would still need this same check (a crash while holding the lock reintroduces the identical
  defect) while this check needs no lock at all.
- **`sidegraph-bootstrap`**: a guided onboarding command that turns an existing ADR/spec corpus
  into reviewed, anchored Sidegraph memory in one run — scan, preview, interactive review, write
  only after the user reviews a redacted action summary and types the literal `confirm`, verify
  anchors and the selected host, and prove production retrieval by calling the same
  `get_task_context` path a live session uses. Preview and review share one immutable plan, so
  what gets reviewed is exactly what gets written. Supports six document profiles in v1 —
  `generic-adr`, `superpowers`, `genkovich-sdd`, `spec-kit`, `bmad`, `openspec` — auto-detected
  from the profile marker and input globs, or pinned with `--profile` (an explicit choice always
  wins; several specific-profile matches force an explicit choice instead of merging dialects).
  `--docs PATH` adds files/directories in the same dialect; permissive arbitrary-document
  extraction (matching no profile at all) is not supported in v1. A host support matrix (in the docs, not
  printed by the command) distinguishes what is actually verified, not just attempted: Claude
  Code gets the complete v1 flow (MCP, SessionStart, Stop, and the Read/Grep `PreToolUse` nudge
  all verified), and the terminal prints `INTEGRATION  Claude Code MCP + hooks verified`; Codex
  gets MCP/SessionStart/Stop verified when configured but has no `PreToolUse` equivalent, so the
  terminal prints `INTEGRATION  Codex best-effort` instead and never `fully supported` (the
  optional `--report` markdown does write `- fully supported: false` for Codex). Exit codes are
  a real contract: `0` covers full activation and safe, no-write diagnostics; `1` is a usage or
  operational error and is unreachable once anything has been durably written (a post-write I/O
  failure during completion rendering is `2`, never `1`, so a `1` never leaves partial memory
  behind for a `--resume` to reconcile); `2` covers `incomplete` (no durable candidate completed
  the plan, or an actionable prerequisite/check remains) and `partial-recoverable` (at least one
  canonical file is durable but a later write/index/reopen/verification step failed). `--resume`
  is a marker only — the underlying scan/review/reconcile flow is idempotent, so reissuing the
  same command without it behaves identically. See
  [`docs/getting-started/bootstrap.md`](docs/getting-started/bootstrap.md).
- **`SIDEGRAPH_RATIFY_POLICY` — auto-ratification policy, `manual` by default**
  (2026-09-13): lets a deployment with no human in the loop delegate the ratification gate
  to a deterministic, stamped policy. `manual` (the default; also unset, empty, or any
  unknown value — matched exactly after trimming, so a typo fails safe) changes nothing:
  canonical state, every rendered surface, and the three CLI batch summary lines stay
  byte-identical. `auto-low-risk` lets an eligible `gotcha`/`lesson` decision or standalone
  fact ratify itself at write time; `auto-all` also admits `adr`/`constraint` decisions and
  domains. Six write surfaces read it once per invocation: `propose_decisions` (decisions
  and standalone facts), `propose_domains`, `sidegraph-import` with `--propose` (rationale
  mode, and `--docs` writes that land as a new `proposed` record — status-derived drafts,
  `superseded` re-imports and `status: rejected` documents never auto-ratify), and
  `sidegraph-domains bootstrap` (a candidate whose derived `path_prefixes` is empty stays
  proposed); a dry run performs no transition. The gates are conjunctive and LLM-free: the
  policy admits the shape (under `auto-low-risk` a superseding draft never does), at least
  one live Tier-1/Tier-2 anchor binding (domains: a graph reader, a clean path-prefix lint,
  and a resolving seed anchor or non-empty prefixes), a clean non-dry-run write, and
  provenance. An attached fact never self-ratifies — it rides its decision's cascade, and
  one ineligible attached fact keeps the decision proposed (re-checked under the write
  lock). An auto-ratified record goes through the same ratify transition a human uses,
  is stamped `ratified_by="auto:<policy>"`, surfaces as ordinary accepted memory, and
  leaves the queue by the ordinary accept path; a superseding proposal written under an
  auto policy leaves its predecessor open until the successor is ratified — automatically,
  or later by a human. Additive
  fields: `ratified_by` and `auto_ratify_error` on every `propose_decisions` result
  (nested facts included) and `propose_domains` result — `null` under `manual`, so the key
  set grows even there — and `auto_ratified`/`auto_ratify_failures` on the import and
  bootstrap reports. Under a non-`manual` policy the three CLI summaries append
  `, auto-ratified N` before `(skipped: …)` and each failure prints
  `auto-ratify failure: <id>: <reason>` on stderr. `sidegraph-doctor`'s human output gains
  two informational lines once any `auto:` stamp exists (`auto share`, `auto supersede
  rate`, hot files plus archive) and `time-to-ratify` now excludes `auto:` stamps; `--json`,
  exit codes and finding codes are unchanged, and an `auto:`-stamped record never raises
  `unratified-accept`. Out of scope: `sidegraph-bootstrap`'s keep-proposed verdicts (a human
  verdict wins), manual domain adds (`add_domain`, `sidegraph-domains add`) and
  `supersede_domain` successors. Legacy `SIDEGRAPH_AUTO_ACCEPT=on` still wins when both are
  set and stays stamp-less. See
  [`docs/reference/configuration.md`](docs/reference/configuration.md).
- **Git-native provenance: commit trailers and `sidegraph-blame`.** A
  `prepare-commit-msg` git hook (`sidegraph-prepare-commit-msg`) comments candidate
  `Sidegraph-Decision:` trailers into the commit-message template (captured-this-session
  records, plus decisions anchored to staged files) for a human or agent to uncomment,
  never auto-appended. `sidegraph-blame` joins `git blame` hunks back to the
  decisions/facts each commit carries, via commit trailers and `provenance.commit`. Both
  are read-only over records and never block or stall a `git commit`. See
  [`docs/reference/git-bindings.md`](docs/reference/git-bindings.md).
- **Codex plugin support.** A marketplace manifest (`.agents/plugins/marketplace.json`)
  and per-host Codex manifests (`plugin/sidegraph/.codex-plugin/`,
  `plugin/sidegraph/codex/`) let the same plugin install into Codex CLI. Each skill ships
  a matching `agents/openai.yaml`, and Codex gets `SessionStart` and `Stop` hooks (no
  `PreToolUse` equivalent). See [`docs/integrations/codex.md`](docs/integrations/codex.md).
- **Community files.** A pull request template (`.github/PULL_REQUEST_TEMPLATE.md`),
  `.github/CODEOWNERS`, and a Contributor Covenant 2.1 code of conduct
  (`CODE_OF_CONDUCT.md`).
- **Public release runbook**
  ([`docs/reference/releasing.md`](docs/reference/releasing.md)): the step-by-step
  procedure for cutting the allowlist-only public snapshot and tagging a release.
- **`pyproject.toml` classifiers and keywords**: PyPI discovery metadata (development
  status, license, supported Python versions, and search keywords such as `mcp`,
  `decision-log`, and `team-memory`).
- **Lint gate additions** in `.pre-commit-config.yaml`: `gitleaks` (broad secret
  detection), `detect-private-key`, `zizmor` and `actionlint` (GitHub Actions workflow
  security and correctness), `check-toml`, and a `uv.lock`/`pyproject.toml` sync check.

### Changed

- **Whitepaper rev 6.0 is the GitHub edition, and it ships alone** (rev 5.0 shipped
  2026-09-05, rewritten to rev 6.0 on 2026-09-15): `design/whitepaper/draft.md` is a
  ground-up rewrite under the leitmotif fixed with the owner the same day. Engineers
  remember decisions, not code. Agents read the same code but keep no decisions.
  Sidegraph is the missing link. The rewrite opens with the three ways teams cope and
  their ceilings, restores the related-work section with every neighbour linked, and
  walks one real decision chain from this repository's own store (`Store.ratify`, two
  supersession chains, the plan check that surfaced a twice-rejected design). Every
  measured number and its boundary from rev 5.1 survives. The claim ledger is now at rev
  2.7. The text passed three review rounds (Codex, muse, a Fable subagent, each blind to
  the others), two style rounds, and eight blind-reader passes. The public snapshot still
  carries `docs/whitepaper/index.md` only: the evidence edition, the claim ledger, the
  artifact bundle, the bibliography and the figures stay in the internal repository, and
  `docs/llms.txt` and the pilot kit no longer point at them. The pilot kit is described as
  the procedure and prompts (four runs per question, blinding done by hand). The
  measurement harness behind the paper is not published.
- **Proposed-record quarantine — a retrieval-ordering change for existing stores**: a proposed
  decision or fact (not yet ratified) now ranks after every accepted record on the five
  production delivery surfaces spec rev 3 §3.5 names — `get_task_context` and `query_decisions`
  (both via `rank_decisions`), `drill_down`, `SessionStart`/TOC, and the `PreToolUse` title
  nudge — instead of competing for mistakes-first placement the way an accepted gotcha does. It
  is still discoverable and still rendered `[unratified]`; it simply can no longer outrank a team's
  already-ratified memory on those five surfaces, so review debt now carries a visible retrieval
  cost. A store that already carries proposed records will see their retrieval position move to
  the back of the list on upgrade. `retrieve_decisions`, the raw MCP listing, is a **deliberate,
  unquarantined exception**: it keeps sorting by kind alone (gotcha, then lesson, then everything
  else), so a proposed gotcha or lesson can still rank ahead of an accepted ADR there — every row
  still renders its own `status`, so trust information is present even though ranking isn't. See
  ADR `01KZ27ZQXHAP8J8TYSAPSHSMT3`.
- **Whitepaper promoted to a public engineering release candidate**: `design/whitepaper/draft.md`
  (rev 2.2, Bootstrap publication audit, 2026-08-02) and `design/whitepaper/README.md` now carry
  "Public engineering whitepaper release candidate" status. The claim-ledger audit (58 claims: 16
  implemented property, 23 design rationale, 15 empirical finding, 4 open hypothesis, backed by
  18 publication sources) permits publishing the bounded architecture, the completed evidence
  program's measured mechanisms and negative results (E14 Phases 0–3), and the new cold-start
  Bootstrap workflow. This is not a final paper: release-candidate technical review findings
  remain open, and any claim that task-aware delivery causes better engineering outcomes, beats a
  baseline, or provides superiority stays blocked on the controlled Track B campaign and its
  raw-artifact review.
