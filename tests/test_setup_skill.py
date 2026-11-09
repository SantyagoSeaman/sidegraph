"""The setup offer's source contract; see the name-domains-offer design note.

These assertions guard agent instructions, not observed model compliance. Runtime maps
remain quiet; their second-session guards live beside the respective map tests.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from sidegraph import server
from sidegraph.cli import init_main
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.store import Store

SKILL = Path("plugin/sidegraph/skills/setup/SKILL.md")
NAME_SKILL = Path("plugin/sidegraph/skills/name-domains/SKILL.md")
OFFER = (
    "Sidegraph's memory tools are connected. Would you like to name this project's "
    "domains now with sidegraph:name-domains?"
)


def _offer_block() -> str:
    text = SKILL.read_text(encoding="utf-8")
    heading = "### Offer domain naming once\n"
    assert heading in text, "setup has no early domain offer"
    return " ".join(text.split(heading, 1)[1].split("### Finish the host check", 1)[0].split())


def test_the_one_line_offer_precedes_optional_setup_questions() -> None:
    """Red on the old late handoff; deleting, duplicating or moving the offer is wrong."""
    text = SKILL.read_text(encoding="utf-8")
    assert text.count(f"> {OFFER}\n") == 1
    assert text.index("## 4. Verify the wiring") < text.index(f"> {OFFER}")
    assert text.index(f"> {OFFER}") < text.index("### Finish the host check")
    assert text.index(f"> {OFFER}") < text.index("## 5. Keep the graph fresh")
    handoff = text.split("## 7. First memory", 1)[1].split("## Reset", 1)[0]
    assert "sidegraph:name-domains" not in handoff
    body = text.split("\n---\n", 1)[1]
    outside = body.split("### Offer domain naming once", 1)[0]
    outside += body.split("### Finish the host check", 1)[1]
    assert "name-domains" not in outside
    assert not re.search(r"name[^.\n]{0,40}domains", outside, re.IGNORECASE)


@pytest.mark.parametrize(
    "instruction",
    [
        'list_domains(status="accepted")',
        'list_domains(status="proposed")',
        "both results' `matched` counts are `0`",
        "an empty `domains` array does not prove zero matches",
        "store from step 3 exists",
        "graph from step 2 is available",
        "If either call fails or a result is malformed, do not offer",
        "list_domain_candidates(min_members=0, limit=1)",
        "`graph_version` is not null and `total_significant > 0`",
        "a missing or unreadable graph, no communities, "
        "or a failed/malformed result means no offer",
        "wait for the human's reply",
        "On yes, invoke `sidegraph:name-domains` now",
        "On no or defer, continue setup",
        "then resume this setup flow here",
        "Do not repeat the offer in the final handoff or in later ordinary sessions",
    ],
)
def test_the_offer_requires_verified_empty_live_domains_and_a_graph(instruction: str) -> None:
    """Red on the old skill; each missing predicate or duplicate-offer instruction matters."""
    assert instruction in _offer_block()


def test_a_setup_yes_only_opens_the_naming_conversation() -> None:
    """Red on the old handoff; an offer response must not authorize domain writes."""
    assert "it does not pick a domain set or authorize domain writes" in _offer_block()
    naming = NAME_SKILL.read_text(encoding="utf-8")
    assert "before the human has explicitly picked\none of the sets you present" in naming


@pytest.mark.parametrize("status", ["accepted", "proposed"])
def test_live_domain_count_survives_an_empty_bounded_response(tmp_path: Path, status: str) -> None:
    """Producer guard: an oversized real row can leave domains empty while matched is one."""
    store = Store(tmp_path / ".sidegraph")
    try:
        store.add_domain(
            Domain(
                slug="large",
                title="Large",
                summary="x" * 5000,
                status=DomainStatus(status),
                provenance=Provenance(source="manual"),
            )
        )
        response = json.loads(server._domain_listing(store, status=status, budget_chars=4000))
        assert response["matched"] == 1
        assert response["domains"] == []
    finally:
        store.close()


def test_community_presence_uses_the_full_count_before_the_limit(tmp_path: Path) -> None:
    """Producer guard: the setup presence check sees both communities despite limit=1."""
    store = Store(tmp_path / ".sidegraph")
    try:
        reader = GraphifyReader(Path("tests/fixtures/mini_graph.json"))
        response = server._list_domain_candidates_impl(store, reader, min_members=0, limit=1)
        assert response["graph_version"] is not None
        assert response["total_significant"] == 2
        assert response["total_candidates"] == 1
        missing = server._list_domain_candidates_impl(store, None, min_members=0, limit=1)
        assert missing["graph_version"] is None
        assert missing["total_significant"] == 0
    finally:
        store.close()


def test_hook_installation_question_is_deferred_until_after_the_offer() -> None:
    """Red on the early step-3 question that can interrupt setup before the offer."""
    prefix = SKILL.read_text(encoding="utf-8").split("### Offer domain naming once", 1)[0]
    assert "Defer the hook question to step 5" in prefix
    prefix = " ".join(prefix.split("\n---\n", 1)[1].split())
    for question in re.finditer(r"\b(ask|put (that|the \w+) question)\b", prefix, re.IGNORECASE):
        assert re.search(r"\bnot\s*$", prefix[max(0, question.start() - 12) : question.start()])


def test_initialization_is_verified_before_any_mcp_wiring_read() -> None:
    """Red on checking existence after list_domains can auto-create a root store."""
    wiring = SKILL.read_text(encoding="utf-8").split("## 4. Verify the wiring", 1)[1]
    prefix = " ".join(wiring.split("Call `list_domains()`", 1)[0].split())
    for instruction in (
        "Before any MCP call",
        "step 3 to exit successfully",
        "`created store:` or `already initialized:`",
        "`empty store:`",
        "`found graph:`",
        "Verify that both reported paths exist",
        "stop and repair steps 2–3",
        "Do not call MCP tools to create a missing store",
    ):
        assert instruction in prefix


def test_no_mcp_call_precedes_the_initialization_guard() -> None:
    """The design's pre-read guard applies to earlier setup steps too."""
    prefix = SKILL.read_text(encoding="utf-8").split("Before any MCP call", 1)[0]
    assert not re.search(r"`[a-z_]\w*\(", prefix)


