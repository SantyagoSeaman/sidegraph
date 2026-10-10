# Contributing

Thanks for considering a contribution. The project is young and small — short, focused
issues and patches land fastest.

## How this repository works

This repository is a **published snapshot**, not the development repository. Each release
replaces its tree wholesale from the upstream working repo. That has one practical
consequence for you:

- **Issues are the best channel** — bug reports, feature requests and questions are read here.
- **Pull requests are ported upstream, not merged here.** A merge commit on this repo would be
  erased by the next release. Open the PR anyway if you have a patch: it will be reviewed here,
  applied upstream with attribution, and it will arrive back in this repo with the next release.

## Development setup

```bash
git clone https://github.com/SantyagoSeaman/sidegraph.git && cd sidegraph
uv sync                        # runtime + dev dependencies (Python 3.13+)
uv run pre-commit install      # once per clone — the lint gate runs before each commit
uv run pytest tests/test_store.py -q -x --lf   # while editing: the touched file
uv run pytest -m "not slow"   # broader pre-push check: skips subprocess-heavy tests, still takes minutes
uv run pytest -q               # full suite: run before a commit or PR, must be green; CI runs it again
uv run pre-commit run --all-files   # lint + format + types + staged secrets
```


The Gitleaks commit hook scans staged content even when all changed paths are excluded from
formatting, including `.sidegraph/`. `--all-files` does not turn it into a committed scan.
CI additionally scans all history reachable from HEAD with the same pinned scanner:

```bash
uv run pre-commit run gitleaks-history --hook-stage manual --all-files
SIDEGRAPH_SECRET_SCAN_INTEGRATION=1 uv run pytest tests/test_secret_scan_gate.py -q
```

The history scanner includes merge changes and traverses every commit reachable from HEAD.
Use a full checkout; CI rejects shallow history before scanning.

The second command runs real scanner integration tests; they are skipped in ordinary Python
test runs to avoid implicit scanner installation. The opt-in is read at collection time.
Findings are redacted. The history command needs a full Git checkout to cover earlier commits.

A passing test's `tmp_path` tree is removed; a failing test's is kept for inspection
(`tmp_path_retention_policy = "failed"` in `pyproject.toml`). Run with
`-o tmp_path_retention_policy=all` to keep every test's tree.

The suite is hermetic to `SIDEGRAPH_*` environment variables: `tests/conftest.py` strips every
one of them before each test runs, so a variable already set in your shell can never change what
the suite asserts. A test that needs one set must set it explicitly with `monkeypatch` rather
than relying on inheriting it.

That gate also runs secret detection (`gitleaks`, `detect-private-key`), GitHub Actions
security/correctness checks (`zizmor`, `actionlint`), and `check-toml`/a `uv.lock`-in-sync
check, alongside the usual formatting and type checks.

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

## Reporting bugs

Open a GitHub issue with: what you ran, what you expected, what happened, and your
`sidegraph`/`graphify` versions. For security issues, see [SECURITY.md](SECURITY.md).
