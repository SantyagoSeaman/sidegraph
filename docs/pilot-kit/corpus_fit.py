#!/usr/bin/env python3
"""Corpus-fit pre-flight: is this repository on the paying side of the cost model?

Run BEFORE installing anything:

    python3 docs/pilot-kit/corpus_fit.py /path/to/repo

The whitepaper's measured result is a corpus-kind split, not a universal saving: memory
paid where retrieval replaced reading DECISION PROSE (-18.4% on a document-heavy ADR
corpus) and cost where it replaced grepping CODE (+25.5% and worse answers on a large
Python monorepo). This script estimates that shape and applies a kill rule. It reads file
names and sizes — it installs nothing, writes nothing, and sends nothing anywhere.

It is a SCREEN, not a prediction: a favourable verdict means "worth a pilot", never
"a saving is expected". The pilot's own cost gate is the real test.

Design notes, every one of them a lesson from a reviewer who RAN this rather than read it
(2026-08-04 practitioner re-review): the first version got two of his three real
repository shapes wrong, both times permissively.

- **Configuration and IaC count as code.** A repository of Terraform, YAML, SQL and
  protobuf is one where grep answers; the first version knew none of those extensions and
  reported "0 code files, 100% prose, PILOT" for an infrastructure monorepo.
- **Decision-shaped documents are matched on path SEGMENTS, relative to the scanned
  root.** The first version substring-matched the absolute path, so a checkout living
  under `~/design-docs/` counted every document in the repository as decision-shaped and
  flipped the verdict. ("adr" as a bare substring also matched "quadrant".)
- **The discriminator is decision prose PER CODE SURFACE, not prose bytes.** A service
  monorepo with a README beside every module is ~50% prose by bytes and still entirely
  grep-answerable, so a prose-share threshold cannot fire on it. What separates the
  measured winner from the measured loser is how much *decision* writing exists relative
  to how much code an agent must navigate.

`tests/test_pilot_kit_corpus_fit.py` pins the verdicts on both measured cells and on the
three shapes above, so "validated against the study's own cells" is a test, not a claim.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROSE_EXT = {".md", ".rst", ".adoc", ".txt", ".org"}

# Anything an agent greps rather than reads for rationale: programming languages AND the
# configuration/IaC/markup surface that dominates real infrastructure repositories.
CODE_EXT = {
    # programming languages
    ".py",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".scala",
    ".swift",
    ".m",
    ".mm",
    ".sh",
    ".bash",
    ".zsh",
    ".pl",
    ".lua",
    ".jl",
    ".ex",
    ".exs",
    ".erl",
    ".hs",
    ".clj",
    ".dart",
    ".vue",
    ".svelte",
    # configuration, IaC, schema, build — grep-answerable, never rationale
    ".tf",
    ".tfvars",
    ".hcl",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".properties",
    ".xml",
    ".sql",
    ".proto",
    ".graphql",
    ".gradle",
    ".bzl",
    ".cmake",
    ".tpl",
    ".jinja",
    ".j2",
}
CODE_FILENAMES = {"Dockerfile", "Makefile", "BUILD", "WORKSPACE", "Jenkinsfile", "Rakefile"}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    "out",
    "vendor",
    "third_party",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".gradle",
    ".next",
    ".nuxt",
    "Pods",
    "bazel-out",
    "bazel-bin",
    ".terraform",
    "coverage",
    "htmlcov",
    ".idea",
    ".vscode",
    ".sidegraph",
    "graphify-out",
}

# A path SEGMENT (directory name, or the file's own stem) that marks decision-shaped
# writing. Matched against the path RELATIVE to the scanned root — never the absolute
# path, which belongs to whoever cloned the repository, not to the repository.
DECISION_SEGMENTS = {
    "adr",
    "adrs",
    "decision",
    "decisions",
    "rfc",
    "rfcs",
    "architecture",
    "design",
    "designs",
    "proposals",
}

# Thresholds derived from the measured corpora, not from intuition. Decision-shaped
# documents per 1,000 code/config files, measured by this script's own `scan()`:
#
#     airflow      1.2/1k   (33,028 code files)  MEASURED LOSER  +25.5% and worse answers
#     xgboost      0.1/1k   (10,873)             measured wash
#     turtles     51.6/1k   (368)                foreign ADR corpus
#     govuk       35.5/1k   (677)                foreign ADR corpus
#     self-corpus  635/1k   (274)                measured -8.4%
#     private-1   7500/1k   (2)                  MEASURED WINNER -18.4%
#
# The measured losers sit at or below 1.2; every ADR-cultured corpus sits at or above
# 35.5. The floor is set just above the losers and the "good" band just below the
# thinnest real ADR corpus, so the wide middle lands in the cost gate rather than being
# waved through in either direction.
_BIG_CODE_SURFACE = 2000  # files; the losing cell carried 33,028
_DENSITY_FLOOR = 2.0
_DENSITY_GOOD = 25.0
_MIN_DECISION_DOCS = 10


def _is_decision_doc(rel: Path) -> bool:
    """True when a path segment (directory or file stem) names decision-shaped writing."""
    if {s.lower() for s in rel.parts[:-1]} & DECISION_SEGMENTS:
        return True
    stem = rel.stem.lower()
    return any(
        stem == seg or stem.startswith(f"{seg}-") or stem.startswith(f"{seg}_")
        for seg in DECISION_SEGMENTS
    )


def scan(root: Path) -> dict:
    prose = code = 0
    prose_files = code_files = decision_docs = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        ext = p.suffix.lower()
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if ext in PROSE_EXT:
            prose += size
            prose_files += 1
            if _is_decision_doc(rel):
                decision_docs += 1
        elif ext in CODE_EXT or p.name in CODE_FILENAMES:
            code += size
            code_files += 1
    density = (1000 * decision_docs / code_files) if code_files else float(decision_docs)
    return {
        "prose_bytes": prose,
        "code_bytes": code,
        "prose_files": prose_files,
        "code_files": code_files,
        "decision_shaped_docs": decision_docs,
        "prose_share": prose / (prose + code) if (prose + code) else 0.0,
        "decision_docs_per_1k_code_files": density,
    }


def verdict(m: dict) -> tuple[str, list[str]]:
    """The kill rule, anchored to the measured cells (whitepaper §8.5 and §10.1)."""
    docs = m["decision_shaped_docs"]
    density = m["decision_docs_per_1k_code_files"]
    code_files = m["code_files"]
    reasons: list[str] = []

    if code_files >= _BIG_CODE_SURFACE and density < _DENSITY_FLOOR:
        reasons.append(
            f"{code_files} code/config files carrying only {docs} decision-shaped "
            f"document(s) ({density:.1f} per 1,000 code files) — the shape of the cell "
            "where memory measured +25.5% cost AND worse answers; grep already answers here"
        )
        return "DO NOT PILOT (kill rule)", reasons

    if docs >= _MIN_DECISION_DOCS and density >= _DENSITY_GOOD:
        reasons.append(
            f"{docs} decision-shaped documents at {density:.1f} per 1,000 code files — "
            "the shape of the corpus where memory measured cheapest"
        )
        return "PILOT", reasons

    reasons.append(
        f"{docs} decision-shaped document(s), {density:.1f} per 1,000 code files, "
        f"{code_files} code/config files — between the measured poles; no cell of the "
        "study looks like this"
    )
    if docs < _MIN_DECISION_DOCS:
        reasons.append(
            "few decision-shaped documents: bootstrap import will seed little (measured: "
            "`rejected` filled on 3 of 41 foreign ADRs) — capture, not import, would have "
            "to carry the pilot"
        )
    return "PILOT ONLY WITH A COST GATE", reasons


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 1
    m = scan(root)
    v, reasons = verdict(m)
    print(f"corpus-fit pre-flight — {root}")
    print(f"  prose      : {m['prose_bytes'] / 1024:>9.0f} KiB in {m['prose_files']} files")
    print(f"  code/config: {m['code_bytes'] / 1024:>9.0f} KiB in {m['code_files']} files")
    print(
        f"  decision-shaped documents: {m['decision_shaped_docs']} "
        f"({m['decision_docs_per_1k_code_files']:.1f} per 1,000 code files)"
    )
    print(f"  prose share of text: {m['prose_share']:.0%}  (reported, not decisive)")
    print(f"\nVERDICT: {v}")
    for r in reasons:
        print(f"  - {r}")
    print(
        "\nThis is a screen, not a prediction. A favourable verdict means the corpus "
        "resembles the cells where memory paid; it does not promise a saving. Run the "
        "pilot's own cost gate before scaling."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
