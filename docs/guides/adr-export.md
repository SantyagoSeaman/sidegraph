# Export and maintain ADRs

`sidegraph-export-adr` makes stored architectural decisions readable as Markdown ADRs.
It projects the memory you have; it does not infer a complete architecture from the code.
Preview first, then run the same command with `--write` after reviewing its diff. Commands are shown by name; without the CLI on your PATH, run them with the `uvx` prefix described in [running the CLIs](../reference/cli.md#running-the-clis-beside-the-plugin).

```bash
sidegraph-export-adr --db .sidegraph --out docs/adr
sidegraph-export-adr --db .sidegraph --out docs/adr --write
```

The default is a read-only preview. It creates no directories or files. Neither preview
nor write opens a `Store`, migrates it, updates its index, or writes `graph.json`. The source
must already be a valid canonical store; migrate legacy databases explicitly with
`sidegraph-init` first. Exported text can contain private reasoning: review the diff before
putting it in tracked or shared documentation.

## What is exported

Each accepted, superseded or deprecated `adr` or `constraint` becomes a document. Proposed
and rejected records stay out. Acceptance status is the trust boundary; legacy accepted
records need not have a ratifier-name stamp. Context, choice, rejected alternatives and
consequences come from the record. Missing optional reasoning says `Not recorded.`

A fixed footer retains status, validity, history, source pointers, durable code descriptors
and complete accepted, unclosed supporting facts. Even a future `valid_to` closes a fact;
the exporter never infers a sample size or runtime qualifier. A successor of another kind
links to its source record rather than a nonexistent ADR. `index.md` groups documents by
explicit accepted domain bindings, otherwise under Unassigned. These are membership
intent and durable descriptors, not claims of current graph resolution.

Document-derived records identify their original source and remain orientation to check
against the artifact and current code. Lessons and gotchas are not turned into ADRs or
inferred pitfalls just because they share an anchor.

## Format and filenames

The flow profile supplies output data; `generic-adr` uses Nygard-style sections and `madr`
uses MADR-style headings. Existing flow readers retain their dialects and ingest globs.

| Profile | Default output below the store's parent |
|---|---|
| `generic-adr`, `madr` | `docs/adr` |
| `superpowers` | `docs/superpowers/adr` |
| `genkovich-sdd` | `docs/features/sidegraph/adr` |
| `spec-kit` | `specs/sidegraph/adr` |
| `bmad` | `_bmad-output/sidegraph-adr` |
| `openspec` | `openspec/adr` |

These are separate ADR projections, not replacements for live flow artifacts. Creating a
profile's directory can affect structural bootstrap detection (`docs/adr`,
`docs/superpowers` or `docs/features`). Detection does not mean import candidates exist.
MADR has no detection marker. Use `--out` to choose a different directory.

```bash
sidegraph-export-adr --profile madr --out docs/adr
sidegraph-export-adr --filename '{number:04d}-{id}.md'
sidegraph-export-adr --template adr-template.txt --out docs/adr
```

Default filenames contain the stable record ID. Numbered patterns append new numbers;
existing generated records keep their number and filename when older IDs arrive or the
pattern changes. Patterns permit only `{id}` and `{number}` with an optional decimal width
of one to eight digits, must contain the ID, and produce a flat portable `.md` basename.
They cannot target `index.md`, a device name, or a path outside the output directory.

A custom template is a regular UTF-8 file up to 64 KiB, using literal text and
`string.Template` placeholders. It must include `$title`, `$context`, `$choice`, `$rejected`
and `$consequences`; `$$` prints a dollar sign. Optional fields are `$kind`, `$status`,
`$valid_from`, `$valid_to`, `$evidence`, `$anchors`, `$domains`, `$supersedes`, `$successors`,
`$record_link` and `$source`. The fixed footer remains even if the template omits these.
Default templates avoid duplicating it. Unknown placeholders or reserved marker lines are
errors. The generated ownership marker precedes the template, so template YAML frontmatter
is not at the first byte of the generated file.

## Keep human edits

A generated file has a checksummed region followed by `Local notes`. Add human commentary
after that region; updates preserve those trailing bytes. If an update writes a file whose
last line lacks a newline, it appends the repository-required final newline.

Changing generated prose, its ID or its number produces a conflict. The complete edited
file survives; the command does not replace it even with `--write`. Move additions to
Local notes, review the source record and the diff, then resolve the generated region
explicitly. There is no force-overwrite or fuzzy title-matching mode.

Checksums bind protocol version, ID, number and LF-normalized body. CRLF/BOM checkouts are
accepted, and equivalent generated content remains a no-op. This detects accidental edits;
it is not authentication against someone deliberately recomputing the checksum. Authored
opening/closing marker lines are escaped to prevent nested ownership regions. In a code
fence that escaping is visible as `&lt;`; the canonical source keeps the original text.

An unmanaged destination, duplicate generated ID/number, or case-insensitive filename
collision is a conflict. A human filename prefix of one to eight digits followed by `-`
or `_` reserves a sequence number; longer timestamps and unseparated digits do not.
Numbers stop at 99999999 with an explicit error. A later human/generated number overlap
warns and keeps both files and existing numbers unchanged. Exact destination collisions
still refuse the complete plan.
An imported human ADR already in the chosen directory is linked rather than copied.
Generated files outside the current export population remain for manual review; they are
never deleted or listed as current. Symlinked output components and nonregular targets are
refused, as is output inside the source store.

A plan containing a conflict applies no files. Writes recheck exact before bytes, create
new files exclusively and replace existing files atomically while retaining their mode.
The batch is not a transaction: an I/O failure or later concurrent edit can leave earlier
files applied, or a partial new file. The report marks an incomplete attempted write;
review the remaining diff before retrying. No protection against an adversarial ancestor
replacement or a simultaneous source/index-mode change is claimed.

## Drift and round trips

Code drift is advisory output, not rewritten ADR prose. A completed capture-to-HEAD check
can report a changed anchored file; it does not prove semantic invalidity. Missing stamps,
uncomputed binding statuses or failed checks remain unknown. Closed/archived decisions are
not applicable. Uncommitted source edits are outside this existing drift check.

The command reuses the doctor's bounded scan for an available rollback index. A WAL index
is left unopened because even SQLite read-only connections can create auxiliary WAL/SHM
files; drift is then unknown. The format check follows SQLite's
[file-header contract](https://www.sqlite.org/fileformat2.html#file_format_version_numbers).
This preserves source files rather than claiming a clean result from an unavailable check.

Generated ADRs are **projections, not new capture evidence**. Both document import and
bootstrap recognize reserved projection claims and skip them, including changed content,
foreign IDs and unknown marker versions. Fenced, indented and inline examples are ordinary
human documents. Import reports `skipped_generated_adr`; bootstrap shows
`generated-projection`. To transfer memory between repositories, use the canonical store,
not generated ADRs. This command is separate from the one-way OKF bundle export.

`--json` gives planned changes/diffs, advisories, warnings, counts and write-completion
flags. Exit 0 means successful preview/write, including a valid empty store's empty index;
2 means invalid input/store, a conflict or operational failure. Review partial-write flags
before assuming a failed write applied nothing.
