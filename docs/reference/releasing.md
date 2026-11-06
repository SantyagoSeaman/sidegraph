# Releasing Sidegraph

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

How a release is cut, end to end. This page is for the maintainer running one, and for
anyone curious how the version they installed came to exist. Only the maintainer can
actually push a tag or a public snapshot. See [`SECURITY.md`](../../SECURITY.md) and
[`CODEOWNERS`](../../.github/CODEOWNERS) for who that is.

## Versioning policy

Sidegraph follows [SemVer](https://semver.org/). Pre-1.0, that comes with the standard
caveat: a minor bump (`0.x.0`) may break an interface without a major version change. What is
actually load-bearing at any given version, and what is free to move, is not this page's
call. See [`reference/stability.md`](stability.md) for the three tiers (Committed,
Provisional, Not a contract) and which surfaces sit in each.

## Where the version string lives

One release bumps every copy below together. `pyproject.toml` is canonical; the rest either
restate it in prose or carry it in a manifest field.

| File | What to change |
|---|---|
| `pyproject.toml` | `[project].version`, the canonical version `publish.yml` checks the tag against |
| `src/sidegraph/__init__.py` | `__version__`, kept in lockstep with `pyproject.toml` |
| `CLAUDE.public.md` | the "pre-1.0 (vX.Y.Z)" line near the top |
| `CHANGELOG.md` | see below, the `[Unreleased]` heading becomes the release heading |
| `plugin/sidegraph/.claude-plugin/plugin.json` | `version` field, kept in lockstep with `pyproject.toml` |
| `plugin/sidegraph/.codex-plugin/plugin.json` | `version` field, kept in lockstep with `pyproject.toml` |
| `plugin/sidegraph/.muse-plugin/plugin.json` | `version` field, kept in lockstep with `pyproject.toml` |
| `plugin/sidegraph/.grok-plugin/plugin.json` | `version` field, kept in lockstep with `pyproject.toml` |
| `package.json` | OpenCode Git package's `version`, kept in lockstep with `pyproject.toml` |
| `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` | Update any `plugins[]` entry's `version` if that optional field is present; leave it omitted otherwise |

The version is also a literal in every launch, because every host runs
`uvx --from sidegraph==X.Y.Z --python 3.13 <entry point>` and nothing rewrites the literal at
release time. The version bump writes it in each of these files, and
`tools/check_public_twins.py` (a pre-commit hook) fails until every one equals
`[project].version`:

| Launch site | Literal |
|---|---|
| `plugin/sidegraph/.mcp.public.json`, `plugin/sidegraph/hooks/hooks.public.json`, `plugin/sidegraph/grok/hooks.public.json`, `plugin/sidegraph/codex/mcp.public.json`, `plugin/sidegraph/codex/hooks.public.json` | `sidegraph==X.Y.Z` in each command (the Codex hooks carry it twice per event, in the POSIX and the Windows command) |
| `plugin/sidegraph/.muse-plugin/plugin.json`, `plugin/sidegraph/muse/hook.py` | the MCP argv and `SOURCE` |
| `index.js`, `.opencode/INSTALL.md` | `RUNTIME_SOURCE` and the MCP override. The OpenCode *package* spec `git+…#main` is not a launch of Sidegraph and stays |
| `plugin/sidegraph/skills/*/SKILL.md` | every runner line of the ten skills that run a CLI |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | the `sidegraph-verify` command |

The launch commands in `src/sidegraph/cli.py` and `src/sidegraph/host/hooks.py` take the version
from `__version__` and need no edit. The development twins (the files without `.public.`) run the
checkout and carry no version.

The plugin launches need `uv` 0.6.10 or newer on the user's machine. That floor is not a version
string to bump; raise it only with a tested change to the launch command.

`README.md` is deliberately absent from this table. Its Status section carries no version
string — only a PyPI badge that reads the live index — so there is nothing there to bump and
nothing that can go stale between releases. It used to carry a `vX.Y.Z, on PyPI as ...` line;
that line was removed in 0.3.0, and this note replaces its row.

The public tests in `tests/test_codex_plugin.py`, `tests/test_muse_plugin.py` and
`tests/test_opencode_plugin.py` compare the host manifest/package versions with
`pyproject.toml`; the Codex test also checks `src/sidegraph/__init__.py`. The internal
`tools/preflight_release.py` checks all four manifests, detects an undeclared direct
`.*-plugin/plugin.json` or root `package.json`, and checks optional versions in the two
catalogs. Its tests and the real four-host snapshot inventory check in
`tests/test_release_snapshot.py` stay internal; they are excluded from the public tree.
Malformed version metadata blocks release; unreadable version inputs make the preflight
exit 2. Use `rg '<old-version>'` to find other
mentions, then distinguish current-version claims from historical changelog, design, and
schema-fixture references.

## Order of a release

A release moves through five stages, and the public `main` moves only in the last:

1. **Prepare** on the internal repository: refresh the dependency lock, bump the version, merge.
2. **Cut** (`tools/release-public.sh "release: vX.Y.Z" --push`): pin the snapshot's
   dependencies, push a public `release/vX.Y.Z` branch, wait for its CI, then push the tag
   `vX.Y.Z`.
3. **Publish**: the tag starts `publish.yml`, which tests (the matrix is skipped when the
   tag's tree already passed it on `release/vX.Y.Z`), builds and, after the `pypi`
   environment approval, uploads to PyPI.
4. **Promote** (`tools/promote-release.sh vX.Y.Z`): once PyPI serves the version, check the
   PyPI wheel against a wheel built from the tag, write the plugin's lock, and fast-forward the
   public `main` to it.
5. **Check** the published release (see [Post-release checks](#post-release-checks)).

A plugin manifest on the public `main` names a version that is on PyPI, because `main` moves
only after step 3. Release candidates (`X.Y.ZrcN`) run the same stages, ending in a scratch
repository instead of `main` (see [Release candidate validation](#release-candidate-validation)).

## Pre-release checklist

Prepare the release on a release-prep branch in the development repository, merge it
through a Ready pull request, then run the clean-tree preflight on merged `main`:

1. **Refresh the dependency lock.** The published package pins every dependency to the exact
   version in `uv.lock`, so a dependency fix reaches users only through a Sidegraph release, and
   this is the step that lets one in. In its own pull request with full CI, run
   `uv lock --upgrade`, `uv sync` and the test suite, and merge it before the release-prep
   branch. Then audit the runtime closure for known advisories:

   ```bash
   env -i PATH="$PATH" HOME="$HOME" uv export --frozen --no-config --no-dev --no-emit-project \
     --no-hashes --no-header --no-annotate --format requirements-txt > "$TMPDIR/closure.txt"
   uvx pip-audit --no-deps --disable-pip -r "$TMPDIR/closure.txt"
   ```

   A finding is fixed with a pin bump in that pull request, or recorded in the release issue
   as accepted. The lock also has to pass the wheel-coverage gate of the cut: a compiled
   dependency with no wheel for a supported platform on every released CPython minor from 3.13
   up (macOS on Apple silicon, Linux on x86-64 and arm64, Windows on x86-64) stops the release.
   A lock that predates a new stable Python fails it until this refresh; the minors the gate
   checks are the `PYTHON_MINORS` constant of `tools/pin_public_dependencies.py`, which the
   release that first meets a new stable Python raises.
2. Update `CHANGELOG.md`: keep a fresh empty `## [Unreleased]` first and add
   `## [X.Y.Z] — YYYY-MM-DD` beneath it. Group changes using Keep a Changelog headings. In
   the link list at the end of the file, point `[Unreleased]` at `compare/vX.Y.Z...HEAD` and
   add `[X.Y.Z]: …/compare/v<previous>...vX.Y.Z` above the previous version's line. A
   candidate (`X.Y.ZrcN`) needs the `## [X.Y.Z]` heading too, dated or not.
3. Update every file in the version table above, including the launch literals, and any
   affected `docs/` page.
   Keep all four manifest/package versions together and update optional catalog entry
   versions already present. Do not add version fields to otherwise unversioned entries.
   Run `uv lock` so the root package version in `uv.lock` agrees with `pyproject.toml`;
   inspect the diff to ensure no unrelated dependency versions changed.
   Run the host version tests before the slower release checks. The release preflight
   (`tools/preflight_release.py`) refuses a checkout with uncommitted changes, so it runs
   in step 9, after the commit.
   If the release adds a field to a stored record (`Decision`, `Fact`, `Domain`, `Entity`,
   `AnchorBinding`, `DriftCheck` or a model nested in them), or changes what one
   serialises, the guard (`tests/test_record_fields.py`) fails until
   `RECORD_FIELDS_REQUIREMENT` in `src/sidegraph/store_layout.py` is raised to this release and
   the snapshot is regenerated (`uv run python tests/test_record_fields.py --write`). The
   CHANGELOG entry must then say that all clients must upgrade and that every team runs
   `sidegraph-require --sidegraph X.Y.Z` and commits `.sidegraph/format` before anyone on the
   new version writes; nothing raises a store's requirement for them, and until they do an older
   client can still drop the field. See [Stability](stability.md#mixed-versions).
4. `uv run pytest -q`. Full suite green. Then `uv run pytest tests/private -q` in the development
   repository: the slow rare-case set that PR CI skips; a release may add tests to it.
5. `uv run pre-commit run --all-files`. Lint, format, types, and staged secret scanning
   clean, including store-only changes; also zizmor, actionlint, and the shipped-surface/
   public-twin/docs-link checkers described in `CONTRIBUTING.md`. Additionally run
   `uv run pre-commit run gitleaks-history --hook-stage manual --all-files` from a full
   checkout: CI rejects shallow history and scans all reachable commits, including merge
   changes, separately from the staged hook.
6. `uv build`. Must succeed and produce both `dist/sidegraph-X.Y.Z.tar.gz` and
   `dist/sidegraph-X.Y.Z-py3-none-any.whl`. This is the internal build, which keeps the source's
   dependency ranges. The cut builds the wheel that is published from the pinned snapshot and
   checks it (step 10 does this).
7. A real-install smoke test against the freshly built wheel, before anything is tagged:
   `uvx --from ./dist/sidegraph-X.Y.Z-py3-none-any.whl --python 3.13 sidegraph-doctor --help`. This confirms
   the package actually installs standalone and its console scripts resolve, which `pytest`
   alone (running against the editable checkout) does not exercise.
8. Commit the release preparation on the release-prep branch, open a Draft pull request,
   and mark it Ready when final. Wait for `full-ci` to pass on its exact head SHA and settle
   the Copilot review, then merge the PR and update internal `main`. A direct push to
   internal `main` is refused by its ruleset. Both checkouts must now be clean; run the
   remaining steps from the merged internal `main`.
9. Run `uv run python tools/preflight_release.py --online`. It verifies version agreement,
   changelog shape, clean branches, and target repository, and that `vX.Y.Z` is not already
   tagged in the **internal** checkout or its origin. Run it **after** the release changes are
   committed: running it first can only report the work you have not prepared yet.
   Then validate the release candidate (see [Release candidate validation](#release-candidate-validation)).
10. Cut the allowlisted public snapshot with
   `tools/release-public.sh "release: vX.Y.Z" --push` (see [The cut](#the-cut)). `--push` itself
   refuses unless `vX.Y.Z` is untagged in the **public** repository and `CHANGELOG.md` dates it.
   Refresh `demo` separately when required.
11. Approve the `pypi` environment when `publish.yml` asks, then promote with
    `tools/promote-release.sh vX.Y.Z` (see [Publish and promote](#publish-and-promote)).

Only after all eleven steps, the candidate validation and any requested
[desktop smoke](#desktop-smoke-when-requested) pass is the release done.

### The cut

`tools/release-public.sh "release: vX.Y.Z" --push` takes the allowlisted files from internal
`main` and, on the lean snapshot, does the following in order:

1. **Pins the dependencies.** `tools/pin_public_dependencies.py` exports the runtime closure of
   the internal `uv.lock` (`uv export`), rewrites the snapshot's `pyproject.toml`
   `[project].dependencies` to those exact `==` pins, locks the snapshot with `uv lock`, and
   requires every locked version to equal the internal lock's and every source to be PyPI. It
   refuses an unpinned or URL requirement, an empty export, and a closure in which a compiled
   pin has no usable wheel for a supported platform on any released CPython minor from 3.13 up.
   `pyyaml` is the one named exception: it ships no wheel for the
   newest minor and installs from its source distribution without a compiler. Every `uv` call
   runs under a clean environment with `--no-config`, so no `UV_INDEX_URL` or `UV_FIND_LINKS` of
   the release machine reaches the lock. The internal `pyproject.toml` keeps its ranges.
2. **Builds the snapshot wheel and checks it** with `tools/check_runtime_wheel.py`: the
   `sidegraph/` members must equal the snapshot's `src/sidegraph` byte for byte
   (`--files-from`), every `Requires-Dist` must be an exact pin and equal to the export
   (`--expected-requires`), and `Requires-Python` must equal the snapshot's
   (`--expected-requires-python`). The wheel is not shipped; `publish.yml` builds the published
   one from the tag.
3. **Lands the snapshot on `release/vX.Y.Z`**, created from `origin/main`, never from a local
   `main` and never as an orphan, and commits it. The cut refuses when `origin/release/vX.Y.Z`
   or the tag `vX.Y.Z` already exists on the remote, except in the re-runs below.
4. **Pushes the branch and waits for its CI.** `ci.yml` runs on `release/**`, and
   `tools/wait-branch-ci.sh` waits for the `ci.yml` push run of the exact head commit and
   requires its `full-ci` job to be green. The tag is pushed only after that: a tag can never be
   deleted from the public repository, and a tagged version is spent.
5. **Pushes the tag `vX.Y.Z`** on that commit. Nothing on the public `main` moves.

`--demo` is unchanged: the `demo` branch is pushed directly, with no release branch and no tag.

Re-running the cut is safe while the tag is absent. If `release/vX.Y.Z` already holds the same
tree, the branch push is skipped, its CI is checked again, and the tag goes on. If it holds a
different tree (its CI was red and the fix is on internal `main`), the script replaces the remote
branch with `--force-with-lease` on the head it fetched, waits for CI and tags. Release branches
are not protected; only `main`, `demo` and `v*` tags are. Any other state on the remote refuses,
with the commands to inspect it.

### Publish and promote

The tag push starts [`publish.yml`](../../.github/workflows/publish.yml) (see [Tag-driven
publish flow](#tag-driven-publish-flow)). Once PyPI lists the version, promote it:

```bash
tools/promote-release.sh vX.Y.Z
```

The script waits for PyPI to serve one `py3-none-any` wheel of that version (30 minutes by
default) and then:

1. fetches the tag from the canonical public repository and, for a final version, requires that
   the public `main` is an ancestor of it;
2. downloads the PyPI wheel and runs `tools/check_runtime_wheel.py --compare` against a wheel
   built from the tagged commit: the `sidegraph/` bytes, the `Requires-Dist` set and
   `Requires-Python` must be equal;
3. writes `plugin/sidegraph/pyproject.toml` and `plugin/sidegraph/uv.lock`, locked to the PyPI
   version with every source on PyPI, and commits them on top of the tag. Claude Code does not
   apply that lock; it is there for tools that read a lock beside the manifest. Because the
   simple index can lag the JSON API, the lock is retried for a bounded time;
4. pushes that commit to `release/vX.Y.Z`, waits for `full-ci` on it, fast-forwards the public
   `main` to it (anything but a fast-forward is refused), and deletes the branch. The tag stays on
   the commit below it; both are on `main`. The promotion commit changes only the plugin's
   `pyproject.toml` and `uv.lock`, so its `full-ci` takes the lint-only path (no test matrix),
   and so does the `main` push that fast-forwards to the same commit.

The target is classified by repository identity (host, owner, repository), not by string:
a final version goes only to the canonical repository, a pre-release only to the scratch
repository named by `SIDEGRAPH_PROMOTE_URL`, and force is never used on the canonical one.

Between the publish and the promote, the public `main` still serves the previous release.

### If a stage fails

Nothing before the tag is spent, and the public `main` does not move before the promote.

- **Branch CI is red.** No tag was pushed. Fix on internal `main` and run the cut again; it
  replaces the branch while the tag is absent.
- **The publish workflow fails after the tag.** Re-run its failed jobs; the `pypi` approval is
  asked again. A failure that repeats is deterministic: the tag cannot be deleted and the cut
  refuses a tagged version, so fix on internal `main` and release the next release candidate or
  patch version.
- **Promote stops.** It changes nothing on `main` until the last push. Run it again with the same
  tag: it resumes from a promotion commit already on `release/vX.Y.Z` when that commit is exactly
  one commit on the tag that changes only the two plugin files and passes the lock checks.
  Anything else on that branch makes it refuse.
- **The PyPI wheel differs from the one built from the tag**, or the lock names a source other
  than PyPI: promote stops before pushing. Do not work around it; the release is not what the
  tag says. Release the next version.
- **The public `main` moved after the cut.** It moves only at a promote, so this means two
  releases overlapped. Promote refuses a final version whose tag is not a fast-forward of `main`;
  release the next version from the new `main`.

### Release candidate validation

Run this between step 9 (preflight) and step 10 (the real cut). The plugins' public manifests
cannot be exercised by the repository's tests alone, so a candidate is published and promoted to
a scratch repository first and checked there. Candidates are PyPI pre-releases (`X.Y.Zrc1`):
a PyPI version can never be reused, and an unpinned install does not select a pre-release. The
commands are the maintainer's; steps marked **[Owner]** are the repository owner's.

The scratch repository is `SantyagoSeaman/sidegraph-rc`, kept private between candidates; the
owner makes it public while the portal validates one. A candidate
goes to the canonical public repository as a branch and a tag, and to PyPI, like a release; only
its promoted lock commit goes to the scratch repository. The candidate's source is therefore
public before the release.

1. Bump the internal version to `X.Y.Zrc1` in every place of the version table, including the
   launch literals. `package.json` carries the npm form `X.Y.Z-rc.1`, which `preflight_release.py`
   maps from the PEP 440 version. Merge it.
2. Cut it: `tools/release-public.sh "rc: vX.Y.Zrc1" --push`. The branch `release/vX.Y.Zrc1` and
   the tag `vX.Y.Zrc1` go to the canonical repository, and after the `pypi` approval
   `X.Y.Zrc1` is on PyPI.
3. Promote it to the scratch repository once PyPI serves it:
   `SIDEGRAPH_PROMOTE_URL=git@github.com:SantyagoSeaman/sidegraph-rc.git tools/promote-release.sh vX.Y.Zrc1`.
   Promote refuses a pre-release for the canonical repository. A scratch target is replaced with
   `--force-with-lease`, not fast-forwarded: a candidate's lineage is a sibling of the previous
   candidate's.
4. **[Owner]** Run the plugin directory portal's Validate on the scratch repository. A blocking
   finding stops the release: fix it on internal `main` and repeat from step 1 with `rc2`.
   Findings that only hold are noted for the submission.
5. **[Owner]** The live checks, each on a machine with an empty uv cache:
   - Claude Code, fresh `CLAUDE_CONFIG_DIR`, the plugin from the scratch marketplace, a session
     started in a project subdirectory: the MCP server connects; the spawned command is
     `uvx --from sidegraph==X.Y.ZrcN --python 3.13 …`; the environment's interpreter is 3.13;
     the installed packages equal the wheel's pins; SessionStart prints the map; a Read fires
     PreToolUse.
   - Grok Build, fresh: the same, plus the Bash filter firing and `/sidegraph:stats` running the
     pinned runner line.
   - Codex: install, accept the hook trust, and see SessionStart and a hot hook fire.
   - The cold first-start time with no Python 3.13 on the machine.
   - The routes the `--python 3.13` flag does not cover, on a machine whose newest Python is a
     minor released after 3.13: `sidegraph-doctor --help` through `uvx` with the pin but no
     `--python` flag, and `pip install` into a venv of that Python, both succeed.
   - A pre-update install: the previous release's public Codex manifest, which launches from the
     repository's `main` branch, against the candidate's promoted source, with only that newest
     Python on the PATH. SessionStart and a hot hook run.
6. Bump to `X.Y.Z` and run steps 2 to 11 of the checklist. The final goes to the canonical
   repository only.
7. Delete the `release/vX.Y.ZrcN` branches of the canonical repository. The candidate tags stay;
   the public ruleset blocks tag deletion.

If the portal does not accept the scratch repository, skip its Validate part and record that in
the release issue; the live checks still run from the scratch repository. Validation then happens
on the released public `main`, and a remaining finding ships in the next release.

### Desktop smoke when requested

Run this check only when the owner requests it for a particular release. Run it against the
latest release candidate, once it is promoted to the scratch repository (step 3 of the
candidate validation), and finish before the final cut. Record the scratch repository's
promoted commit, app/OS, installation route, expected results and observed evidence. Before starting, create a
separate local Git scratch repository for each surface and commit a source file in each.
Run steps 1–4 in each surface and its own repository. A result belongs only to the surface
where it was observed.

1. Install or update Sidegraph for that scratch repository using
   the [desktop integration steps](../integrations/desktop-apps.md). For Claude, use a
   marketplace entry already added through Claude Code. Before installing, refresh it from
   a terminal with `claude plugin marketplace update sidegraph`, then install into this
   repository. For Codex, use the verified CLI installation and hook-trust path before
   opening the app. If its marketplace is already present, run
   `codex plugin marketplace upgrade sidegraph` rather than adding it again.
   For existing installs, use the [Claude Code update recipe](../getting-started/claude-code-setup.md#update-an-existing-plugin)
   or [Codex update recipe](../getting-started/codex-setup.md#keep-the-plugin-updated).
   Open and trust that repository in the app as the integration steps direct. Preserve
   the install scope and enable any project/local installation in this scratch repository.
   Review changed hooks, then close and relaunch the app from the GUI.
2. Ask the agent to run the Sidegraph setup skill in that repository. Confirm the graph
   and store were created. Ask it to use `add_decision` to create one test record with a
   graph-resolved anchor: an entity name and its repository-relative `file_path`. Save the
   returned record `id`. Confirm `status` is `accepted` and both `anchors_skipped` and
   `anchors_orphaned` are empty. Then start a fresh session in that surface and confirm
   the opening map.
3. Read the version from the installed plugin manifest and the opening context's
   `Sidegraph X.Y.Z` line. Both must match the candidate version. Check source commits too:
   version equality alone cannot distinguish two builds of the same version. Match the
   installed plugin source commit to the scratch repository's promoted commit, or compare
   hashes of its loaded manifests, MCP configuration, hooks and skills with that commit if
   install metadata has no commit.
   Locate the running MCP server's Python environment from its process information, in the
   uv archive (`uv cache dir`, then `archive-v0/<id>/`), not a project Python installation.
   Its `site-packages` must hold `sidegraph-X.Y.Z.dist-info` for the candidate version, with
   no `direct_url.json`: a PyPI install has none, so a file there means a git or path
   install. The same check serves every host, because every host launches the same pin.
   Confirm that the session did not write the
   [launch-commit file](hooks.md#the-launch-commit-file). A launch warning, missing artifact or
   unresolved source identity is incomplete evidence. For Claude, retain the installed-plugin
   entry's commit too.
4. Call `get_task_context` for the anchored file and confirm the saved test-record `id`
   is returned.
5. In Claude Desktop's local Code tab, ask the agent to Read that file for the first time
   in this session. Retain the PreToolUse transcript block naming the saved record `id`,
   or matching journal evidence in `.sidegraph/index.db`: a
   [`retrieval_events`](store-format.md#retrieval_events-derived-index-only) row with
   `kind='inject'`, `key` equal to the file, `detail` equal to the record id and the current
   `session_id`; or a `delivery_occurrences` row for that session with `surface='hook_read'`
   joined to a `delivery_placements` row for that record (`occurrence` equals the header's
   `id`). See
   [delivery evidence](store-format.md#delivery-evidence-derived-index-only). Journal
   recording requires `SIDEGRAPH_TELEMETRY` not to be `off`; use transcript evidence when
   it is disabled. Missing evidence is not a pass. Codex has no Sidegraph read hook, so
   mark this item not applicable and retain its explicit retrieval result from step 4.
6. In a fresh interactive Codex thread, send at least two real work prompts and retain the
   Stop capture prompt as that thread's rollout `hook_prompt` or in-app Hook feedback
   entry. Answers to agent questions and hook feedback do not count as work
   prompts. See the [capture gates](hooks.md#sidegraph-stop).

Use separate result rows for the Code tab and Codex view of the ChatGPT desktop app.
All applicable checks in a requested smoke must pass before the final cut. Skipped, unsupported
or unobserved checks do not count as passes. The Codex read-hook item is not applicable.
The macOS live evidence does not establish native Windows desktop-app results.

If a requested check fails, stop before the final cut. Fix it on internal `main`, put the
fix's changelog entry under the version's heading, and release the next candidate (`rc2`)
through a new release-preparation pull request: steps 3 to 9 of the checklist, then steps 2 and
3 of the candidate validation. This includes the tests, private suite, pre-commit, build, wheel
smoke and online preflight. Then repeat this whole desktop smoke against the new candidate.

### The public main moves only at promotes

Plugin installs older than 0.25.0 launch from the public repository's `main`, so pushing `main`
reaches them at their next session start, on their machine, in any project. Current plugin
manifests name an exact version and update only when the plugin version changes, but the public
`main` is also what `/plugin marketplace add` clones. For both reasons `main` moves only at a
release, and only by `tools/promote-release.sh`, after PyPI serves the version. The cut never
pushes it: `tools/release-public.sh --push` refuses unless the version is untagged in the public
repository and `CHANGELOG.md` dates it (a pre-release needs its final version's heading), checked
before anything is written and again right before each push. A docs-only or other non-release
change waits for the next release, or ships in an urgent patch release.

Two consequences follow. First, bump the version in `pyproject.toml` and date its
`CHANGELOG.md` heading only as part of release preparation (steps 2 and 3 above). Once both
are on `main`, every cut counts as a release until the tag exists, so doing them ahead of the
release defeats the guard. Second, a tag cannot be deleted from the public repository, and the
cut refuses a tagged version. A failed publish is therefore never fixed by re-tagging: see [If a
stage fails](#if-a-stage-fails).

Between the release-preparation commit and the tag, every `--push` is a re-cut of that same
release, and the tag goes on the last one.

Never push the public `main` by hand: the gates live in `tools/release-public.sh` and
`tools/promote-release.sh`, and a manual `git push` from the public checkout bypasses them.

## Tag-driven publish flow

PyPI publishing is driven entirely by a git tag. There is deliberately no manual dispatch: a
manual run has no tag ref, and the version guard below only means something against a real
one. The tag is pushed by the cut (`tools/release-public.sh "release: vX.Y.Z" --push`), on the
head of `release/vX.Y.Z` once branch CI is green, so do not tag by hand.

**The tag goes to the public repository, not the internal one.** PyPI's trusted publisher is
configured against `SantyagoSeaman/sidegraph`, so a tag pushed to the internal repository
produces a valid OIDC token with the wrong `repository` claim and the publish job dies on
`invalid-publisher` — after the `test` job has run. The cut pushes to the public
repository and nowhere else. Pushing the release-prep branch and merging its Ready PR to
internal `main` triggers CI, but no publish.

The tag push triggers [`.github/workflows/publish.yml`](../../.github/workflows/publish.yml)
(`v*.*.*` matches a release candidate such as `v0.25.0rc1` too), three jobs in sequence.

1. **`test`**: reuses `ci.yml` wholesale (pre-commit lint plus the pytest matrix). `ci.yml`
   first checks whether the tag's tree already passed the full matrix on `release/vX.Y.Z`. If
   it did, the matrix is skipped and only lint runs; otherwise (a tag pushed by hand, for
   example) the full matrix runs. Build and publish never run if this fails. On the public
   repository `pyproject.toml` pins every dependency with `==`, so the `sdk-compatibility`
   job prints "pinned package: the SDK range is tested in the source repository" and skips
   its remaining steps; the ranges are proved on the internal repository.
2. **`build`**: checks out the tag, then runs the tag-to-version guard. It reads
   `[project].version` out of `pyproject.toml` with `tomllib`, strips the leading `v` off
   `GITHUB_REF_NAME`, and fails the job if the two strings disagree. This check is
   unconditional, not gated on the tag pattern, so a future trigger that isn't a real tag push
   fails loudly instead of silently shipping whatever `pyproject.toml` happens to say. Then
   `uv build` runs, and the sdist and wheel upload as the `dist` artifact. The snapshot's
   `pyproject.toml` carries the pinned dependencies, so the wheel's `Requires-Dist` is the
   exact closure.
3. **`publish`**: downloads that artifact and pushes it to PyPI via Trusted Publishing (OIDC,
   `pypa/gh-action-pypi-publish`), with no stored API token. Gated on the `pypi` GitHub
   Environment, which needs the maintainer's approval.

When the workflow is green, run [the promote](#publish-and-promote).

## GitHub Release

`publish.yml` does not create a GitHub Release. That step is manual, after the workflow goes
green:

```bash
gh release create vX.Y.Z --title vX.Y.Z --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md | sed '$d')
```

Or, from the UI, paste the same dated `CHANGELOG.md` section (heading through the entries
under it, stopping before the next `##`) as the release notes body.

## Post-release checks

Green CI and a pushed tag are not proof the release actually works for a consumer. Confirm:

- The PyPI project page (`https://pypi.org/project/sidegraph/`) shows the new version.
- `uv tool install --python 3.13 sidegraph==X.Y.Z` (or `uvx --from sidegraph==X.Y.Z --python 3.13
  sidegraph-doctor --help`) succeeds against the real published package, not the local checkout.
- The public `main` is the promotion commit: it holds `plugin/sidegraph/pyproject.toml` and
  `plugin/sidegraph/uv.lock`, the tag `vX.Y.Z` is its parent, and `release/vX.Y.Z` is gone.
- The plugin marketplace path installs the version it names: `/plugin marketplace add
  SantyagoSeaman/sidegraph` then `/plugin install sidegraph@sidegraph` inside Claude Code, and
  the spawned MCP command is `uvx --from sidegraph==X.Y.Z --python 3.13 …`. Plugin manifests
  name the release version, so the marketplace needs no tag.
- After `publish.yml` succeeds, run `tools/verify-release.sh --after-publish vX.Y.Z`. It
  clones the public repository at the tag, builds a wheel from it, downloads the PyPI wheel of
  that version and compares them with `tools/check_runtime_wheel.py --compare`: the package
  bytes, the `Requires-Dist` set and `Requires-Python` must be equal. It cannot run earlier,
  because PyPI has no wheel for the version until the publish finishes. The promote runs the
  same comparison before it moves `main`.

The internal maintainer checkout also carries a network-bound install smoke script
(`tools/verify-release.sh`; it is intentionally absent from the public snapshot) that exercises
`uvx --from sidegraph==X.Y.Z --python 3.13` for `sidegraph-init`, `sidegraph-bootstrap --help`
and an MCP `initialize` handshake, and checks that the released plugin manifests carry the same
pin, all against the real published package. It is deliberately not wired into CI, since it
installs over the network. The maintainer runs it by hand after the promote.

## The public snapshot

Release preparation happens in the private development repository; the release branch, the tag
and the publish workflow run in the public repository. `sidegraph` on GitHub is a **published
snapshot**, cut from a private development repository by an allowlist-only script. Every
release replaces the snapshot's tree wholesale
with exactly the paths that script names, nothing else; the snapshot's `pyproject.toml` pins
every dependency exactly, where the internal one declares ranges, and the promote adds the
plugin's lock on top. When published, a `demo` branch carries
the same snapshot plus a sample `.sidegraph/` decision store, so you can browse an
example of the store without running anything. See
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) for what that means for a pull request here.
