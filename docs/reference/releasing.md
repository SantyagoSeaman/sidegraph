# Releasing Sidegraph

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

`README.md` is deliberately absent from this table. Its Status section carries no version
string — only a PyPI badge that reads the live index — so there is nothing there to bump and
nothing that can go stale between releases. It used to carry a `vX.Y.Z, on PyPI as ...` line;
that line was removed in 0.3.0, and this note replaces its row.

`tests/test_codex_plugin.py` enforces that the two plugin manifests and
`src/sidegraph/__init__.py` all agree with `pyproject.toml`; a release with a stale copy
fails the test suite rather than shipping silently. Find any remaining stragglers before a
release with `grep -rn "0\.1\.0" . --include=*.md --include=*.toml --include=*.py
--include=*.json` from the repo root (excluding `.venv/` and `uv.lock`), then substitute the
actual current version. Historical mentions (old CHANGELOG entries, dated design notes, store
`schema_version` fixtures) are not part of this list. Only the ones above assert the *current*
package version.

## Pre-release checklist

Run from a clean `main`, in this order.

0. `uv run python tools/preflight_release.py`. Seconds, no network. It checks what the rest
   of this checklist cannot: that every restatement of the version agrees with
   `pyproject.toml`, that the changelog carries a dated heading with a fresh `[Unreleased]`
   above it, that both checkouts are clean and on `main`, that the tag does not already
   exist — and, above all, **which repository the tag belongs to**, derived from the
   checkout rather than assumed. Add `--online` for the two remote lookups. It reports every
   finding in one run rather than stopping at the first, so a release is prepared in one
   sitting instead of five.

1. `uv run pytest -q`. Full suite green.
2. `uv run pre-commit run --all-files`. Lint, format, and types clean; this is exactly what
   CI runs, and now also gitleaks, zizmor, actionlint, and the shipped-surface/public-twin/
   docs-link checkers described in `CONTRIBUTING.md`.
3. `CHANGELOG.md`: turn the `## [Unreleased]` heading (or, before the first release, the top
   `## [0.1.0] — Unreleased` heading) into a dated release heading in the same style,
   `## [X.Y.Z] — YYYY-MM-DD`, and add a fresh empty `## [Unreleased]` section above it for
   whatever lands next. Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/):
   group entries under `Added`, `Changed`, `Fixed`, and so on. If no `## [Unreleased]`
   heading survived from the previous release (0.1.0 through 0.2.0 shipped with none), write
   the new dated heading directly above the previous release's own heading instead of
   transforming anything, then add the fresh empty `## [Unreleased]` section above that, so
   the placeholder exists again for next time.
4. Confirm every file in the version table above already carries the new version, and that
   any behaviour change shipping in this release has its matching page under `docs/` updated
   in the same change (see `CONTRIBUTING.md`).
5. `uv build`. Must succeed and produce both `dist/sidegraph-X.Y.Z.tar.gz` and
   `dist/sidegraph-X.Y.Z-py3-none-any.whl`.
6. A real-install smoke test against the freshly built wheel, before anything is tagged:
   `uvx --from ./dist/sidegraph-X.Y.Z-py3-none-any.whl sidegraph-doctor --help`. This confirms
   the package actually installs standalone and its console scripts resolve, which `pytest`
   alone (running against the editable checkout) does not exercise.

Only once all six pass is the tree ready to tag.

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
internal `main` is a separate step that triggers no publish at all.

That push triggers [`.github/workflows/publish.yml`](../../.github/workflows/publish.yml),
three jobs in sequence.

1. **`test`**: reuses `ci.yml` wholesale (pre-commit lint plus the pytest matrix). Build and
   publish never run if this fails.
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
- The plugin marketplace path still installs from the tag it now points at:
  `/plugin marketplace add SantyagoSeaman/sidegraph` then `/plugin install
  sidegraph@sidegraph` inside Claude Code.

The internal repo also carries a network-bound install smoke script
(`tools/verify-release.sh`, not part of the public snapshot, see below) that exercises
`sidegraph-init`, `sidegraph-bootstrap --help`, an MCP `initialize` handshake, and the
released plugin hooks' install ref, all against the real published source. It is deliberately
not wired into CI, since it clones and installs over the network. The maintainer runs it by
hand after a push.

## The public snapshot

None of this runs against the GitHub repository you are reading this on, if you are reading
it there. `sidegraph` on GitHub is a **published snapshot**, cut from a private development
repository by an allowlist-only script. Every release replaces the snapshot's tree wholesale
with exactly the paths that script names, nothing else. A `demo` branch carries the same
snapshot plus a sample `.sidegraph/` decision store, so you can browse a real (if small)
example of what the store looks like without running anything. See
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) for what that means for a pull request here.
