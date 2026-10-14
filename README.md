# Sidegraph

[![tests](https://img.shields.io/github/actions/workflow/status/SantyagoSeaman/sidegraph/ci.yml?label=tests)](https://github.com/SantyagoSeaman/sidegraph/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/SantyagoSeaman/sidegraph)](https://github.com/SantyagoSeaman/sidegraph/releases)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)
[![Windows x64 full plugin session checked](https://img.shields.io/badge/Windows%20x64-full%20plugin%20session%20checked-blue)](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#platform-coverage)
[![macOS arm64 full plugin session checked](https://img.shields.io/badge/macOS%20arm64-full%20plugin%20session%20checked-blue)](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#platform-coverage)

Sidegraph is a local MCP server and CLI for preserving engineering decisions and
facts alongside your code. It records why a design was chosen, which alternatives were
rejected, and what was learned during implementation, then retrieves that context for
the files and symbols an agent is working on.

Records live in your repository as reviewable JSON files. A derived SQLite index supports
retrieval; [Graphify](https://github.com/safishamsi/graphify) supplies the code and document
graph used to anchor records. Claude Code and Codex plugins connect capture and retrieval
to coding sessions.

- Keep decisions, rejected alternatives, and evidence with their provenance and history.
- Hand the agent the relevant mistakes and constraints before it edits, sized to fit its context.
- Review agent proposals before accepting them, or let low-risk ones be accepted automatically.
- Follow code through refactors, and flag records whose code changed or disappeared.

[Documentation](docs/README.md) · [Installation](docs/getting-started/installation.md) ·
[Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

## Quickstart

### Set up a repository (once)

**You need** Git, Claude Code or Codex, and [uv](https://docs.astral.sh/uv/getting-started/installation/),
which also downloads Python 3.13 if you do not have it. On Windows you also need
[Git for Windows](https://gitforwindows.org/) with its default options
([details](docs/getting-started/installation.md#windows)); run the commands below in
PowerShell, using the Windows line where one is given. No API keys and no existing ADRs are required.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh                                     # uv on macOS, Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # uv on Windows
```

**1. Build the code graph.** [Graphify](https://github.com/safishamsi/graphify) maps your
functions, classes and files, so a record can point at the exact function it is about.

```sh
uv tool install "graphifyy==0.9.6"   # two y's in the package name; the command is graphify
cd /path/to/your/repo
graphify update .
echo "graphify-out/" >> .gitignore   # the graph is rebuilt from your code; keep it out of git
Add-Content .gitignore "graphify-out/"   # the same line on Windows (PowerShell); do not use >> there
```

**2. Create the store**, the `.sidegraph/` directory that holds the records:

```sh
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

It asks two questions. Both default to yes, so press Enter twice.

- *Keep the code graph fresh?* Yes installs a git hook that rebuilds the graph after each commit.
- *Auto-ratify low-risk records?* Yes means the lessons, gotchas and facts the agent proposes
  are accepted without review. Other proposals still wait for you. The answer is saved in
  `.claude/settings.json`, which you commit. The plugin applies it in Claude Code and in Codex,
  so it covers the whole team.

Details: [`sidegraph-init`](docs/reference/cli.md#sidegraph-init).

**3. Install the plugin.** It gives the agent the memory at session start and asks it to save
new decisions at the end of a session. It updates itself: each new session downloads and runs
the latest Sidegraph release.

In Claude Code, run these inside a session in the repository, then start a new session:

```text
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

In Codex, run these in a terminal in the repository, then start `codex` and approve the
Sidegraph hooks when it asks:

```sh
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

**4. Record a decision.** In a session, ask the agent, naming a real file and function:

> Record this gotcha: the upstream API limits batch requests to 100 items. We rejected
> a single unbounded request because it fails for larger imports. Anchor it to
> `import_batch` in `src/importer.py`.

It is saved right away. Records the agent proposes on its own may wait for your review: run
`/sidegraph:ratify-decisions` in Claude Code, or ask Codex to "review pending decisions".

**5. Check and commit.** Start a new session and ask: "Before I edit `src/importer.py`, what
should I know?" The agent should show the gotcha. Then commit `.sidegraph/` and `.gitignore`,
and `.claude/settings.json` if step 2 created or changed it. The index file inside
`.sidegraph/` is git-ignored automatically.

### Each teammate

After cloning, each teammate runs steps 1, 2 and 3. In a clone, step 2 leaves the committed
records and settings alone. It only installs the graph hook there.

Next, [name your domains](docs/guides/naming-your-domains.md) to give the memory a table of
contents, or import existing ADRs and design specs with the
[preview-first bootstrap](docs/getting-started/bootstrap.md). The
[verification checklist](docs/guides/verifying-your-setup.md) covers the whole workflow.

### Other options

- **Without the graph.** Skip step 1 and answer no to the first question in step 2. Records
  then attach to whole files instead of functions.
- **Versions.** You always run the latest release. The plugin and the step 2 command install
  Sidegraph from the public repository on GitHub, whose `main` branch changes only when a
  release is published. To pin a version instead, see
  [pinning](docs/getting-started/installation.md#mutable-development-references).
- **Sidegraph commands in your own terminal.** Optional:
  `uv tool install git+https://github.com/SantyagoSeaman/sidegraph.git@main`, from the same place as the plugin. It does not update itself, so
  after each release run `uv tool upgrade sidegraph`
  ([details](docs/getting-started/installation.md#option-a--the-cli-on-your-path)).
- **Other MCP clients.** Run `sidegraph-mcp` over stdio; see the
  [MCP reference](docs/reference/mcp-tools.md) and [configuration](docs/reference/configuration.md).
  Automatic memory needs a host with hooks, such as Claude Code or Codex.
- **Windows details**, including what the plugins need from Git for Windows:
  [Windows setup](docs/getting-started/installation.md#windows).

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

1. **Capture.** During a session the agent saves a decision when you ask for one. At the end of
   a session the plugin reminds it to propose the decisions worth keeping. Secrets are redacted
   before anything is written.
2. **Review.** You accept or drop what the agent proposed; Sidegraph calls this *ratifying*.
   Records are JSON files in `.sidegraph/`, committed with your code.
3. **Anchoring.** Each record is linked to the functions, classes, files or document headings it
   is about, using the graph Graphify builds from your code. When a refactor moves code,
   Sidegraph follows the move; when it cannot tell where the code went, it flags the record for
   review.
4. **Recall.** At the start of the next session the agent gets a short table of contents of the
   project's areas, called *domains*. Before changing a file it asks for that file's records,
   with known mistakes first. In Claude Code, the agent also receives a file's records the
   first time it reads or edits that file.
5. **Subagents.** In Claude Code, when the agent hands a task to a subagent, the records for the
   files named in that task are added to it. In Claude Code and Codex, each new subagent is told
   that the memory exists and how to use it.

Domains are named by you or proposed by the agent; each one says what an area is for and which
code belongs to it. See the [mind model](docs/concepts/mind-model.md) and the
[anchoring model](docs/concepts/anchoring.md).

## What gets stored

| Record | What it is |
|---|---|
| `Decision` | Kind (`adr`, `lesson`, `constraint`, or `gotcha`), context, choice, rejected alternatives, consequences, validity, history, and provenance |
| `Fact` | A benchmark, external constraint, or observed finding that supports a decision or stands alone |
| `Entity` | A stable ID for a function, class, file or heading, kept when the graph is rebuilt |
| `AnchorBinding` | The link between a decision or fact and an entity it is about |
| `Domain` | A named area with a purpose, membership rules, and optional subdomains |

Nothing is ever deleted. A reversed decision is closed and replaced by a new one, so *"tried
before, abandoned because…"* stays retrievable with `get_entity_history`. Dropping a proposal
marks it rejected; it stays in the history too.

Facts follow the same review and history rules as decisions. Accepting or dropping a decision also accepts or drops
the pending facts attached to it. Details:
[docs/concepts/data-model.md](docs/concepts/data-model.md) and
[docs/guides/capturing-decisions.md#facts-the-evidence-layer](docs/guides/capturing-decisions.md#facts-the-evidence-layer).

## MCP tools

The `sidegraph-mcp` stdio server exposes retrieval, capture, review and maintenance tools.
The ones an agent uses most: `get_task_context` (the records for the files or functions it is
working on), `drill_down` (everything in one domain), `get_entity_history` (every decision
ever made about one function or file, including reversed ones), `propose_decisions` (drafts
for review) and `ratify` (accept or drop drafts).

See the [MCP reference](docs/reference/mcp-tools.md) for the complete tool set, schemas,
return shapes, and side effects. The [CLI reference](docs/reference/cli.md) covers setup,
scripted use, and CI maintenance. Tool names and return shapes are
[provisional before 1.0](docs/reference/stability.md).

## Works on code and on docs

Anchor decisions to functions and classes, or to **headings in your architecture documents**;
Graphify builds both graphs without an LLM. `sidegraph-bootstrap` turns existing ADRs and
design specs into anchored decisions, also without an LLM.

An optional **semantic pass** (`graphify extract`, which needs an API key for its model
provider and caches results per file) goes deeper on documentation: it turns prose into
concepts and themes that records can anchor to. Walkthrough:
[docs/guides/semantic-docs.md](docs/guides/semantic-docs.md).

## Trust & privacy

- **Local runtime:** Sidegraph reads repository files and the graph, and writes records,
  a derived index, and optional setup or export files. It makes no network calls of its own
  and sends nothing anywhere. Installing and updating it downloads it from GitHub. What the agent
  does with Sidegraph's answers is up to your agent host, which sends them to its model
  provider like any other context. Graphify's optional semantic pass uses its own model
  provider. See [SECURITY.md](SECURITY.md) for the data boundary.
- **Reviewable capture:** text is redacted before storage using best-effort patterns.
  Review drafts and scan the committed store for secrets. Agent proposals wait for human
  review unless the project answered yes to auto-ratify in `sidegraph-init`; even then,
  architecture decisions and domains wait. See the
  [ratification policy](docs/reference/configuration.md).
- **Preserved history:** a reversal closes the old record and adds a new one. Only two things
  change a record in place: review sets its status, and a confirmed file move updates where
  its anchor points.
- **Local counts:** for `sidegraph-stats`, Sidegraph counts how often memory was shown. The
  counts stay in `.sidegraph/index.db`, which is git-ignored and never leaves your machine.
  To stop counting, set `SIDEGRAPH_TELEMETRY=off`.

## The engine underneath

The graph of your code and documents comes from
[Graphify](https://github.com/safishamsi/graphify), built without an LLM. Sidegraph only reads
it and never writes into it. Without a graph, records anchor to files and domains, and recall
still works. Three things need the graph: anchoring a record to a function, grouping related
code automatically, and following moved code (see the
[operations reference](docs/reference/operations.md#the-graph-dependency-stated-plainly)).
The graph and the local index can be rebuilt at any time. The JSON records in `.sidegraph/`
cannot: they are the source of truth, so keep them in your repository and backups.

## Status

Sidegraph is in beta. Read the [stability policy](docs/reference/stability.md) before
building automation against it. By default the plugin follows the latest release; for
reproducible deployments, [pin a version](docs/getting-started/installation.md#mutable-development-references).
The [changelog](CHANGELOG.md) records release changes.

The platform badges at the top summarize the 0.13.0 release checks of 2026-10-14: Windows x64
and macOS arm64 both ran the full plugin session for Claude Code and Codex. The badges are not
live CI status; see [Platform coverage](docs/getting-started/installation.md#platform-coverage).

## Contributing and support

Bug reports, questions, and feature requests are welcome in
[GitHub Issues](https://github.com/SantyagoSeaman/sidegraph/issues). Include the commands
you ran, expected and actual behavior, and package versions; remove secrets from logs.
For patches, see [Contributing](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities through the private
channel in [SECURITY.md](SECURITY.md).

The public repository holds the released code; development happens in a private repository.
Contributions are reviewed here and carried over, with attribution, before the next release.

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
