# Sidegraph

[![tests](https://img.shields.io/github/actions/workflow/status/SantyagoSeaman/sidegraph/ci.yml?label=tests)](https://github.com/SantyagoSeaman/sidegraph/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/SantyagoSeaman/sidegraph)](https://github.com/SantyagoSeaman/sidegraph/releases)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)
[![Windows x64: Claude Code and Codex plugin sessions checked](https://img.shields.io/badge/Windows%20x64-Claude%20Code%20%2B%20Codex%20checked-blue)](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#platform-coverage)
[![macOS arm64: Claude Code and Codex plugin sessions checked](https://img.shields.io/badge/macOS%20arm64-Claude%20Code%20%2B%20Codex%20checked-blue)](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#platform-coverage)

Sidegraph is a local MCP server and CLI for preserving engineering decisions and
facts alongside your code. It records why a design was chosen, which alternatives were
rejected, and what was learned during implementation, then retrieves that context for
the files and symbols an agent is working on.

Records live in your repository as reviewable JSON files. A derived SQLite index supports
retrieval; [Graphify](https://github.com/Graphify-Labs/graphify) supplies the code and document
graph used to anchor records. Claude Code, Codex and Grok Build plugins connect capture and
retrieval to coding sessions. OpenCode V2 and the experimental Muse expose MCP tools and shared
skills for explicit retrieval and capture; Muse also receives the session map and a subagent
brief.

- Keep decisions, rejected alternatives, and evidence with their provenance and history.
- Hand the agent the relevant mistakes and constraints before it edits, sized to fit its context.
- Review agent proposals before accepting them, or let low-risk ones be accepted automatically.
- Follow code through refactors, and flag records whose code changed or disappeared.

[Documentation](docs/README.md) · [Installation](docs/getting-started/installation.md) ·
[Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

## Quickstart

### Set up a repository (once)

**You need** Git, one of the hosts below, and [uv](https://docs.astral.sh/uv/getting-started/installation/),
which also downloads Python 3.13 if you do not have it. On Windows you also need
[Git for Windows](https://gitforwindows.org/) with its default options
([details](docs/getting-started/installation.md#windows)); run the commands below in
PowerShell, using the Windows block where one is given. No API keys and no existing ADRs are required.

macOS, Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Linux follows the macOS commands but is not among the checked platforms. Open a new terminal afterwards so `uv` is on your PATH, then change into the root of your
repository (`cd /path/to/your/repo`). Every command below runs there.

**Already using Sidegraph?** (New here? Skip to step 1.) Follow the [0.14 migration guide](docs/guides/migrating-to-0.14.md)
before upgrading Graphify, so accepted domains keep their membership. Update every client
first, then use the existing-install recipe for your host below. Claude Code and Codex keep
the same `sidegraph@sidegraph` identity; no uninstall/reinstall is needed.

**The short path** (macOS or Linux, Claude Code; every step is explained below):

```sh
uv tool install "graphifyy==0.9.90" && GRAPHIFY_NO_AUTO_REFRESH=1 graphify update .   # step 1 (optional)
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init   # step 2: press Enter twice
```

then, in Claude Code, `/plugin marketplace add SantyagoSeaman/sidegraph` and
`/plugin install sidegraph@sidegraph` (step 3), and ask the agent to record a gotcha (step 4).

**1. Build the code graph** (recommended; you can [skip it](#other-options), and then answer
no to the first question in step 2). [Graphify](https://github.com/Graphify-Labs/graphify) maps your
functions, classes and files, so a record can point at the exact function it is about.

macOS, Linux:

```sh
uv tool install "graphifyy==0.9.90"   # two y's in the package name; the command is graphify
export GRAPHIFY_NO_AUTO_REFRESH=1     # stops Graphify rewriting its skill files in your agent after an upgrade (this shell only; the git hook in step 2 sets it itself)
graphify update .                     # ends with "Code graph updated."
grep -qxF "graphify-out/" .gitignore 2>/dev/null || printf '\ngraphify-out/\n' >> .gitignore   # the graph is rebuilt from your code; keep it out of git
```

Windows (PowerShell):

```powershell
uv tool install "graphifyy==0.9.90"   # two y's in the package name; the command is graphify
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"   # stops Graphify rewriting its skill files in your agent after an upgrade (this window only; the git hook in step 2 sets it itself)
graphify update .
if (-not (Select-String -Path .gitignore -Pattern "^graphify-out/$" -Quiet -ErrorAction SilentlyContinue)) { Add-Content .gitignore "`ngraphify-out/" }   # keep the graph out of git; do not use >> in PowerShell
```

**2. Create the store**, the `.sidegraph/` directory that holds the records:

```sh
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

The command is the same on Windows. It prints `created store: .sidegraph` and asks two questions. Both default to yes, so press
Enter twice. Outside a terminal (a script, CI, an agent session) it asks nothing, installs no
git hook and leaves `.claude/settings.json` alone; pass `--hooks` and
`--ratify-policy auto-low-risk` to answer yes up front.

- *Keep the code graph fresh?* Yes installs a git hook that rebuilds the graph after each commit.
- *Auto-ratify low-risk records?* Yes (the default) means the lessons, gotchas and facts the
  agent proposes are accepted without review once they are anchored to code; answer no to
  review every proposal. ADRs, constraints and domains always wait for you. The answer is saved in
  `.claude/settings.json`, which you commit. Sidegraph reads it for every MCP integration unless an explicit
  `SIDEGRAPH_RATIFY_POLICY` overrides it, so teammates use the same project policy.

Details: [`sidegraph-init`](docs/reference/cli.md#sidegraph-init).

**3. Choose one host.** Install its plugin, then start a new session in this repository.
| Host | Automatic session memory | Checked |
|---|---|---|
| Claude Code | yes: hooks, capture reminders, context on reads | macOS and Windows |
| Codex | yes: hooks, after you approve them | macOS and Windows |
| Muse (experimental) | partly: session map and subagent brief, after you approve them | macOS |
| OpenCode V2 | no: MCP and skills | macOS |
| Grok Build | yes: hooks, capture reminders, context on reads; the session map through the MCP server | macOS |

Claude Code is shown first; open the section for another host. Host plugin updates and the
MCP runtime are separate: the default runtime follows the public repository's `main`, which
advances at releases; that does not refresh cached skills, manifests or hook definitions.
Use your host's update recipe after a release.

#### Claude Code

In a Claude Code session in the repository:

```text
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Review the installation summary and any approval prompts, then start a new Claude Code
session. The plugin provides MCP, skills, session memory, capture reminders and context on reads.
To check the connection, run `/mcp` in the new session: `sidegraph` should be listed as connected.
For an existing install, run these in your terminal:

```sh
claude plugin marketplace update sidegraph
claude plugin update sidegraph@sidegraph
```

Keep the same install scope; see the [Claude Code guide](docs/getting-started/claude-code-setup.md#update-an-existing-plugin).
Start a new session after updating, or use `/reload-plugins` and review its prompts.

<details>
<summary><strong>Codex</strong></summary>

#### Codex

Run these in a terminal in the repository:

```sh
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

Start a new interactive Codex session. Review Sidegraph's hooks through the startup warning
or `/hooks`, then approve them. Without approval, MCP remains available but automatic session
memory, capture reminders and subagent notices stay silent.
For an existing install, run this in your terminal:

```sh
codex plugin marketplace upgrade sidegraph
```

Start a new session and review any changed hook definitions through the startup warning or
`/hooks`; updating does not grant approval. This update recipe requires Codex CLI 0.162.1
or later. See the [Codex guide](docs/getting-started/codex-setup.md#keep-the-plugin-updated).

</details>

<details>
<summary><strong>Grok Build</strong></summary>

#### Grok Build

The plugin brings the MCP tools, the skills and the hooks: records arrive when the agent reads,
searches, edits or writes a file or starts a subagent, and the agent is reminded to propose
decisions once a session has done some work. Grok does not pass the `SessionStart` hook's output
to the model, so the session map comes from the MCP server instead. Run in a terminal:

```sh
grok plugin marketplace add SantyagoSeaman/sidegraph
grok plugin install sidegraph@sidegraph
```

Accept the trust prompt (`--trust` skips it), then start a new Grok session in the repository.
Ask the agent which Sidegraph tools it has: `get_task_context` should be among them. Checked
on macOS with Grok Build 1.0.70 (hooks, session map, capture reminder) and 1.0.62 (install,
MCP tools, skills, a gotcha recorded in one session and retrieved in the next). Windows is not
checked. See the [Grok Build guide](docs/integrations/grok.md).
For an existing install, run these in your terminal, then start a new session:

```sh
grok plugin marketplace update
grok plugin update sidegraph
```

</details>

<details>
<summary><strong>Muse</strong> (experimental)</summary>

#### Muse (experimental)

MCP, shared skills, a session map at start and a memory brief for subagents; no context on
reads and no capture reminder. The hooks need `python3` (3.9 or later; the macOS one works).
Add the marketplace and install the plugin:

```sh
muse plugins marketplace add sidegraph https://github.com/SantyagoSeaman/sidegraph
muse plugins install sidegraph@sidegraph
muse plugins inspect sidegraph
```

Review the two hook capabilities and the MCP capability, then approve them:

```sh
muse plugins approve sidegraph
```

Exit Muse and start a new Muse process from the intended project directory. Restart it there
whenever you switch projects; selecting a workspace in a running process is insufficient.
For an existing install, refresh the marketplace and the plugin, then inspect the changed
capability:

```sh
muse plugins marketplace update sidegraph
muse plugins update sidegraph
muse plugins inspect sidegraph
```

Review changes, then approve and restart Muse from the target project:

```sh
muse plugins approve sidegraph
```

Installation, update, MCP approval, the session map and the subagent brief were checked on
Muse 1.4.14 on macOS; Windows is not checked. See the [Muse guide](docs/integrations/muse.md).

</details>

<details>
<summary><strong>OpenCode V2</strong></summary>

#### OpenCode V2

Requires OpenCode V2 (native baseline 2.0.29), `uv` and Git on PATH. Native packaging and MCP
were checked on macOS; Windows is not checked. In your terminal:

```sh
opencode plugin add 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
```

Start a new OpenCode session in your project and use `@sidegraph-setup`. Keep OpenCode's
normal MCP and skill permissions; no additional plugin-approval command is needed. This
route supplies MCP and skills only: ask for context and capture decisions explicitly.
For an existing install, check and update the Git package, then restart OpenCode:

```sh
opencode plugin check 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
opencode plugin update 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
```

The [OpenCode installation guide](.opencode/INSTALL.md) is the canonical recipe, including
configuration overrides. The package target `#main` and the MCP runtime's Git ref are
separate settings.

</details>

### First memory

**4. Record a decision.** In a session, ask the agent, naming a real file and function:

> Record this gotcha: the upstream API limits batch requests to 100 items. We rejected
> a single unbounded request because it fails for larger imports. Anchor it to
> `import_batch` in `src/importer.py`.

A record you ask for is accepted at once when the agent saves it directly (`add_decision`), or
when auto-ratify is on (step 2) and the function is found in the graph; otherwise it waits for
your review, like the records the agent proposes on its own.
Ask the agent to review pending Sidegraph decisions through MCP on any host; Claude Code
also offers `/sidegraph:ratify-decisions`.

**5. Check and commit.** Start a new session and ask: "Before I edit `src/importer.py`, what
should I know? Retrieve Sidegraph context for that file with `get_task_context`." A record still waiting for review is shown tagged `[unratified]`. The reply should
contain the gotcha and the rejected alternative, for example "import_batch calls an
upstream API that accepts at most 100 items per batch request". The agent
should show the gotcha on every host, including those without lifecycle hooks. Then commit `.sidegraph/` and `.gitignore`,
and `.claude/settings.json` if step 2 created or changed it. The index file inside
`.sidegraph/` is git-ignored automatically.

### Each teammate

After cloning, each teammate runs steps 1, 2 and 3. In a clone, step 2 leaves the committed
records and settings alone. It only installs the graph hook there.

Next, [name your domains](docs/guides/naming-your-domains.md) to give the memory a table of
contents, or import existing ADRs and design specs with the
[preview-first bootstrap](docs/getting-started/bootstrap.md). The
[verification checklist](docs/guides/verifying-your-setup.md) covers the whole workflow.

### If something does not work

- Run `sidegraph-doctor` (as `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor`,
  or by its plain name if you installed the CLI, see [Other options](#other-options)): it lists what is wrong with the store, the graph and the wiring.
  [Troubleshooting](docs/guides/troubleshooting.md) explains each message, and the
  [verification checklist](docs/guides/verifying-your-setup.md) walks the whole workflow.
- `get_task_context` finds nothing for a function: there is no graph yet (step 1), or the
  record was saved before the graph existed; run `sidegraph-sync`.
- The agent has no Sidegraph tools: start a new session after installing, approve the
  plugin's prompts (Codex: its hooks too), and in Claude Code check `/mcp`.

**Removing Sidegraph.** Remove the plugin with your host's plugin command
(`/plugin uninstall sidegraph@sidegraph` in Claude Code, `grok plugin uninstall sidegraph`
in Grok Build). `sidegraph-init --remove-hooks` removes the git hook. The records in
`.sidegraph/` are your project's memory and are committed: delete them only if you mean to
lose them. `graphify-out/` can always be deleted and rebuilt.

### Other options

- **Without the graph.** Skip the Graphify install and `graphify update` in step 1 (still
  work from the repository root) and answer no to the first question in step 2. Records then
  attach to whole files instead of functions.
- **Versions.** The default MCP runtime and the step 2 command install Sidegraph from the
  public repository on GitHub, whose `main` branch changes only when a release is published.
  To pin a version instead, see
  [pinning](docs/getting-started/installation.md#mutable-development-references).
- **Sidegraph commands in your own terminal.** Optional:
  `uv tool install git+https://github.com/SantyagoSeaman/sidegraph.git@main`. It does not update itself, so
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
   a Claude Code, Codex or Grok Build session the plugin reminds it to propose decisions worth
   keeping (on Grok Build in interactive sessions only). In OpenCode and Muse, ask for capture
   explicitly. Known secret patterns are redacted before storage (best effort).
2. **Review.** You accept or drop what the agent proposed; Sidegraph calls this *ratifying*.
   Records are JSON in `.sidegraph/`, committed with your code: one file per record, or lines
   in shard files once maintenance has packed them.
3. **Anchoring.** Each record is linked to the functions, classes, files or document headings it
   is about, using the graph Graphify builds from your code. When a refactor moves code,
   Sidegraph follows the move; when it cannot tell where the code went, it flags the record for
   review.
4. **Recall.** Claude Code and Codex inject a short table of contents of the project's areas,
   called *domains*, at session start. In every host, ask the agent to retrieve a file's
   records with `get_task_context` before changing it, with known mistakes first. In Claude Code, the agent also receives a file's records the
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

Facts follow the same review and history rules as decisions. Accepting a decision also accepts the pending facts
attached to it; dropping it drops them unless they still back another live decision. Details:
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
  review unless the project answered yes to auto-ratify in `sidegraph-init` (the default
  answer; `SIDEGRAPH_RATIFY_POLICY` overrides it); even then, architecture decisions,
  constraints and domains wait. See the
  [ratification policy](docs/reference/configuration.md).
- **Preserved history:** a reversal closes the old record and adds a new one. Records change in
  place only in narrow ways: review sets a status, closing a superseded record sets its end
  date and status, adding anchors adds bindings, and a confirmed file move updates where an
  anchor points.
- **Local counts:** for `sidegraph-stats`, Sidegraph counts how often memory was shown. The
  counts stay in `.sidegraph/index.db`, which is git-ignored and never leaves your machine.
  To stop counting, set `SIDEGRAPH_TELEMETRY=off`.

## The engine underneath

The graph of your code and documents comes from
[Graphify](https://github.com/Graphify-Labs/graphify), built without an LLM. Sidegraph only reads
it and never writes into it. Without a graph, records anchor to files and domains, and recall
still works. Three things need the graph: anchoring a record to a function, grouping related
code automatically, and following moved code (see the
[operations reference](docs/reference/operations.md#the-graph-dependency-stated-plainly)).
The graph and the local index can be rebuilt at any time. The JSON records in `.sidegraph/` (files or shard lines)
cannot: they are the source of truth, so keep them in your repository and backups.

## Status

Sidegraph is in beta. Read the [stability policy](docs/reference/stability.md) before
building automation against it. For reproducible deployments,
[pin a version](docs/getting-started/installation.md#mutable-development-references).
The [changelog](CHANGELOG.md) records release changes.

The platform badges cover full Claude Code and Codex plugin sessions on Windows x64 and
macOS arm64, with Python 3.13 and 3.14, including Windows PowerShell 5.1 without
PowerShell 7. Muse, OpenCode V2 and Grok Build are checked on macOS only. See
[Platform coverage](docs/getting-started/installation.md#platform-coverage) for limits.

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
[ADR export](docs/guides/adr-export.md) ·
[Reference](docs/reference/mcp-tools.md) ·
Integrations: [Graphify](docs/integrations/graphify.md) · [Claude Code](docs/integrations/claude-code.md) · [Codex](docs/integrations/codex.md) ·
[Muse](docs/integrations/muse.md) · [OpenCode V2](docs/integrations/opencode.md) · [Grok Build](docs/integrations/grok.md) ·
[Verify your setup](docs/guides/verifying-your-setup.md) ·
[Operations](docs/reference/operations.md) ·
[Pilot kit](docs/pilot-kit/README.md) ·
[Engineering whitepaper](docs/whitepaper/index.md)

## License

[Apache-2.0](LICENSE).
