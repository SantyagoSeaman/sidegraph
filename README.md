# Sidegraph

[![tests](https://img.shields.io/github/actions/workflow/status/SantyagoSeaman/sidegraph/ci.yml?label=tests)](https://github.com/SantyagoSeaman/sidegraph/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sidegraph)](https://pypi.org/project/sidegraph/)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)
[![Windows plugin capture and recall checked](https://img.shields.io/badge/Windows-plugin%20capture%20%2B%20recall%20checked-blue)](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#platform-coverage)
[![macOS arm64 full plugin session checked](https://img.shields.io/badge/macOS%20arm64-full%20plugin%20session%20checked-blue)](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#platform-coverage)

Platform badges summarize the release checks of 0.12.0, verified on 2026-10-12;
they are not live CI status. What each check covers is in
[Platform coverage](docs/getting-started/installation.md#platform-coverage).

Sidegraph is a local MCP server and CLI for preserving engineering decisions and
facts alongside your code. It records why a design was chosen, which alternatives were
rejected, and what was learned during implementation, then retrieves that context for
the files and symbols an agent is working on.

Records live in your repository as reviewable JSON files. A derived SQLite index supports
retrieval; [Graphify](https://github.com/safishamsi/graphify) supplies the code and document
graph used to anchor records. Claude Code and Codex plugins connect capture and retrieval
to coding sessions.

- Keep decisions, rejected alternatives, and evidence with their provenance and history.
- Retrieve relevant mistakes and constraints before editing, within a character budget.
- Review agent proposals before accepting them; manual ratification is the default.
- Detect changed code and unresolved anchors for review after refactors.

[Documentation](docs/README.md) · [Installation](docs/getting-started/installation.md) ·
[Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

## Quickstart

You need Python 3.13+, [uv](https://docs.astral.sh/uv/), and a repository to work in.
See [installation](docs/getting-started/installation.md) for platform limitations and
manual MCP configuration. Existing ADRs and API keys are not required for the local workflow.

**Windows:** native x64 CI checks the portable core and installed-wheel MCP stdio on
Python 3.13/3.14, and each release runs real Claude Code and Codex plugin sessions there
(installation, capture and fresh-session recall). The supplied plugins require a POSIX
shell for their MCP launchers and hooks, and Codex hooks need PowerShell 7. The manual
recipes use Bash; run them inside WSL. See
[Windows setup and release caveats](docs/getting-started/installation.md#windows).

Start with the plugin: connect your agent using one of the host guides:

| Host | Setup |
|---|---|
| Claude Code | [Plugin installation and verification](docs/getting-started/claude-code-setup.md) |
| Codex | [Plugin installation and verification](docs/getting-started/codex-setup.md) |
| Other MCP clients | Run `sidegraph-mcp` over stdio (`python -m sidegraph.server` is the same server without host identity, so session ids stay null); see the [MCP reference](docs/reference/mcp-tools.md) and [configuration](docs/reference/configuration.md). Automatic hooks depend on the host. |

Then install the verified graph-engine version and initialize your repository. The commands run
Sidegraph through `uvx`, the way the plugin does, so nothing else needs installing (`@main` is a mutable ref; to pin it see
[mutable development references](docs/getting-started/installation.md#mutable-development-references)):

```bash
uv tool install "graphifyy==0.9.6"  # Package name has two y's; the command is graphify.
cd /path/to/your/repo
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

(`uv tool install sidegraph` puts the bare `sidegraph-*` names on your `PATH`. It is optional
beside a plugin and the usual setup without one. It never updates itself, so run
`uv tool install sidegraph@latest` after every release; see
[installation](docs/getting-started/installation.md#option-a--the-cli-on-your-path-pypi).)

`sidegraph-init` creates `.sidegraph/` and prints setup guidance. In an interactive
terminal it also offers graph-refresh hooks and a ratification policy; review those
choices before enabling them. Graphify's optional `[mcp]` extra installs its separate
MCP server and is not needed by Sidegraph.

In a connected session, ask the agent to record a decision, for example:

> Record this gotcha: the upstream API limits batch requests to 100 items. We rejected
> a single unbounded request because it fails for larger imports. Anchor it to
> `import_batch` in `src/importer.py`.

Replace the example with a finding and file from your project. Ask for the context for
that file in a new session to verify retrieval. Commit the canonical `.sidegraph/`
files with the related code; its derived index is gitignored. The
[verification checklist](docs/guides/verifying-your-setup.md) covers the full workflow.

Next, [name your domains](docs/guides/naming-your-domains.md) to create a project table
of contents. If you already have ADRs or design specs, use the
[preview-first bootstrap](docs/getting-started/bootstrap.md) to import them.

## Why

Code describes the current implementation, but often omits the alternatives a team
tried, operational constraints it discovered, and reasons for changing direction.
Sidegraph captures that reasoning and links it to the code and planning documents it
concerns. It works alongside an existing ADR or specification process.

The useful starting point is a project with decisions worth retaining and people willing
to review them. Memory quality depends on capture, review, and whether the agent actually
retrieves it. Small tasks answerable directly from code may gain little from the extra
context. The [engineering whitepaper](docs/whitepaper/index.md) describes the evidence,
negative results, and adoption criteria; the [pilot kit](docs/pilot-kit/README.md) helps
evaluate it on your own repository.

## How it works

```
 you work a session ──▶ Stop hook nudges the agent to distill durable decisions
                                │ propose_decisions / propose_domains (secrets redacted)
                                ▼
                        you ratify / drop  ──▶  append-only decision log,
                                                 committed with your repo
                                                        │
 next session ◀── SessionStart TOC            anchored to entities in the
 of named domains ◀── get_task_context ◀────── engine's graph (read-only);
 mistakes first        drill_down             re-anchored after refactors
 Read / Edit / Bash read ──▶ the file's records delivered in the call (once per file per agent)
```

Subagents get the memory too: on Claude Code a hook appends the decisions for the files a
subagent's brief names to that brief, and on Claude Code and Codex another hook tells each new
subagent that decision memory exists and how to ask it.

The graph describes code and document structure and can be rebuilt from source. The
store preserves decisions independently of that graph. Anchors identify functions,
classes, files, or document headings rather than line numbers; synchronization follows
confirmed moves and reports ambiguous or missing matches for review.

Named **domains** group related records and code into a described table of contents.
Their membership is based on durable anchors and path rules. See the
[mind model](docs/concepts/mind-model.md) and [anchoring model](docs/concepts/anchoring.md).

## What gets stored

| Record | What it is |
|---|---|
| `Decision` | Kind (`adr`, `lesson`, `constraint`, or `gotcha`), context, choice, rejected alternatives, consequences, validity, history, and provenance |
| `Fact` | A benchmark, external constraint, or observed finding that supports a decision or stands alone |
| `Entity` | Durable identity for a code/doc entity — survives the engine's shifting node ids |
| `AnchorBinding` | Links between decisions or facts and the entities they concern |
| `Domain` | A named area with a purpose, membership rules, and optional subdomains |

Append-only is a feature: a reversed decision is closed and superseded, never deleted —
*"tried before, abandoned because…"* stays retrievable via `get_entity_history`.

Facts follow the same append-only and ratification rules as decisions; ratifying or
dropping a decision carries its still-pending facts along in the same verdict. Details:
[docs/concepts/data-model.md](docs/concepts/data-model.md) and
[docs/guides/capturing-decisions.md#facts-the-evidence-layer](docs/guides/capturing-decisions.md#facts-the-evidence-layer).

## MCP tools

The `sidegraph-mcp` stdio server exposes retrieval, capture, ratification, and maintenance
tools. Start with `get_task_context` for files and symbols, `drill_down` for a domain,
and `get_entity_history` for a decision chain. `propose_decisions` creates reviewable
drafts; `ratify` accepts or drops proposals.

See the [MCP reference](docs/reference/mcp-tools.md) for the complete tool set, schemas,
return shapes, and side effects. The [CLI reference](docs/reference/cli.md) covers setup,
scripted use, and CI maintenance. Tool names and return shapes are
[provisional before 1.0](docs/reference/stability.md).

## Works on code and on docs

Anchor decisions to functions and classes — or to **headings in your architecture
markdown** (LLM-free graph build, non-git folders supported). `sidegraph-bootstrap` parses
existing ADRs and design specs into anchored decisions deterministically, no LLM.

An optional **semantic pass** (`graphify extract`, one API key, cached per file) goes a
layer deeper on documentation: prose becomes `concept` nodes and thematic clusters, giving
retrieval a richer graph to anchor against. Walkthrough:
[docs/guides/semantic-docs.md](docs/guides/semantic-docs.md).

## Trust & privacy

- **Local runtime:** Sidegraph reads repository files and the graph, and writes records,
  a derived index, and optional setup or export files. It makes no network calls and
  sends no remote telemetry. Installation and updates download packages; your agent host
  controls where MCP results are sent. Optional Graphify semantic analysis uses its
  configured model provider. See [SECURITY.md](SECURITY.md) for the data boundary.
- **Reviewable capture:** text is redacted before storage using best-effort patterns.
  Review drafts and scan the committed store for secrets. Agent proposals wait for human
  ratification by default; automatic ratification is an
  [explicit policy choice](docs/reference/configuration.md).
- **Preserved history:** reversals close a record and append a successor. Ratification
  updates status, and confirmed file moves may update anchor descriptors. Local usage
  diagnostics live in the gitignored index; `SIDEGRAPH_TELEMETRY=off` disables recording.

## The engine underneath

Entity extraction and graph construction come from
[Graphify](https://github.com/safishamsi/graphify) (its LLM-free build covers both code
and markdown); Sidegraph never re-implements them or writes into the engine's output.
The engine is optional at runtime: without a graph, records anchor to file paths and
domains and retrieval still works, but symbol-level anchors, communities, and moved-code
resolution need it (see the [operations reference](docs/reference/operations.md#the-graph-dependency-stated-plainly)).
Graph output and the local SQLite index are derived data. Canonical JSON records and
archives in `.sidegraph/` are the durable source of truth; include them in your repository
and backup policy.

## Status

Sidegraph is in beta. Read the [stability policy](docs/reference/stability.md) before
building automation against it, and pin a package version or commit for reproducible
deployments. The [changelog](CHANGELOG.md) records release changes.

## Contributing and support

Bug reports, questions, and feature requests are welcome in
[GitHub Issues](https://github.com/SantyagoSeaman/sidegraph/issues). Include the commands
you ran, expected and actual behavior, and package versions; remove secrets from logs.
For patches, see [Contributing](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities through the private
channel in [SECURITY.md](SECURITY.md).

The public repository is a release snapshot. Contributions are reviewed there and
ported to the development repository with attribution before the next release.

## Documentation

[Getting started](docs/getting-started/installation.md) ·
[Concepts](docs/concepts/decision-memory.md) ·
[Guides](docs/guides/capturing-decisions.md) ·
[Reference](docs/reference/mcp-tools.md) ·
[Integrations](docs/integrations/graphify.md) (Graphify · Claude Code · Codex) ·
[Verify your setup](docs/guides/verifying-your-setup.md) ·
[Operations](docs/reference/operations.md) ·
[Pilot kit](docs/pilot-kit/README.md) ·
[Engineering whitepaper](docs/whitepaper/index.md)

## License

[Apache-2.0](LICENSE).
