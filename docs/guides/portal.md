# Review and browse the store in Sidegraph Portal

> **Start here:** Open the Portal (below), then use the section for the task you came for. Commands are shown by name; without the CLI on your PATH, run them with the `uvx` prefix described in [running the CLIs](../reference/cli.md#running-the-clis-beside-the-plugin). [Glossary](../concepts/glossary.md).

Sidegraph Portal is a small web page on your own computer. It shows the decisions, facts and
domains in your store and lets you act on them in a browser:

- **Queue.** Accept or reject what an agent proposed, with the agent's recommendation beside it when it left one.
- **Edit.** Correct a record. The store is append-only, so saving writes a new version.
- **Browse.** See the store as a graph, search it, and open a record to see the code it is
  anchored to.
- **Agent view.** Run a hook (an event the host fires on its own, such as a session start) or a
  retrieval or inspection MCP tool (an action the agent can call) and see the text a model would
  receive in a
  fresh session.

Every change the Portal makes goes through the same code as `sidegraph-ratify` and the MCP
tools. The same store rules apply.

Words used here. The **store** is the directory of record files (`.sidegraph/`) that you commit
beside your code. A **domain** is a named area of the project. The **graph** is the map of your
code that Graphify builds, and an **anchor** links a record to a function, file or heading in
it. The **index** is a local database rebuilt from the store's files. A **sync** compares the
anchors with the current graph and updates their status. The [glossary](../concepts/glossary.md)
has the rest. **Provenance** is a record's note of who wrote it and where from.

## Open the Portal

**From chat.** Ask your agent to "show the portal". The agent calls
[`open_portal`](../reference/mcp-tools.md#open_portal) and puts a link in its reply. Click the
link. The Portal also opens a browser tab on its own, unless you asked the agent not to open
one. Both lead to the same page.

**From a terminal.**

```bash
sidegraph-portal
```

The command prints one line to its standard output and keeps running (server messages go to
the error output):

```text
url http://127.0.0.1:53355/#t=K6ZEZcvOeT-5FhTifgE4jOEDslqJRZeb5aTAevr-i9o
```

The Portal tries to open your browser on that link. Add `--no-browser` to open it yourself, `--db PATH` to choose a
store and `--port N` to choose a port. Without `--port`, the system picks a free port, so yours
will probably not match the one above. `--graph PATH` points the Portal at another code graph than the
default one. Every option is in the [CLI reference](../reference/cli.md#sidegraph-portal).

Use the **whole link**, including everything after `#`. The part after `#` is the access token.
Your browser never sends that part to the server by itself. The page reads it and attaches it to each
request it makes. A tab opened without the token shows "Open the portal from its link" and does
nothing else.

**If the browser does not open.** A browser installed as a snap or a flatpak package may not be
able to read the temporary file the Portal uses to open it. Copy the link from the agent's reply
or from the terminal and paste it into the browser.

**What opening does and does not do.**

- The Portal never creates a store. If there is none, it tells you how to set one up.
- In chat, one Portal belongs to one agent session. Asking again returns the same link while it
  runs. Two sessions get two Portals on two ports.
- A Portal opened from chat closes when its session ends. The page then shows the banner "The
  portal has stopped"; ask the agent to open it again.
- A Portal opened from either place closes after 30 minutes without a request you caused by
  using the page (the default; `idle_minutes` in `portal.json` changes it). A tab that is only open does not
  keep it alive. In a terminal, Ctrl+C stops it.
- A tab left open after its Portal stopped shows the banner "The portal has stopped", whether the
  Portal told the tab it was closing, or stopped without telling it (a tab that was in the
  background, or a killed process). The "closed" page appears instead only when a
  Portal that is still running refuses the tab's link, for example a newer Portal that took
  the same port. Open the new link in either case.

Preferences (theme, refresh interval, editor link, idle time) are in
[`portal.json`](../reference/configuration.md#portal-settings). The theme button in the page
writes the theme there.

## The pages

The header has four pages: **Graph**, **Records**, **Queue** (with the number waiting) and
**Agent view**. The footer shows "N files changed under the store" and "The portal never commits. Commit them in a pull request." when
the store is inside a git checkout. You review and commit those files yourself, as with any
other [change to the store](team-workflow.md).

The page updates by itself. It checks the store every few seconds while a tab is visible, and
a change made by an agent or by `sidegraph-ratify` in another window appears without a reload.
If the store is busy, the page says it is retrying.

## Review the queue

The Queue lists everything waiting for a decision: proposed decisions, standalone facts and
domains. Each item shows the record, the agent's recommendation when there is one, and three
signals: the status of its code anchors, similar records, and where it came from (session and
commit).

**Accept and Reject.** Mark each item **Accept** or **Reject**. The Portal says **Reject** where
the CLI and the MCP tools say *drop*. A rejected decision or fact has the status `rejected`; a
rejected domain has the status `dropped`. Nothing is deleted: a rejected record stays in the
store's history.

**Apply.** Marks are only marks until you press **Apply verdicts**. Until then you can clear
any of them. Applying cannot be undone: an accepted record does not go back to being a proposal, and a
rejected one is not reopened. (Editing an accepted record later writes a new version; it does not
undo the acceptance.) To bring a rejected idea back, ask the agent to propose it again or record
it with `add_decision`. Each record you accept is stamped with your git user name as its
ratifier, when git has one configured.

**Evidence follows its decision.** Facts proposed as evidence for a decision are shown inside
that decision and have no marks of their own. Accepting the decision accepts its pending
evidence. Rejecting it rejects an evidence fact when every decision the fact is evidence for is
rejected too. Evidence for another decision that is still undecided stays in the queue. A fact
proposed on its own, or for a decision that is already accepted, is its own item in the queue.

**The cascade preview.** Before you apply, the queue shows what each mark will do to the
evidence, counting all your marks together. A fact that supports two decisions is shown as
rejected only when you rejected both. If you reject one and accept the other, it is not
rejected. The preview uses the same rule the store applies, so
the result matches it unless the store changes between the preview and Apply.

**Agent recommendations.** An agent can leave a recommendation (accept, reject or edit, with
a reason) on an item. It is advice kept on your machine. The Portal never acts on it: your click
is the decision. A recommendation disappears when the record's text changes, because the
agent reviewed different text. When an agent opens the Portal from chat, it is told to review the
waiting items and leave a recommendation on each. An item without advice says "Reviewing…" until its
advice arrives, but only during the first ten minutes after the Portal opened. After that it
says "No recommendation". A Portal started from a terminal never says "Reviewing…".

If **Apply verdicts** reports that some items were not applied, it says why for each one. The
others are applied. Reopen the queue to see what is still waiting.

## Edit a record

Open a record and choose **Edit as new version**. You can change a decision's title, kind,
layer, context, choice, rejected alternatives and consequences, and a fact's statement and
source. Domains and anchors are not edited in the Portal; use the `manage-domains` skill and
the `add_anchors` tool.

> **Saving does not overwrite this record. It creates a new version.**

The store is append-only. Saving writes a new accepted record that replaces the old one. The
old version closes, becomes `superseded`, and stays in the record's history, so "what we said
before" is never lost. The new version keeps the old one's code anchors, scope and layer unless
you changed the layer. A field-level diff is shown before you save.

The new version has no ratifier stamp, because nobody ratified it: it is accepted at once.
Instead, its provenance names you as the author (your git user name, when git has one), with the
source `human`.

**Evidence.** Accepted facts that support the record are listed with "carry over" checked.
Each carried fact is replaced by a new version that supports the new decision; unchecked facts
stay as they are, attached to the old version in history. The new decision and its carried facts are saved as one
operation made of several steps. If a step fails, the page tells you which steps are done and
which remain. If the page
shows **Retry**, press it on that same page: the retry resumes where the save stopped and
writes nothing twice. Reloading or leaving the page loses the retry. If you want a backup of your text, copy it
before you press Save.

**Pending evidence blocks the edit.** If the record has proposed (not yet decided) evidence facts,
the Portal says "This record has N pending evidence facts; decide on them in the queue first."
and links to the queue. Decide on them there, then come back.

**The drift warning.** When a record is marked for content review (its code changed since it
was written or last checked), the edit page says so. Check the text against the code before you
save. Saving corrected text records that it holds for the current code, and the review marker
clears. If the text is right as it stands, do not save it again: the Portal refuses a save with
nothing changed and says "To confirm that this record still holds, record a drift check
instead." For a decision, the [`record_drift_check`](../reference/mcp-tools.md#record_drift_check) tool,
or the `sidegraph:triage-drift` skill, confirms it without making a new version.

**If the save is refused.** When the record has a newer version, the page offers to compare
your text with it or to start again from it. When an agent's proposal to replace the record is
waiting, decide on that in the queue first. The Portal writes nothing in either case.

## Edit and accept a proposal

A proposal cannot be changed in place. **Edit and accept** (a button on a proposed decision or
fact in the Queue) corrects it on the way in. The form needs at least one changed field. The agent's
draft is rejected and your text is written as a new accepted record. The draft stays in history
as `rejected`. It is not linked to your record as a predecessor.

Your record gets exactly the draft's code anchors. If the draft proposed to replace an accepted
record, your version replaces that one. Evidence proposed with the draft is listed with "keep" checked.
Each kept fact is written again as a new accepted fact that supports your record, and the
original proposed fact is rejected. Unchecked facts are rejected with the draft. A fact that
also supports another decision that is not rejected is left as it is.

The form's button is **Save and accept**. The operation has several steps: reject the agent's
draft, settle the draft's other evidence, write the new version, close the old version it
replaces, copy the code anchors and write the kept evidence. The page shows each step as done
now (written by this attempt), done (written by an earlier attempt), failed or waiting. A step
can fail before the new version exists: read each step's status.

**If it stops part-way,** the page says "Saving stopped part-way" and what is already
written stays written. Press **Retry**. The retry repeats the same request, continues with the
remaining steps and writes nothing twice. A reply lost on the way back is handled the same way:
retry, and you get one new record. This works only on the same open page: if you reload or
close it, the retry is gone. If you want a backup of your text, copy it before you press
Save.

**If the page offers "Save your text as a new record,"** the Portal found the draft already
rejected and cannot tell whether that was its own step or someone else's. This happens when
another person or agent rejected the draft first. Your text is not lost: the button saves it as
an ordinary new accepted record with the draft's anchors. Only your text is written. A record
the draft proposed to replace stays current, so replace it yourself if you mean to.

**If the Portal process is killed,** the open page cannot reach the next Portal you open: a new
Portal has a new token, and your text and the save's request key live only in that page. Text
that you did not copy is gone. Open the Portal again, look at the record to see how far a save
got, and edit it again.

## Browse

**Graph.** The store drawn as a graph, with a search box above it. Search does not filter the
graph. It lists hits in a dropdown; click one or press Enter to open its record. The graph draws
up to 3,000 nodes; a larger store is truncated, and the page says how many nodes were left out.
The layout is computed behind a "Laying out the graph…" notice, again when a filter or the
data changes, and in between the graph stays still: dragging a node moves only that node. Tick
"Superseded / history" to include superseded and rejected records; the search box above the graph
follows the same tick.

**Records.** A table with the same search and filters for status, kind, domain, layer and code
anchors. Tick "Search history too" to search closed records too.

Records and Graph search match each word against record text and searchable anchor names or
paths. Anchor matching skips domain labels. Use the domain filter to browse by label. A record
must match every word. When none does, the search lists the closest partial matches. It is the
matching `retrieve_decisions(query=...)` uses, with one
difference: that tool leaves out some proposals, depending on its settings, and the Portal
shows all of them. Typing the first eight or more characters of a record id in the search box
opens that record when only one record's id starts with them; keep typing otherwise.

**The record card.** Every field, the version history in both directions, supporting facts,
provenance and the ratifier. Each code anchor shows its file, line and a snippet of the code,
with a link that opens the file in your editor (VS Code by default; change `editor_url` in the
[settings](../reference/configuration.md#portal-settings)). A snippet shows up to 12 lines first and up to 200 on
request. A record with no code location shows no snippet. The Portal only reads source files
inside the repository, never follows a symbolic link, and shows no snippet for a file larger
than 2 MB.

**Status words.** Each anchor has one of four badges. Hover or focus a badge for its
explanation.

| Word | Meaning |
|---|---|
| found | The graph holds exactly this symbol. |
| ambiguous | Several symbols match. Sidegraph does not guess which. |
| missing | The symbol is no longer in the graph. |
| unchecked | The index was rebuilt and no sync has run since. |

*Unchecked* is the status of every anchor right after the store's index is rebuilt (for
example after a branch switch). It never shows "found" before a sync has checked. The status
updates on the next sync: a commit, a new agent session, or `sidegraph-sync`. Opening a page
never runs a sync (some Agent view runs do, as described below). Two notices can appear: "Anchor status not checked since the index was
rebuilt", and "The graph was rebuilt since the last sync". Line numbers can lag the file
too. When a source file is newer than the graph, the card says its lines may have shifted.

If you switch branches with the Portal open, it picks the change up. A card for a record that
is not on the new branch says so.

## Agent view

Agent view shows the text a model would receive. Pick a hook (SessionStart, PreToolUse, the
brief added to an Agent or Task call, SubagentStart, Stop, or the MCP server instructions) or a
retrieval or inspection MCP tool, fill in its parameters, and press **Run**. The page shows
the text, its size, and any budget or cut the surface reports. It is the output of the
hook or tool for the inputs you gave, in an emulated fresh session, not proof that the host uses it: a host can discard it.
Choose which host's wiring to emulate: `claude`, `codex`, `grok` or `muse`, as the page names
them. It starts on the host of the session that opened the Portal when that host is Claude Code,
Codex or Grok. A session on any other host (Muse, for example) and a Portal started from a
terminal start on `claude`; from a terminal, `--host-family` picks another. When the chosen
host does not use a hook, the hook carries the tag "Not wired on" and the host's name, and
running it shows no text. A few hooks have extra
conditions on one host: Grok, for example, discards SessionStart output. Those appear under
"Emitted only when", with the hook's usual conditions.

Under "Emulated session" the page shows "Fresh session", and "Emitted only when" lists the
conditions. That means:

- The session is fresh. Nothing was shown or claimed earlier, so a record that an earlier call
  in a real session would have delivered is delivered here, and every due notice appears. Size
  limits still apply. A real session does not repeat in full a record it already received.
- Stop is run as if its transcript check had passed ("The transcript gate passes." under
  "Emitted only when"). PreToolUse and the Agent brief need input that fits the store: type the
  path of a file that has records, or a prompt that names one. A hook that does not fire,
  because the host does not use it or nothing in the store matches, reports why and shows no
  text.

**Some runs prepare the store as the agent's next call would.** SessionStart, and the tools that
sync before they answer (`get_task_context`, `query_decisions`, `query_structure`, `drill_down`
and `retrieve_decisions` with files), first run the same sync a real call runs. If a code file
that records are anchored to moved in a commit, that sync follows the move and rewrites the
moved code's entity file in `.sidegraph/entities/`. A tool call would leave the same change. When
a run did, the page says so: "Preparing this view ran the same sync the agent's next call would
run; it updated N records' code locations". N counts the moved code locations. The change shows in
the footer's count of changed files. In a linked worktree that uses the main checkout's graph,
the sync writes nothing to `.sidegraph/`. Review and commit it like any other change to the store. The other surfaces and tools run no
sync.

**What a run does not do.** It records nothing about a session. It does not mark records as
shown, claim files, count a retrieval or write the delivery log. It does not change the usage
figures of `sidegraph-stats`. Looking at what a model would see is not counted as a model
using memory. When you started the Portal from a terminal with a `--graph` that differs from
the graph the agent's server would use, Agent view runs no sync and says so. The text may then
differ from what the agent would get.

## Who can open the Portal

The Portal listens on `127.0.0.1` only, so it is reachable from your computer and not from the
network. Three rules apply to it:

- Every API request needs the access token from your link. The browser never sends the part
  after `#` by itself, so the page attaches the token to each request, and the Portal logs no
  requests. Without the token, the page shell loads but every data request is refused.
- Other web pages open in your browser cannot use the Portal. It checks the address of every
  request and the origin of every request that changes something, refuses to be framed, and
  sends no cross-origin permission.
- Another user on your computer cannot open it without the link. The Portal does not put the
  token in a browser command line, where a process list would show it.

**The boundary.** In chat, the agent receives the link, because you asked it for one. An agent
that can run shell commands can therefore call the Portal and ratify on its own, and the record
then carries your git user name (when git has one), as it does for a command run in your terminal. The token keeps
out web pages and other users. It does not keep out the agent you are working with. If that
matters to you, do not ask an agent for the link, and run `sidegraph-portal` yourself. Even then,
an agent that can run shell commands can ratify with the CLI.

## What the words mean

| In the Portal | In the CLI and MCP tools |
|---|---|
| Accept | `accept`: accepted |
| Reject | `drop`: `rejected` for decisions and facts, `dropped` for domains |
| Edit as new version | `supersede_decision`, `supersede_fact`, with you as the author |
| Edit and accept | Reject the draft, then add an accepted record with the corrected text |
| found, ambiguous, missing | `live`, `degraded`, `orphaned` |
| unchecked | Not a store status: the index was rebuilt and no sync has run |
