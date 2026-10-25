"""The corpus-fit pre-flight's kill rule, pinned against the repository shapes a
practitioner re-review broke it with. The study's own measured cells (the airflow and
this-repository scans) are pinned in the maintainer's release-only test set: they need the
sandbox corpora and the private ``design/`` tree.

Why this file exists: the paper claimed the rule was "validated against this study's own
cells" while shipping no fixture, no recorded output and no test — a reviewer searched for
the evidence, did not find it, and correctly called that out in a paper whose credibility
rests on never asserting an unmeasured claim. Now the claim IS the test.

Red targets: the three synthetic shapes below were each measured wrong (permissively) by
the first implementation — service monorepo and infra monorepo both returned PILOT-ish
verdicts, and the parent-directory case flipped a verdict on the name of the directory the
repository happened to be cloned into. The measured cells are the guard on thresholds:
they must keep matching after any threshold change.
"""

from __future__ import annotations

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


# The kill rule fires at corpus_fit._BIG_CODE_SURFACE (2,000 code files) with density under
# the floor. The scanner is exercised on a small real tree; the threshold is exercised on its
# metrics scaled to 2,100 modules, 5% over the threshold: a mutant that raises it to 2,200, or
# drops the surface test, flips the verdict, while the tree stays 30% smaller than the 3,000
# the first reviewer built. The "drops the surface test" mutant is killed by the
# small-repository test below instead.
_MONOREPO_MODULES = 2100
_SMALL_MODULES = 30


@pytest.fixture
def service_monorepo(tmp_path: Path) -> Path:
    """Services, a README each, cloned into a directory whose name is design-shaped.

    The tree is small and real; the tests that need the big-repository verdict scale its
    metrics. Because the root sits under ``design-docs/``, the monorepo test also fails for a
    regression that matches decision shapes on the absolute path: that parent can only ADD
    decision-shaped documents, which moves the tree away from the kill rule, so the test
    cannot pass for a wrong reason.
    """
    inner = tmp_path / "design-docs" / "checkout"
    for i in range(_SMALL_MODULES):
        _write(inner, f"services/svc{i}/main.py", 900)
        _write(inner, f"services/svc{i}/README.md", 900)
    return inner


def _at_monorepo_scale(root: Path) -> dict:
    """The scan of a small tree, as if it had _MONOREPO_MODULES modules (same shape: ~50%
    prose by bytes, no decision-shaped documents)."""
    return {**corpus_fit.scan(root), "code_files": _MONOREPO_MODULES}


def test_service_monorepo_with_a_readme_per_module_is_not_waved_through(service_monorepo):
    """2,100 modules + a README each: ~50% prose by bytes, still entirely grep-answerable.
    The first rule needed prose < 10% to fire, so it structurally could not reject this."""
    assert corpus_fit.scan(service_monorepo)["code_files"] == _SMALL_MODULES
    assert corpus_fit.verdict(_at_monorepo_scale(service_monorepo))[0] == "DO NOT PILOT (kill rule)"


def test_infrastructure_repo_is_code_not_prose(tmp_path):
    """Terraform/YAML/SQL/proto are grep-answerable. The first version knew none of these
    extensions and reported '0 code files, prose share 100%, VERDICT: PILOT'."""
    for i in range(8):
        _write(tmp_path, f"terraform/mod{i}/main.tf")
    for i in range(12):
        _write(tmp_path, f"k8s/app{i}/deploy.yaml")
    for i in range(3):
        _write(tmp_path, f"db/migrations/{i}.sql")
    for i in range(2):
        _write(tmp_path, f"proto/svc{i}.proto")
    for i in range(24):
        _write(tmp_path, f"docs/architecture/note{i}.md")
    m = corpus_fit.scan(tmp_path)
    assert m["code_files"] == 25  # first version saw ZERO of these
    # The same shape at 2,500 code files: 9.6 docs/1k sits between the measured losers (<=1.2)
    # and every real ADR corpus (>=35.5), so the honest verdict is the cost gate -- not the
    # PILOT the first version gave, and not a kill rule stretched to cover an ambiguous shape.
    scaled = {**m, "code_files": 2500, "decision_docs_per_1k_code_files": 24 / 2.5}
    assert corpus_fit.verdict(scaled)[0] == "PILOT ONLY WITH A COST GATE"


def test_verdict_does_not_depend_on_the_parent_directory_name(service_monorepo):
    """The first version substring-matched the ABSOLUTE path, so cloning into
    ~/design-docs/ turned every file in the repo into a 'decision-shaped document'."""
    assert "design-docs" in service_monorepo.parts
    assert corpus_fit.scan(service_monorepo)["decision_shaped_docs"] == 0
    assert corpus_fit.verdict(_at_monorepo_scale(service_monorepo))[0] == "DO NOT PILOT (kill rule)"


def test_a_small_repository_without_decision_docs_is_not_killed(tmp_path):
    """The kill rule needs BOTH a big code surface and a low density. 50 code files and no
    decision documents have density 0, under the floor, but nothing there is a measured losing
    shape: the verdict is the cost gate. Dropping the surface test would kill it."""
    for i in range(50):
        _write(tmp_path, f"src/mod{i}.py")
    assert corpus_fit.scan(tmp_path)["code_files"] == 50
    assert _verdict(tmp_path) == "PILOT ONLY WITH A COST GATE"


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
