"""The argument names an agent tries, and the error for one that is not a parameter.

The field study saw ``task``, ``seeds`` and ``paths`` passed to the seed-taking tools, and
FastMCP's own error does not name the valid parameters. A middleware accepts ``paths`` and
``seeds`` on ``get_task_context``, ``query_decisions`` and ``query_structure`` (a seed goes to
``files`` or ``entities`` by its shape), refuses ``task`` with a line saying there is no
free-text query, and turns every other unknown argument, on any tool, into an error that lists
the tool's real parameters.
see design/superpowers/specs/2026-10-04-tool-annotations-and-argument-names-design.md (D3, T3-T6)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import fastmcp
import pytest

from sidegraph import server
from tests.test_config_borrowed_graph import make_main
from tests.test_server_borrowed_graph import remember, serve

SEED_TOOLS = ["get_task_context", "query_decisions", "query_structure"]
FREE_TEXT_LINE = (
    "There is no free-text query: pass files or entities. intent is only a label for statistics."
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """Two files in the graph (``fn_0()`` in ``pkg/a.py``, ``fn_1()`` in ``pkg/b.py``) and a
    gotcha anchored to ``pkg/a.py``; the server runs in it."""
    root = make_main(tmp_path, files=("pkg/a.py", "pkg/b.py"))
    remember(root / ".sidegraph", "a.py keeps a retry budget", "pkg/a.py", name="fn_0()")
    serve(monkeypatch, root)
    return root


def call(name: str, args: dict):
    """The tool called through the real FastMCP dispatch, errors returned rather than raised."""

    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(run())


def text(name: str, args: dict) -> str:
    result = call(name, args)
    assert not result.is_error, result.content[0].text
    return result.content[0].text


def error(name: str, args: dict) -> str:
    result = call(name, args)
    assert result.is_error, result.content[0].text
    return result.content[0].text


@pytest.fixture
def received(monkeypatch) -> dict:
    """What ``get_task_context``'s body receives, to see the arguments after the middleware."""
    got: dict = {}

    def spy(files, entities, structure_budget, memory_budget, intent):
        got.update(files=files, entities=entities)
        return "spied"

    monkeypatch.setattr(server, "_get_task_context_with_sync", spy)
    return got


# -- T3: the aliases ---------------------------------------------------------------------


@pytest.mark.parametrize("tool", SEED_TOOLS)
@pytest.mark.parametrize("alias", ["seeds", "paths"])
def test_t3_an_alias_holding_a_path_is_files(repo, tool, alias):
    """Red today: a validation error. Mutation M4 drops ``query_decisions`` from the tools the
    middleware serves."""
    assert text(tool, {alias: ["pkg/a.py"]}) == text(tool, {"files": ["pkg/a.py"]})


def test_t3_the_path_alias_is_not_empty_text(repo):
    """The control: ``files`` itself reaches the answer, so the equalities above compare
    something."""
    assert "a.py keeps a retry budget" in text("get_task_context", {"files": ["pkg/a.py"]})


def test_t3_a_symbol_seed_is_an_entity_not_a_file(repo):
    """``files=["fn_0()"]`` is "not a repo-relative path"; the entity form resolves the symbol and
    returns its records and the map. Mutation M3 sends every seed to ``files``."""
    as_files = text("get_task_context", {"files": ["fn_0()"]})
    as_entity = text("get_task_context", {"entities": [{"name": "fn_0()"}]})

    assert as_entity != as_files  # the fixture bites
    assert text("get_task_context", {"seeds": ["fn_0()"]}) == as_entity
    assert "a.py keeps a retry budget" in as_entity


def test_t3_a_bare_string_seed_is_one_seed(repo):
    assert text("get_task_context", {"seeds": "pkg/a.py"}) == text(
        "get_task_context", {"files": ["pkg/a.py"]}
    )


def test_t3_seeds_are_routed_by_shape(received, repo):
    text(
        "get_task_context",
        {"seeds": ["pkg/a.py", "ABC-42", "x.py", "Store refresh middleware", "RetryPolicy"]},
    )

    assert received["files"] == ["pkg/a.py", "x.py"]
    assert received["entities"] == [
        {"name": "ABC-42"},
        {"name": "Store refresh middleware"},
        {"name": "RetryPolicy"},
    ]


def test_t3_a_dotted_symbol_is_an_entity_not_a_file(received, repo):
    """``Store.close`` ends in ``.close``, which is no file extension: a method reads as a symbol,
    so it reaches the graph by name where ``files`` would only say "not a repo-relative path"."""
    text(
        "get_task_context",
        {"seeds": ["Store.close", "server.mcp", "Store.refresh_if_stale", "v1.2", "README"]},
    )

    assert received["files"] is None
    assert received["entities"] == [
        {"name": "Store.close"},
        {"name": "server.mcp"},
        {"name": "Store.refresh_if_stale"},
        {"name": "v1.2"},
        {"name": "README"},
    ]


