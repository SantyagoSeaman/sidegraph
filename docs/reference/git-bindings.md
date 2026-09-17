# Git bindings: commit trailers + `sidegraph-blame`

Two features that make git history itself carry pointers to decision memory, then join
blame hunks back to it — line-granularity "why" (killer feature #2, "walk the chain").
Both are read-only over records: neither creates, mutates, or supersedes a `Decision` or
`Fact`. A trailer or a blame hunk names a ULID; the record's content always lives in the
store — never in git history itself. A wrong pointer is corrected by store-side
supersession, never by rewriting a commit.

The seam for both is [`src/sidegraph/gitio.py`](../../src/sidegraph/gitio.py) — the only
module that runs git subprocesses for these two features (`capture._capture_commit` and
`verify._find_repo_root` already shell out for their own, unrelated needs).

## `sidegraph-prepare-commit-msg` — commented trailer candidates

A `prepare-commit-msg` git hook. On a plain `git commit`, it comments candidate
`Sidegraph-Decision:` trailers into the message template for you (or your agent) to
uncomment — it never appends one automatically. Wrong attribution baked into an immutable
trailer is the failure mode this gate exists to prevent; a human or agent stays in the
loop.

```
# Sidegraph: uncomment the trailers this commit actually implements
# Sidegraph-Decision: 01KZ… — retry idempotency keys via httpx event hooks
```

### Install

Raw git hook:

```bash
cat > .git/hooks/prepare-commit-msg <<'EOF'
#!/bin/sh
exec sidegraph-prepare-commit-msg "$@"
EOF
chmod +x .git/hooks/prepare-commit-msg
```

Or via the [`pre-commit`](https://pre-commit.com) framework — add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/<org>/sidegraph
    rev: <tag>
    hooks:
      - id: sidegraph-prepare-commit-msg
```

then `pre-commit install --hook-type prepare-commit-msg` (the framework does not install
`prepare-commit-msg`-stage hooks with a bare `pre-commit install`). `sidegraph-init` prints
this hint; it never touches `.git/hooks` itself — installing a git hook is something a
repo opts into explicitly, not something `sidegraph-init` does on your behalf.

### Candidates

Two sources, deduped by id:

- **(a) captured this session** — decisions AND facts whose `provenance.commit` equals
  `HEAD` (or, for a pre-git-bindings record with no `commit` stamp at all, one created
  after `HEAD`'s commit time — best-effort, and shrinking as more records carry a real
  stamp). On an empty repo (no commits yet), `git rev-parse HEAD` has nothing to name, so
  this rule — and its fallback — are skipped entirely; only rule (b) runs.
- **(b) staged-file bindings** — for each file `git diff --cached --name-only` reports,
  the decisions anchored to an entity whose descriptor names that exact path, capped at
  the 5 strongest bindings by weight. Entities with no file path (Tier-0/Tier-1,
  abstract/domain anchors) are never even considered — they can't consume a slot in that
  cap.

An empty candidate set writes nothing at all (no header noise, no diff to review).

### The comment char, and why `auto` degrades to nothing

The candidate lines are written using the repo's own `core.commentChar` (`git config
--get core.commentChar`) — a custom char (e.g. `;`) is used verbatim; the default when
unset is `#`.

`core.commentChar=auto` is special-cased to **write nothing**, deliberately: git resolves
`auto` to a real character *before* this hook ever runs, but `git config --get
core.commentChar` still hands this hook back the literal string `"auto"` — the character
git actually picked is unknowable from here. Guessing (e.g. falling back to `#`) risks the
candidate lines surviving into the final commit message uncommented, exactly the
permanent-wrong-attribution failure mode the whole comment-gate exists to prevent
(measured: a `commit.template` with a leading `#` line makes git's `auto` resolution pick
a *different* character, so a hook that assumes `#` writes lines that are never actually
comments). Degrading to "write nothing" beats guessing.

### Never blocks, never stalls

`git commit` must never fail, warn, or hang because this hook did:

- any internal error writes nothing and exits `0`;
- the store's `index.db` is opened **strictly read-only** (`file:…?mode=ro&immutable=0`)
  with a 0.5-second busy-timeout fallback — well under Python `sqlite3`'s default 5.0s,
  which against a concurrent writer's held lock would otherwise turn into a multi-second
  `git commit` stall (the actual uninstall failure mode, measured);
- the index is **never rebuilt** inside the hook — a missing or unreadable `index.db`
  degrades to "write nothing" rather than attempting a first-commit-after-clone rebuild;
  a commit is not the place for that;
- the whole candidate-collection pass is bounded by a 2-second wall-clock budget, past
  which it gives up and writes nothing.

## `sidegraph-blame` — derived line-level "why"

```
sidegraph-blame <file> [--range A,B] [--db PATH] [--json]
```

`git blame`, joined to the decisions and facts each hunk's commit carries, via **two**
join paths (deduped):

- **trailers** — `git log -1 --format="%(trailers:key=Sidegraph-Decision,valueonly,separator=%x2C)" <sha>`
  reads back every `Sidegraph-Decision:` trailer that commit carries, comma-joined.
  (Note the exact option spelling: git's trailer format uses `valueonly`, singular — the
  plausible-looking `valuesonly` is not a recognized option, and git silently echoes the
  literal format string back instead of erroring, which would otherwise look like every
  hunk resolving to one bogus "record.")
- **provenance** — decisions and facts whose `provenance.commit` equals that same sha,
  independent of any trailer (this is what lets a record captured before this repo ever
  adopted the hook still join by blame).

```
$ sidegraph-blame src/client.py --range 40,55
src/client.py
  42-48	a1b2c3d4e5f6	2026-08-05T10:03:00+00:00	01KZ… (decision): retry idempotency keys via httpx event hooks
  49	9f8e7d6c5b4a	2026-08-01T14:22:00+00:00	01KY… (superseded by 01KZ…): use requests with manual retry loop
  50-55	a1b2c3d4e5f6	2026-08-05T10:03:00+00:00	(no decision/fact recorded)
```

A record resolved via an old sha that has since been superseded prints its
`superseded_by` pointer — blame surfaces the history, the store's own supersession chain
does the rest. An unresolved trailer ULID (a typo, or a record from a different store) is
listed as unresolved — never guessed at or silently dropped.

Read-only over records here too: `sidegraph-blame` opens the store's index read-only and
never a writable store; a missing or unreadable index degrades every hunk to unresolved
records rather than failing the command outright (blame without a store is still useful
blame). A git-level failure — not a repository, an unknown file or `--range`, git not
installed — is a genuine operational error and exits `1`.

**Output cap**: 50 hunk rows or 6000 characters, whichever comes first. When the cap is
hit, the human table's last line (`--json`'s `"cap"` field) states the limit and that rows
were omitted — never a silent truncation.

## See also

- [`reference/cli.md`](cli.md#sidegraph-prepare-commit-msg) /
  [`reference/cli.md`](cli.md#sidegraph-blame) — flags and exit codes
- [`concepts/data-model.md`](../concepts/data-model.md) — `Provenance.commit`, stamped
  best-effort on every decision/fact write path
