# Security internals

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

The exact runtime rules behind [`SECURITY.md`](../../SECURITY.md): what the inspection
history records, how redaction behaves at its edges, the input limits, which diagnostics
stay content-free, and how Git baselines and bootstrap reads are validated. The policy, scope
and reporting process live in `SECURITY.md`; this page is the reference a reviewer or an
operator checks a specific behavior against.

## Scoped inspection history

`record_drift_check` writes immutable `.sidegraph/verifications/<id>.json` files intended
for source control; on a packed (format 2) store, `sidegraph-compact` later folds the committed
ones into `verifications/packed.shard` byte for byte and drops dead checks. They contain a redacted rationale, decision id, UTC time, inspected
revision, repository-relative paths, Git blob ids and modes, a subject digest and agent
provenance. They store no human author or session id, and no source body or transcript
in these files. Paths and Git content addresses are durable repository metadata; review
the rationale and paths before sharing a store. Redaction is best-effort and cannot
remove sensitive semantics from ordinary prose. These assertions can be wrong; they are
neither truth certificates nor ratification. Older runtimes ignore and preserve them.

## Secret hygiene

Text captured into decisions passes a redaction step before it is stored (API keys,
tokens, `key=value` credentials). Redaction is best-effort pattern matching — review
drafts at ratification time, and treat the committed store like any other repo content in
your secret-scanning setup. Operator tags on capture, direct MCP writes and document
import are redacted before slugification. MCP comma-separated tag strings are cleaned
before splitting; if a comma-containing string contains a matched secret, all its tags are
omitted because a comma may belong to the credential. Use a list for explicit tag
boundaries. Quoted assignment values include triple-quoted and multiline values. If no
legal closer is found, or an assignment span (including URL/card expansion) would swallow
another assignment's key, redaction conservatively consumes the remaining input. A
multiline quoted span splitting a matched URL credential or checksum-valid grouped card
number also consumes the remaining input; this may also remove following prose. A single
quote followed by a Unicode letter or digit is treated as an apostrophe inside the value;
if this leaves no closer, the remainder is removed conservatively. Bare assignment values
and single-line glued tails extend through crossing URL/card matches, including
overlapping valid card spans, while otherwise preserving later prose. Redaction applies
when a record is written; it does not rewrite earlier records, tags or Git history, so review
older tags and rotate any credential that was ever captured.

## Resource limits and redaction runtime