@pytest.mark.parametrize(
    "seed",
    ["a.py", "App.swift", "setup.cfg", "pyproject.toml", "notes.MD", "web/App.TSX", "lib.rs"],
)
def test_t3_a_known_file_extension_is_a_file(received, repo, seed):
    text("get_task_context", {"seeds": [seed]})

    assert received["files"] == [seed]
    assert received["entities"] is None


@pytest.mark.parametrize("seed", ["pkg/Store.close", "docs/guide", "src/"])
def test_t3_a_slash_is_a_file_whatever_the_suffix(received, repo, seed):
    text("get_task_context", {"seeds": [seed]})

    assert received["files"] == [seed]


def test_t3_free_text_holding_a_path_is_still_an_entity(received, repo):
    """A seed with whitespace is free text, even when it contains a slash or a dotted word."""
    text("get_task_context", {"seeds": ["fix the pkg/a.py retry", "see a.py today"]})

    assert received["files"] is None
    assert received["entities"] == [{"name": "fix the pkg/a.py retry"}, {"name": "see a.py today"}]


def test_t3_routed_seeds_are_appended_to_explicit_values_without_duplicates(received, repo):
    text(
        "get_task_context",
        {
            "files": ["pkg/b.py", "pkg/a.py"],
            "entities": [{"name": "fn_0()"}],
            "seeds": ["pkg/a.py", "pkg/c.py", "fn_0()", "fn_1()"],
            "paths": "pkg/c.py",
        },
    )

    assert received["files"] == ["pkg/b.py", "pkg/a.py", "pkg/c.py"]
    assert received["entities"] == [{"name": "fn_0()"}, {"name": "fn_1()"}]


def test_t3_an_alias_that_is_not_text_is_named(repo):
    message = error("get_task_context", {"seeds": 5})

    assert "`seeds`" in message and "list of strings" in message


@pytest.mark.parametrize("tool", SEED_TOOLS)
def test_t3_the_docstring_names_the_aliases(tool):
    """The schema is what an agent reads: it learns the accepted names from the description."""
    tools = {t.name: t for t in asyncio.run(_list_tools())}

    assert "paths" in tools[tool].description and "seeds" in tools[tool].description


async def _list_tools():
    async with fastmcp.Client(server.mcp) as client:
        return await client.list_tools()


def test_the_aliases_are_for_the_seed_taking_tools_only(repo):
    message = error("retrieve_decisions", {"seeds": ["pkg/a.py"]})

    assert "Unknown argument `seeds` for `retrieve_decisions`" in message


def test_list_domain_candidates_keeps_its_own_paths(repo):
    """``paths`` is a real parameter there, not an alias for ``files``."""
    assert not call("list_domain_candidates", {"paths": ["pkg/"]}).is_error


# -- T4, T5: ``task`` is not an alias ----------------------------------------------------


def test_t4_task_on_get_task_context_is_an_unknown_argument_with_the_free_text_line(repo):
    """Red today: an opaque pydantic error."""
    message = error("get_task_context", {"task": "x", "files": ["pkg/a.py"]})

    assert "Unknown argument `task` for `get_task_context`." in message
    assert (
        "Its parameters are: files, entities, structure_budget, memory_budget, intent." in message
    )
    assert FREE_TEXT_LINE in message


def test_t5_task_on_query_structure_gets_the_same_error(repo):
    message = error("query_structure", {"task": "x"})

    assert "Unknown argument `task` for `query_structure`." in message
    assert "Its parameters are: files, entities, budget_chars." in message
    assert FREE_TEXT_LINE in message


# -- T6: any other unknown argument ------------------------------------------------------


def test_t6_an_unknown_argument_names_itself_and_lists_the_real_parameters(repo):
    message = error("get_task_context", {"include_archived": False})

    assert (
        "Unknown argument `include_archived` for `get_task_context`. Its parameters are: "
        "files, entities, structure_budget, memory_budget, intent." in message
    )
    assert FREE_TEXT_LINE not in message  # only ``task`` earns that line


def test_t6_several_unknown_arguments_are_all_named(repo):
    message = error("query_decisions", {"zeta": 1, "alpha": 2, "files": ["pkg/a.py"]})

    assert "Unknown arguments `zeta`, `alpha` for `query_decisions`." in message


def test_t6_a_write_tool_gets_the_same_error_and_writes_nothing(repo):
    store = server._get_store()
    before = len(list(store.iter_decisions()))

    message = error(
        "add_decision",
        {"title": "t", "kind": "adr", "context": "c", "choice": "ch", "confidence": 1},
    )

    assert "Unknown write argument. Its parameters are: title," in message
    assert len(list(store.iter_decisions())) == before


def test_t6_a_tool_without_parameters_says_so(repo):
    message = error("list_proposed", {"x": 1})

    assert message == "Unknown argument `x` for `list_proposed`. It takes no parameters."


def test_known_arguments_pass_through_untouched(repo):
    """Nothing is renamed or dropped when every argument is a parameter."""
    assert text("get_task_context", {"files": ["pkg/a.py"], "intent": "x"})
    assert text("query_decisions", {"files": ["pkg/a.py"], "budget_chars": 500})
