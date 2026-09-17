<!--
This file ships in two places. On the internal repo, this PR is the normal path to merge.
On the public repo (a released snapshot, see CONTRIBUTING.md), this PR is a proposal: it is
reviewed here and ported upstream with attribution, then arrives back with the next release.
Either way, fill in the same two sections.
-->

## What changed and why

<!-- One or two sentences: what this PR does and the problem it solves. -->

## Checklist

- [ ] Tests pass (`uv run pytest -q`)
- [ ] `uv run pre-commit run --all-files` is clean
- [ ] Docs under `docs/` are updated for any behaviour change
- [ ] A CHANGELOG entry is added under `[Unreleased]` for any user-visible change
- [ ] No hand edits to records under `.sidegraph/` (the store is append-only and written by
      the tools, never by hand)
- [ ] Any new dependency is called out and justified in this description