The MCP rate limit, the per-request input limits and the rules for raw Python values are part
of the tool contract: see [invocation admission](mcp-tools.md#invocation-admission) and
[capture and direct-write resource limits](mcp-tools.md#capture-and-direct-write-resource-limits).

Document bodies use an independent 8 MiB per-file limit: an import can exceed 4 MiB in total or
produce more than 100 records. After path/profile/escape and existing final-component
no-follow checks, at most 8 MiB plus one byte is read from the opened file before hashing,
decoding, parsing or redaction. A file exactly at the limit is allowed; an oversized file is
skipped whole with no prefix parsing or raw fallback. Operator tags and free-text import
options are checked separately under the capture field/structure/request limits in both
real and dry runs. The explicit post-redaction `--section-limit` summary option is unchanged.

Assignment-key, URL-credential, JWT and private-key-block recognition use linear scanners
to avoid repeated suffix searches in the four known attack families. This is a practical
bound for those recognizers and admitted inputs, not a universal regex complexity proof or
a guarantee of detecting every secret. Other fixed-token passes retain their patterns;
existing misses such as email addresses and bare hexadecimal strings remain. The conservative
quoted-assignment behavior above remains unchanged. Continue reviewing drafts and scanning
committed content for secrets.

## Write diagnostics and logging

Malformed capture drafts and write arguments produce bounded schema diagnostics: known
field names, bounded list indices and fixed error codes. Input values, custom validator
messages, contexts, model names, arbitrary exception class names and unknown field/code
strings are omitted. At most 10 errors, 8 path components and 1,024 characters are shown.

Logical MCP writes check raw admission before signature coercion and intercept exceptions
before FastMCP warning/error logging. The finite input policy also applies to raw
`ratify`, `sync_anchors` and `add_anchors` arguments. Read-tool aliases
and diagnostic help retain their existing behavior; write errors list valid parameters
without repeating unknown argument names. Unsupported or unaudited tool registrations
fail closed. The compatibility adapter's tested versions are listed under
[safe write diagnostics](mcp-tools.md#safe-write-diagnostics).

A failed store operation can have written a canonical file even when the derived index
rolled back. A rejected incomplete proposal therefore warns that a record may exist on
disk: do not re-propose in the same session; reopen and inspect it in the next one.
Post-write results retain their write status and recovery guidance. Ratification and
domain activation/TOC diagnostics use controlled causes instead of raw exception text.

This protects the specified malformed-write and exception diagnostic channels, not all
logging or metadata. FastMCP DEBUG argument tracing runs before middleware and can record
raw inputs, including valid ones; avoid enabling it for sensitive writes. Transport
framing, caller-created model errors, custom Python methods, unrelated read/filesystem
diagnostics and caller logging are outside this guard. Successful descriptors, paths,
record identifiers and ratify result keys still serve their normal API roles. Historical
records and logs are not rewritten.

## Git verification baseline

`sidegraph-verify --against` and `sidegraph-doctor --against` resolve one commit-ish before
passing a baseline to Git diff or show. Leading-dash, empty and NUL-containing inputs are
rejected; the protected resolver must return one full lowercase SHA-1 or SHA-256 commit ID.
Unknown or unavailable history, non-commit objects, revision ranges and resolver failures
are operational errors (exit `1`), without an unsafe fallback. Moving a branch during the
check cannot change the frozen baseline used to read earlier records.

The CLI rejects malformed argument syntax with exit `2`: for example, `--against --bad`
leaves the option without a value. Use `--against=--bad` to pass a leading-dash value to
the resolver, which rejects it operationally with exit `1`.

This freezes the baseline only. Concurrent working-tree changes are not an atomic snapshot.
Diff paths use unquoted NUL-delimited bytes, preserving Unicode, whitespace, quotes and
literal TAB/LF/CR characters. Malformed, incomplete or unsupported diff framing fails
operationally before classifying any paths; it cannot produce a partial clean report.

A human-readable report can fail on a path the terminal cannot encode (exit `1`).
For undecodable pathname bytes, use `--json`, which escapes the path and preserves
the non-clean exit `2`. This reporting limitation does not produce a clean exit `0`.

## Content a clone brings

A repository you clone can carry files placed to steer the tools that run in it. Sidegraph
treats three kinds as hostile input.

**Git configuration inside the checkout.** Every git command Sidegraph runs goes through one
function, and two layers apply to each call.

- *The guard.* Git is not started in a directory that holds `HEAD` and `objects` (or
  `commondir`) without a `.git` entry, anywhere between the directory git would start in and
  the first `.git` above it. Git would adopt such a directory as a repository and read its
  `config`, which a clone can bring. The guard reads directory entries only and works with
  every Git version. A directory named `.git`, and the places Git itself creates below one
  (`.git/worktrees/<name>`, `.git/modules/<name>`), are not refused. A checkout that sits
  below a folder named `.git` is still guarded. A refusal looks like a missing git: the checks
  that need git are skipped, and nothing runs.
- *The settings.* Every call also carries `safe.bareRepository=explicit` and an empty
  `core.fsmonitor`, through `GIT_CONFIG_COUNT` and its key and value variables, appended after
  any pairs the environment already holds. The environment mechanism needs Git 2.31, and
  `safe.bareRepository` needs Git 2.38; an older Git ignores them and the guard alone applies.
  No fsmonitor command or daemon is started for these calls, so Sidegraph's git calls do not use
  your own fsmonitor, and a `git status` that does not take optional locks can remove the
  fsmonitor extension from your index until your next `git status` (only `sidegraph-compact` and
  `sidegraph-migrate` run such a call). A `git -c` value that a parent git process passes to
  Sidegraph through the environment is read after the two settings and wins over them; the
  guard still applies.

These checks were run with Git 2.54 on macOS. The version limits come from Git's release notes
and source and were not run, and Windows was not measured.

Git still enters a submodule you have checked out. Its git directory is written on your
machine, not by the clone, so a command configured there needs someone who can already write
on your machine and is outside this model. Your own configuration, your environment and a
`.git` entry delivered inside an archive (Git never checks one out) are outside it as well, and
a filter, diff driver or signing program in your own configuration still runs when Git needs it.

**A `.env` file.** FastMCP reads `FASTMCP_*` settings from a `.env` file in the working
directory when it is first imported, including the transport, so a committed file could replace
the stdio server with an HTTP listener. Importing `sidegraph` sets `FASTMCP_ENV_FILE` to the
null device unless you have set it, which makes the MCP server read its settings from the
environment only. `FASTMCP_*` variables you export keep working, and so does a
`FASTMCP_ENV_FILE` you set yourself. The variable is set for the whole process: a program that
imports `sidegraph` and then runs its own FastMCP server does not get its `.env` read either.

**A store link.** A store path that leaves its repository through a symlink or a Windows
junction is refused before anything is read. The path counts as leaving when its real location
is outside any repository that encloses the path as written, whether the link is the store
itself or a folder above it. A link whose target stays inside the repository is followed. The
commands and MCP tools name the link in their error, and SessionStart reports that the store
cannot be opened. No setting allows the link. To share one store between projects, set
`SIDEGRAPH_DIR` to the shared directory itself; a store named that way belongs to the directory
that holds it. The plugins' own commands set `SIDEGRAPH_DIR=.sidegraph`, so a plugin install
cannot place the store outside its repository.

A committed `.claude/settings.json` can set environment variables for hooks and define hooks.
Your agent host gates that file; it is not covered here.

## Bootstrap source validation

The default source size limit is 512,000 bytes. Bootstrap excludes `.git/`, `.sidegraph/`,
`.venv/`, `venv/`, `node_modules/`, `vendor/`, `dist/`, `build/`, `target/`, `graphify-out/`,
`_build/` and `coverage/`, plus binary, unreadable and oversized files.

Bootstrap admits regular source files only. FIFO, sockets, devices and directories are
excluded before reading; include overrides directory/size rules, never regularity, root,
binary-prefix or UTF-8 requirements. Frozen resolved targets are rechecked against the
repository root and excluded directories, with exact included-target overrides.

The scanner opens one descriptor, checks its type and size again, and validates strict
UTF-8 through unbuffered reads. An ordinary file has a size-limit-plus-one byte budget;
an included file has an opened-size-plus-one budget, with observed growth excluded as
`changed-during-scan`. Binary detection is restricted to the first 4,096 bytes.
Available nonblocking and no-follow flags protect final-component FIFO/link replacement;
platforms without those flags do not have the same leaf-race guarantee. Ancestor changes
and concurrent content edits are not an atomic snapshot. This scanner change does not
make later planner/apply reads atomic or impose a global cap on explicit includes.
