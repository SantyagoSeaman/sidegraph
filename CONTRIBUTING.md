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
uv run pytest -q               # full suite must be green
uv run pre-commit run --all-files   # lint + format + types, exactly what CI runs
```

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
