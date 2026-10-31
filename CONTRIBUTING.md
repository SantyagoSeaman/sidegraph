# Contributing

Contributions are welcome: bug reports, documentation improvements, tests, and code
changes. For substantial changes, open an issue first to discuss the problem and scope.
By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md).

## Your first contribution

The lowest-risk ways in are a documentation fix, a missing test, or a bug report with a
reproduction. Issues labelled
[`good first issue`](https://github.com/SantyagoSeaman/sidegraph/labels/good%20first%20issue)
or [`help wanted`](https://github.com/SantyagoSeaman/sidegraph/labels/help%20wanted), when there are any, are
scoped for someone new to the code. Use the
[issue forms](https://github.com/SantyagoSeaman/sidegraph/issues/new/choose) for bugs and
feature requests.

## How this repository works

This repository is a **published snapshot**, not the development repository. Each release
replaces its tree wholesale from the upstream working repository. In practice:

- **Issues are the best channel.** Bug reports, feature requests and questions are read
  here, and a first response usually comes within a few days.
- **Pull requests are reviewed here and ported upstream, not merged here.** A change
  applied only to this snapshot would be overwritten by the next release, so the reviewed
  patch is applied upstream instead. You stay credited as its author, and the PR gets the
  `status:awaiting-release` label until the release that contains it.
- **Your change comes back with the next release.** The release replaces this tree, the
  PR is closed with a link to that release, and the CHANGELOG entry describes the change.

## Development setup

```bash
git clone https://github.com/SantyagoSeaman/sidegraph.git && cd sidegraph
uv sync --locked               # runtime + dev dependencies (Python 3.13+)
uv run pre-commit install      # once per clone: the lint gate runs before each commit
uv run pytest tests/test_store.py -q -x --lf   # while editing: the test file for your area
uv run pytest -m "not slow"   # broader pre-push check: skips subprocess-heavy tests, still takes minutes
uv run pytest -q               # full suite: run before a commit or PR, must be green; CI runs it again
uv run pre-commit run --all-files   # lint, format, types and secret detection
```

This block is the one list of setup, test and lint commands; `AGENTS.md` and `CLAUDE.md` point here.

A passing test's `tmp_path` tree is removed; a failing test's is kept for inspection
(`tmp_path_retention_policy = "failed"` in `pyproject.toml`). Run with
`-o tmp_path_retention_policy=all` to keep every test's tree.

The suite is hermetic to `SIDEGRAPH_*` environment variables: `tests/conftest.py` strips every
one of them before each test runs, so a variable already set in your shell can never change what
the suite asserts. A test that needs one set must set it explicitly with `monkeypatch` rather
than relying on inheriting it.

## Preparing a pull request

Describe the problem, the resulting behavior, and the checks you ran. Link the related
issue when there is one, keep the patch focused, and update affected documentation.
Use an area-prefixed imperative commit subject, for example `store: preserve history`,
and sign off each commit as described below.

CI runs lint on every PR push. On a pull request that is ready for review (not a draft),
on `main` and before publishing, it also runs the full suite on Linux with Python 3.13 and
3.14 and a selected native Windows core/MCP suite on both versions; see
[platform coverage](docs/getting-started/installation.md#platform-coverage). If your change
touches paths, permissions, subprocesses, or shell commands, describe any additional
platform checks you performed.

## Ground rules

- **Tests first for behavior changes.** Schema invariants and store write-path rules are
  the contract — a change without a test asserting the new behavior won't be taken.
- **Respect the invariants** (see `CLAUDE.md`): the store is append-only; the engine's
  `graph.json` is read-only input; engine specifics stay confined to
  `src/sidegraph/engine/`; host specifics to `src/sidegraph/host/`.
- **Match the surrounding style.** Type hints everywhere; terse docstrings. Formatting and
  lint are mechanical — `pre-commit` decides, not review.
- **Docs are part of the change.** If a flag, tool, or behavior changes, update the
  matching page under `docs/` in the same change.
- **Never publish a session link.** No `Claude-Session:` trailer, no claude.ai/chatgpt.com
  session URL, no bare `session_*` id, in a commit message or a PR description — enforced by
  the `no-session-links` commit-msg hook and CI's `pr-description-link-gate` job.

Cutting an actual release (version bumps, the tag-driven PyPI publish, post-release checks)
is a maintainer task: see [`docs/reference/releasing.md`](docs/reference/releasing.md).

## Sign-off (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/). Add
`Signed-off-by: Your Name <you@example.com>` to each commit (`git commit -s`).

## Security gates

`uv run pre-commit run --all-files` runs secret detection (`gitleaks`, `detect-private-key`),
GitHub Actions security and correctness checks (`zizmor`, `actionlint`), `check-toml` and a
`uv.lock`-in-sync check, alongside the formatting and type checks.

The Gitleaks commit hook scans staged content even when every changed path is excluded from
formatting, including `.sidegraph/`; `--all-files` does not turn it into a scan of committed
history. CI additionally scans all history reachable from `HEAD`, merge changes included,
with the same pinned scanner. Findings are redacted. To run that scan yourself, use a full
checkout (CI rejects shallow history):

```bash
uv run pre-commit run gitleaks-history --hook-stage manual --all-files
```

## Reporting bugs

Open a GitHub issue with: what you ran, what you expected, what happened, and your
`sidegraph`/`graphify` versions. For security issues, see [SECURITY.md](SECURITY.md).
