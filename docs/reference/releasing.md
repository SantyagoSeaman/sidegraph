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

The Claude plugin launches the runtime wheel bundled under `plugin/sidegraph/runtime/`, so it
needs `uv` 0.6.10 or newer on the user's machine. That floor is not a version string to bump;
raise it only with a tested change to the plugin's launch command.

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

## Pre-release checklist

Prepare the release on a release-prep branch in the development repository, merge it
through a Ready pull request, then run the clean-tree preflight on merged `main`:

1. Update `CHANGELOG.md`: keep a fresh empty `## [Unreleased]` first and add
   `## [X.Y.Z] — YYYY-MM-DD` beneath it. Group changes using Keep a Changelog headings. In
   the link list at the end of the file, point `[Unreleased]` at `compare/vX.Y.Z...HEAD` and
   add `[X.Y.Z]: …/compare/v<previous>...vX.Y.Z` above the previous version's line.
2. Update every file in the version table above and any affected `docs/` page.
   Keep all four manifest/package versions together and update optional catalog entry
   versions already present. Do not add version fields to otherwise unversioned entries.
   Run `uv lock` so the root package version in `uv.lock` agrees with `pyproject.toml`;
   inspect the diff to ensure no unrelated dependency versions changed.
   Run the host version tests before the slower release checks. The release preflight
   (`tools/preflight_release.py`) refuses a checkout with uncommitted changes, so it runs
   in step 8, after the commit.
   If the release adds a field to a stored record (`Decision`, `Fact`, `Domain`, `Entity`,
   `AnchorBinding`, `DriftCheck` or a model nested in them), or changes what one
   serialises, the guard (`tests/test_record_fields.py`) fails until
   `RECORD_FIELDS_REQUIREMENT` in `src/sidegraph/store_layout.py` is raised to this release and
   the snapshot is regenerated (`uv run python tests/test_record_fields.py --write`). The
   CHANGELOG entry must then say that all clients must upgrade and that every team runs
   `sidegraph-require --sidegraph X.Y.Z` and commits `.sidegraph/format` before anyone on the
   new version writes; nothing raises a store's requirement for them, and until they do an older
   client can still drop the field. See [Stability](stability.md#mixed-versions).
3. `uv run pytest -q`. Full suite green. Then `uv run pytest tests/private -q` in the development
   repository: the slow rare-case set that PR CI skips; a release may add tests to it.
4. `uv run pre-commit run --all-files`. Lint, format, types, and staged secret scanning
   clean, including store-only changes; also zizmor, actionlint, and the shipped-surface/
   public-twin/docs-link checkers described in `CONTRIBUTING.md`. Additionally run
   `uv run pre-commit run gitleaks-history --hook-stage manual --all-files` from a full
   checkout: CI rejects shallow history and scans all reachable commits, including merge
   changes, separately from the staged hook.
5. `uv build`. Must succeed and produce both `dist/sidegraph-X.Y.Z.tar.gz` and
   `dist/sidegraph-X.Y.Z-py3-none-any.whl`. The snapshot step additionally builds the Claude
   plugin's runtime wheel from the snapshot's own sources and checks its contents (step 9
   does this; `uv build` here is the PyPI build).
6. A real-install smoke test against the freshly built wheel, before anything is tagged:
   `uvx --from ./dist/sidegraph-X.Y.Z-py3-none-any.whl sidegraph-doctor --help`. This confirms
   the package actually installs standalone and its console scripts resolve, which `pytest`
   alone (running against the editable checkout) does not exercise.
7. Commit the release preparation on the release-prep branch, open a Draft pull request,
   and mark it Ready when final. Wait for `full-ci` to pass on its exact head SHA and settle
   the Copilot review, then merge the PR and update internal `main`. A direct push to
   internal `main` is refused by its ruleset. Both checkouts must now be clean; run the
   remaining steps from the merged internal `main`.
8. Run `uv run python tools/preflight_release.py --online`. It verifies version agreement,
   changelog shape, clean branches, and target repository, and that `vX.Y.Z` is not already
   tagged in the **internal** checkout or its origin. Run it **after** the release changes are
   committed: running it first can only report the work you have not prepared yet.
   Then validate the release candidate (see [Release candidate validation](#release-candidate-validation)).
9. Cut the allowlisted public snapshot with
   `tools/release-public.sh "release: vX.Y.Z" --push`. This creates and pushes the public
   `main` commit that will receive the release tag. `--push` itself refuses unless `vX.Y.Z`
   is untagged in the **public** repository and `CHANGELOG.md` dates it (see "The public
   main moves only at releases" below). Refresh `demo` separately when required.

Only after all nine steps, the candidate validation and any requested
[desktop smoke](#desktop-smoke-when-requested) pass is the public snapshot ready to tag.

### Release candidate validation

Run this between step 8 (preflight) and step 9 (the real snapshot push). The bundled Claude
plugin runtime cannot be exercised by the repository's tests alone, so a candidate snapshot is
cut into a scratch public repository first and checked there. The commands are the
maintainer's; steps marked **[Owner]** are the repository owner's.

1. **[Owner]** Create an empty scratch public repository on GitHub and give its name and SSH
   URL.
2. Clone it next to the internal checkout. Cut the candidate into it with the same script and
   gates as a release:
   `SIDEGRAPH_PUBLIC=<scratch checkout> SIDEGRAPH_PUBLIC_URL=<scratch SSH URL> tools/release-public.sh "rc: vX.Y.Z" --push`.
   The script prints a NOTE for each override. The snapshot is the one step 9 will push, so
   nothing beyond the coming release is published.
3. **[Owner]** Run the plugin directory portal's Validate on the scratch repository.
4. Read the result. Any blocking finding stops the release: fix it on internal `main` and
   repeat from step 2. Findings that only hold are noted for the submission.
5. **[Owner]** Grok gate (release-blocking): install the plugin in Grok from the scratch
   repository on a machine with no earlier Sidegraph environment. Start a session in a
   project subdirectory and confirm that the MCP server connects, SessionStart prints the map,
   a Read fires PreToolUse, and `runtime/.venv` exists under Grok's plugin copy. Then run
   `/sidegraph:stats` and confirm from the transcript that the command Grok ran starts with
   the absolute runtime path, not `uvx --from git+…@main`. Outcomes:
   - Hooks or the MCP server fail: release-blocking. Flip `GROK_PUBLIC_FORM` in
     `tools/check_public_twins.py` to `"main"`, restore the Grok public hooks to the previous
     release's `@main` form, land that through a normal pull request, and repeat from step 2.
   - Hooks and MCP pass, but the skills take their `@main` fallback (Grok does not substitute
     the plugin path in skill bodies): not blocking. Record Grok as a fallback host for skills
     in the release notes.
6. **[Owner]** Delete the scratch repository.

If the portal does not accept the scratch repository, skip its Validate part and record that
in the release issue; the Grok gate still runs from the scratch repository. Validation then
happens on the released public `main`, and a remaining finding ships in the next release.

### Desktop smoke when requested

Run this check only when the owner requests it for a particular release. Start just after
step 9 and finish before tagging. Record the pushed public snapshot commit, app/OS,
installation route, expected results and observed evidence. Before starting, create a
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
   version equality alone cannot distinguish same-version re-cuts. Match the installed
   plugin source commit to the snapshot, or compare hashes of its loaded manifests, MCP
   configuration, hooks and skills with that snapshot if install metadata has no commit.
   Locate the running MCP server's Python environment from its process information. Read
   its `site-packages/sidegraph-*.dist-info/direct_url.json`, rather than a project Python
   installation. For Claude, that file is an `archive_info` URL naming the wheel under the
   installed plugin's `runtime/` folder, and the installed plugin version must equal the
   candidate version. For Codex, its `vcs_info.commit_id` must match the pushed snapshot, and
   for SessionStart you immediately read the
   [launch-commit file](hooks.md#the-launch-commit-file) it records from its own source
   metadata. That value must match the snapshot too. Recheck it before each hot-hook check.
   Run surfaces one at a time with other Sidegraph sessions closed, because the file is
   shared across projects. A launch warning, missing artifact or unresolved source identity
   is incomplete evidence. For Claude, retain the installed-plugin entry's commit too.
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
All applicable checks in a requested smoke must pass before tagging. Skipped, unsupported
or unobserved checks do not count as passes. The Codex read-hook item is not applicable.
The macOS live evidence does not establish native Windows desktop-app results.

If a requested check fails, stop before tagging. Keep the same version and dated release
heading, put the fix's changelog entry under that heading, and repeat steps 2–9 through a
new release-preparation pull request, merged internal main and public snapshot. This
includes the tests, private suite, pre-commit, build, wheel smoke and online preflight.
Then repeat this whole desktop smoke against the new snapshot.

### The public main moves only at releases

The Codex, Muse and OpenCode manifests install from the public repository's `main`. The MCP
server and `SessionStart` let `uv` re-resolve that branch reference at each start, and
`SessionStart` records the commit it got for the `Stop`, `PreToolUse` and `SubagentStart`
hooks to launch from, so pushing `main` reaches those users at their next session start, on
their machine, in any project. It also reaches Claude installs older than the bundled
runtime, which still launch from `main`. Newer Claude installs run the wheel bundled in the
plugin and update only when the plugin version changes. For that reason the public `main` moves only as part of a release: `tools/release-public.sh --push`
refuses to push it unless the version being published is untagged in the public repository
and `CHANGELOG.md` dates it, checked before anything is written and again right before the
push. A docs-only or other non-release change waits for the next release, or ships in an
urgent patch release.

Two consequences follow. First, bump the version in `pyproject.toml` and date its
`CHANGELOG.md` heading only as part of release preparation (steps 1 and 2 above). Once both
are on `main`, every snapshot counts as a release until the tag exists, so doing them ahead of
the release defeats the guard. Second, re-cutting a version whose tag-driven publish failed still needs
its tag deleted in both the public checkout and its origin before the next `--push`;
otherwise the push is refused.

Between the release-preparation commit and the tag, every `--push` is a re-cut of that same
release, and the tag goes on the last one. Start any requested desktop smoke immediately
after the push, then tag promptly once the required checks pass, so the window stays short.

Never push the public `main` by hand: the gate lives in `tools/release-public.sh`, and a
manual `git push` from the public checkout bypasses it.

## Tag-driven publish flow

PyPI publishing is driven entirely by a git tag. There is deliberately no manual dispatch: a
manual run has no tag ref, and the version guard below only means something against a real
one. Cut the release with:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

**Run that in the public checkout, not the internal one.** PyPI's trusted publisher is
configured against `SantyagoSeaman/sidegraph`, so a tag pushed to the internal repository
produces a valid OIDC token with the wrong `repository` claim and the publish job dies on
`invalid-publisher` — after the whole test matrix has run. The public snapshot is therefore
cut and pushed *first*, and the tag goes on the commit that snapshot created. Pushing the
release-prep branch and merging its Ready PR to internal `main` triggers CI,
but no publish.

That push triggers [`.github/workflows/publish.yml`](../../.github/workflows/publish.yml),
three jobs in sequence.

1. **`test`**: reuses `ci.yml` wholesale (pre-commit lint plus the pytest matrix; a tag push
   is not a pull request, so the full matrix always runs). Build and publish never run if
   this fails.
2. **`build`**: checks out the tag, then runs the tag-to-version guard. It reads
   `[project].version` out of `pyproject.toml` with `tomllib`, strips the leading `v` off
   `GITHUB_REF_NAME`, and fails the job if the two strings disagree. This check is
   unconditional, not gated on the tag pattern, so a future trigger that isn't a real tag push
   fails loudly instead of silently shipping whatever `pyproject.toml` happens to say. Then
   `uv build` runs, and the sdist and wheel upload as the `dist` artifact.
3. **`publish`**: downloads that artifact and pushes it to PyPI via Trusted Publishing (OIDC,
   `pypa/gh-action-pypi-publish`), with no stored API token. Gated on the `pypi` GitHub
   Environment.

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
- `uv tool install sidegraph==X.Y.Z` (or `uvx --from sidegraph==X.Y.Z sidegraph-doctor
  --help`) succeeds against the real published package, not the local checkout.
- The plugin marketplace path still installs from the public `main` (plugin manifests do not
  switch to the release tag; the Codex and Muse manifests launch from `@main`, and the Claude and
  Grok manifests launch the runtime bundled in the plugin):
  `/plugin marketplace add SantyagoSeaman/sidegraph` then `/plugin install
  sidegraph@sidegraph` inside Claude Code. This is the deliberate plugin exception to the
  normal [pinning guidance](../getting-started/installation.md#mutable-development-references).

- After `publish.yml` succeeds, run `tools/verify-release.sh --after-publish vX.Y.Z`. It
  clones the public repository at the tag, downloads the PyPI wheel of that version, and
  checks that the wheel bundled under `plugin/sidegraph/runtime/` holds the same package
  bytes. It cannot run earlier, because PyPI has no wheel for the version until the publish
  finishes.

The internal maintainer checkout also carries a network-bound install smoke script
(`tools/verify-release.sh`; it is intentionally absent from the public snapshot) that exercises
`sidegraph-init`, `sidegraph-bootstrap --help`, an MCP `initialize` handshake, and the
Codex plugin hooks' `@main` install ref, all against the real published source. It is deliberately
not wired into CI, since it clones and installs over the network. The maintainer runs it by
hand after the snapshot push, using its default `main` ref. A tag argument tests package
installation at that tag but cannot assert that mutable plugin refs changed to it, because
they intentionally do not.

## The public snapshot

Release preparation happens in the private development repository; tagging and the publish
workflow run in the public repository. `sidegraph` on GitHub is a **published snapshot**, cut
from a private development repository by an allowlist-only script. Every release replaces
the snapshot's tree wholesale
with exactly the paths that script names, nothing else. When published, a `demo` branch carries
the same snapshot plus a sample `.sidegraph/` decision store, so you can browse an
example of the store without running anything. See
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) for what that means for a pull request here.
