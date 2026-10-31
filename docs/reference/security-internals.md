# Security internals

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
`ratify`, `ratify_decisions`, `sync_anchors` and `add_anchors` arguments. Read-tool aliases
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
