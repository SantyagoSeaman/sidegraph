"""The corpus-fit pre-flight's kill rule, pinned against the study's own measured cells
and against the repository shapes a practitioner re-review broke it with.

Why this file exists: the paper claimed the rule was "validated against this study's own
cells" while shipping no fixture, no recorded output and no test — a reviewer searched for
the evidence, did not find it, and correctly called that out in a paper whose credibility
rests on never asserting an unmeasured claim. Now the claim IS the test.

Red targets: the three synthetic shapes below were each measured wrong (permissively) by
the first implementation — service monorepo and infra monorepo both returned PILOT-ish
verdicts, and the parent-directory case flipped a verdict on the name of the directory the
repository happened to be cloned into. The two real-cell tests are the guard: they must
keep matching the measured outcome after any threshold change.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docs" / "pilot-kit"))
import corpus_fit  # noqa: E402


def _write(root: Path, rel: str, size: int = 400) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x" * size, encoding="utf-8")


def _verdict(root: Path) -> str:
    return corpus_fit.verdict(corpus_fit.scan(root))[0]


# ── the shapes a reviewer built and the first version waved through ──────────────────


def test_service_monorepo_with_a_readme_per_module_is_not_waved_through(tmp_path):
    """3,000 modules + a README each: ~50% prose by bytes, still entirely grep-answerable.
    The first rule needed prose < 10% to fire, so it structurally could not reject this."""
    for i in range(3000):
        _write(tmp_path, f"services/svc{i}/main.py", 900)
        _write(tmp_path, f"services/svc{i}/README.md", 900)
    assert _verdict(tmp_path) == "DO NOT PILOT (kill rule)"


def test_infrastructure_repo_is_code_not_prose(tmp_path):
    """Terraform/YAML/SQL/proto are grep-answerable. The first version knew none of these
    extensions and reported '0 code files, prose share 100%, VERDICT: PILOT'."""
    for i in range(800):
        _write(tmp_path, f"terraform/mod{i}/main.tf")
    for i in range(1200):
        _write(tmp_path, f"k8s/app{i}/deploy.yaml")
    for i in range(300):
        _write(tmp_path, f"db/migrations/{i}.sql")
    for i in range(200):
        _write(tmp_path, f"proto/svc{i}.proto")
    for i in range(24):
        _write(tmp_path, f"docs/architecture/note{i}.md")
    m = corpus_fit.scan(tmp_path)
    assert m["code_files"] == 2500  # first version saw ZERO of these
    # 9.6 docs/1k sits between the measured losers (<=1.2) and every real ADR corpus
    # (>=35.5), so the honest verdict is the cost gate — not the PILOT the first version
    # gave, and not a kill rule stretched to cover an ambiguous shape.
    assert _verdict(tmp_path) == "PILOT ONLY WITH A COST GATE"


def test_verdict_does_not_depend_on_the_parent_directory_name(tmp_path):
    """The first version substring-matched the ABSOLUTE path, so cloning into
    ~/design-docs/ turned every file in the repo into a 'decision-shaped document'."""
    inner = tmp_path / "design-docs" / "checkout"
    for i in range(3000):
        _write(inner, f"services/svc{i}/main.py", 900)
        _write(inner, f"services/svc{i}/README.md", 900)
    assert _verdict(inner) == "DO NOT PILOT (kill rule)"


def test_adr_substring_does_not_match_an_unrelated_word(tmp_path):
    for i in range(20):
        _write(tmp_path, f"docs/quadrant-analysis-{i}.md")
    _write(tmp_path, "src/app.py")
    assert corpus_fit.scan(tmp_path)["decision_shaped_docs"] == 0


# ── the favourable shape still passes ────────────────────────────────────────────────


def test_adr_corpus_shape_still_reads_as_pilot(tmp_path):
    """150 docs/1k — the band every real ADR corpus measured in (35.5-7500)."""
    for i in range(30):
        _write(tmp_path, f"docs/adr/{i:04d}-some-decision.md", 3000)
    for i in range(200):
        _write(tmp_path, f"src/mod{i}.py", 1500)
    assert _verdict(tmp_path) == "PILOT"


def test_thin_documentation_lands_in_the_cost_gate_band(tmp_path):
    for i in range(3):
        _write(tmp_path, f"docs/adr/{i}.md")
    for i in range(300):
        _write(tmp_path, f"src/mod{i}.py")
    assert _verdict(tmp_path) == "PILOT ONLY WITH A COST GATE"


# ── the measured cells: the claim "validated against the study's own cells" ──────────

# SIDEGRAPH_SANDBOXES names the sandbox's corpus working-copy root (see
# design/testing/sandbox.md, "Two environment variables"; read by the sandbox's own
# harness/sandboxes.py). Reused here rather than a hardcoded path so this file ships without
# the maintainer's local username/layout — unset locally, this test just skips.
_SANDBOXES_DIR = os.environ.get("SIDEGRAPH_SANDBOXES")
_AIRFLOW = Path(_SANDBOXES_DIR) / "airflow" if _SANDBOXES_DIR else None
_SELF = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    _AIRFLOW is None or not _AIRFLOW.is_dir(),
    reason="SIDEGRAPH_SANDBOXES not set, or measured corpus not present there",
)
def test_measured_losing_cell_is_refused():
    """airflow — the cell that measured +25.5% cost and worse answers. Its shape as this
    script sees it: 39 decision-shaped docs across 33,028 code files = 1.2 per 1,000."""
    m = corpus_fit.scan(_AIRFLOW)
    assert m["decision_docs_per_1k_code_files"] < corpus_fit._DENSITY_FLOOR
    assert corpus_fit.verdict(m)[0] == "DO NOT PILOT (kill rule)"


def test_measured_self_corpus_reads_as_pilot():
    """This repository: 174 decision-shaped documents across 274 code files (635 per
    1,000) — the band the measured winners occupy."""
    m = corpus_fit.scan(_SELF)
    assert m["decision_docs_per_1k_code_files"] >= corpus_fit._DENSITY_GOOD
    assert corpus_fit.verdict(m)[0] == "PILOT"
