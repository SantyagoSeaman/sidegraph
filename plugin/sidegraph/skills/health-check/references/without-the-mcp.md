# Reading memory without the MCP

The store is plain text under `.sidegraph/`, and a root `.ignore` keeps recursive search (`rg`,
editor search) out of it, so an agent that has no MCP does not stumble on records by searching.
Read them through the command line instead, and run the commands by the pinned launch:

- `uvx --from sidegraph==0.29.0 --python 3.13 sidegraph-export-adr --json` prints the ADRs and
  constraints.
- `uvx --from sidegraph==0.29.0 --python 3.13 sidegraph-export-okf --out <dir>` writes every
  ratified record, with its history, into `<dir>`.
- `uvx --from sidegraph==0.29.0 --python 3.13 sidegraph-doctor` checks the store and lists what
  needs attention.

Inside a Sidegraph checkout, use `uv run --project <checkout> --package sidegraph --frozen
--no-active <cli>` instead.

The raw files are not a substitute for these: a packed shard line can hold an older image of a
record that a per-record file has since superseded, and drift is not in the files at all. To
search the store by hand anyway, name its path: `rg PATTERN .sidegraph/`.