def test_existing_empty_store_success_is_allowed_before_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Producer regression: a successful empty-store retry must still reach the offer."""
    skill = SKILL.read_text(encoding="utf-8")
    graph_bytes = Path("tests/fixtures/mini_graph.json").read_bytes()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    graph = tmp_path / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_bytes(graph_bytes)
    db = tmp_path / ".sidegraph"
    Store(db).close()
    monkeypatch.chdir(tmp_path)
    assert init_main(["--db", str(db), "--graph", str(graph), "--no-settings"]) == 0
    output = capsys.readouterr().out.splitlines()
    store_message = output[0].split(":", 1)[0] + ":"
    assert store_message == "empty store:"
    assert f"found graph: {graph}" in output
    assert db.is_dir() and graph.is_file()
    wiring = skill.split("## 4. Verify the wiring", 1)[1].split("Call `list_domains()`", 1)[0]
    allowed = re.findall(r"`([^`]+ store:|already initialized:)`", wiring)
    assert store_message in allowed


def test_reset_list_says_remove_hooks_leaves_the_ignore_block() -> None:
    """Red target: the `.ignore` bullet sat between the git-config bullet and its
    `--remove-hooks clears it` line, so the cleanup list claimed init's removal flag clears it."""
    text = SKILL.read_text().split("## Reset / cleanup", 1)[1].split("## See also")[0]
    reset = " ".join(text.split())
    config = "recorded by `sidegraph-init --no-hooks`. `--remove-hooks` clears it."
    assert config in reset
    assert "`--remove-hooks` leaves it; delete the block by hand." in reset
