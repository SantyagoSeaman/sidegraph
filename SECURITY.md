# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub's **"Report a vulnerability"** button
(Security tab) on this repository — do not open a public issue for security problems.
You should get a first response within a few days.

## Data boundary — what this tool touches

Sidegraph is a local-only tool. Its entire data surface:

- **Reads:** your repository's files and the graph engine's `graphify-out/graph.json`
  (strictly read-only — Sidegraph never writes into engine output).
- **Writes:** confined to one store directory inside the target repository
  (`.sidegraph/` by convention) — small, human-readable JSON record files meant to be
  committed to your repo, a derived and gitignored `index.db` (a local SQLite index
  rebuilt from those files for fast queries, never itself committed), a committed format
  marker, and a `.gitignore` the store writes for itself. Opening a legacy single-file
  store (`decisions.db`, from before store schema 0.4.0) triggers a one-time migration
  that renames the legacy file to `decisions.db.migrated-backup` (kept, never deleted)
  alongside writing the new
  directory layout. The store is the default persistence location, not the only one:
  `sidegraph-init` can write `.claude/settings.json` (the ratification-policy env entry);
  the visualization, OKF export and bootstrap-report commands write to the output paths you
  give them; and the optional `sidegraph-prepare-commit-msg` git hook, once you install it,
  adds commented decision trailers to the commit message file git hands it
  (normally `.git/COMMIT_EDITMSG`). Three more writes sit outside the repository's own
  files. `sidegraph-init --hooks` (or a yes to its refresh question) writes a helper script
  and a marked block in three git hooks (`post-commit`, `post-merge`, `post-checkout`) under
  `.git/hooks/`. When `core.hooksPath` is set, `sidegraph-init` writes only the helper (to
  `.git/hooks/`) and prints the lines to add to your hooks by hand. While it runs, the helper
  also keeps a lock, a flag file and a log in the git directory. `sidegraph-init --no-hooks`
  writes the git config key `sidegraph.graphRefresh` to the repository's own config, and
  `sidegraph-init --remove-hooks` takes the blocks, the helper and that key out again. The
  SessionStart hook writes one 40-character commit id to
  `${XDG_CACHE_HOME:-~/.cache}/sidegraph/launch-commit`, and only when the running package was
  installed from the canonical `git+https://github.com/SantyagoSeaman/sidegraph.git@main`. That
  covers the plugin and the manual `uvx --from …@main` recipe. A PyPI, tag, SHA or editable
  install never writes it. Beyond those, the only filesystem writes are the standard config
  snippets you install yourself (`.mcp.json`, hook entries). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md) for the full layout.
- **Network:** none. No remote telemetry, no phone-home, nothing leaves your machine. Local
  retrieval telemetry is recorded in the gitignored `index.db` by default (which records
  were shown and which files were touched, never uploaded); set `SIDEGRAPH_TELEMETRY=off` to
  stop new recording. The only
  network-using feature is the *optional* semantic documentation pass, which is executed
  by the separate Graphify CLI against the LLM provider you configure — not by Sidegraph.

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
overlapping valid card spans, while otherwise preserving later prose. Earlier
secret-bearing tag entities and Git history are not changed by this fix: review prior tags
and rotate any exposed credentials.

## Resource limits and redaction runtime

Capture and direct add/supersede decision, fact and domain entrypoints inspect inputs before
redaction, further model validation or canonical writes. Fixed limits cover every supplied
string, including tags, mapping keys, metadata, descriptors and attached facts:

| Resource | Limit |
|---|---|
| Each string field | 256 KiB UTF-8 |
| Admitted strings per agent or direct write request | 4 MiB UTF-8 |
| Decision/fact/domain drafts per agent request, including attached facts | 100 |
| Structure depth per draft | 32 |
| Visited structure nodes per draft | 4,096 |
| Admitted structure nodes per agent or direct write request | 65,536 |
| Each imported document | 8 MiB bytes |

There is no silent truncation or environment switch disabling these limits. Cycles and
strings that cannot encode as UTF-8 are rejected with static policy errors. A per-item
field/structure violation rejects that top-level draft, including its attached facts;
valid siblings retain normal behavior. Rejected items do not consume the shared admitted
byte/node budgets. A request-wide draft, byte or node violation rejects the request before
any canonical write. Request metadata violations also reject the request. Rejected drafts
still count toward the 100-draft ceiling. Combined MCP decisions and standalone facts share
one budget. A structure node is each scalar/container visit, including mapping keys;
root depth is zero and a leaf at depth 32 is allowed.

Document bodies use the independent per-file limit: an import can exceed 4 MiB in total or
produce more than 100 records. After path/profile/escape and existing final-component
no-follow checks, at most 8 MiB plus one byte is read from the opened file before hashing,
decoding, parsing or redaction. A file exactly at the limit is allowed; an oversized file is
skipped whole with no prefix parsing or raw fallback. Operator tags and free-text import
options are checked separately under the capture field/structure/request limits in both
real and dry runs. The explicit post-redaction `--section-limit` summary option is unchanged.

Raw Python inputs use strings, strict UTF-8 bytes/bytearray, `None`, booleans, `int`/`float`,
mappings, lists/tuples and stored Pydantic model fields. Draft batches and metadata
sequences must be lists/tuples. Other opaque values, including sets, deques, dict views,
iterators/generators and non-string Enum objects, are rejected without consuming them even
when Pydantic would otherwise coerce them. Only Enum subclasses of `str` retain their
measured base-string behavior; numeric and binary Enum subclasses are rejected before
conversion. Numeric enums can become strings in Pydantic string fields, unlike plain numbers. An unsupported nested value rejects its containing draft; unsupported
request metadata or batch shape rejects the whole request. These are data checks, not a
sandbox for caller-defined Python methods.

The bounded preflight starts at the Python entrypoint. Caller-side construction of a model
may already have validated or normalized its fields; transport framing and allocation before
MCP tool invocation are outside this boundary. Already constructed models are still checked.
The reusable `redact` helper has no global field cap.

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
`ratify`, `ratify_decisions`, `sync_anchors` and `add_anchors` arguments. Read-tool aliases
and diagnostic help retain their existing behavior; write errors list valid parameters
without repeating unknown argument names. Unsupported or unaudited tool registrations
fail closed. The compatibility adapter is tested with locked FastMCP 3.4.2 and Pydantic
2.13.4; it requires the supported argument-only core-schema shape.

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

## Bootstrap source validation

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

## Supported versions

Pre-1.0: only the latest released version receives fixes.
