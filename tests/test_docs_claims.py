"""Guards for the numeric doc claims the 2026-08-02 docs sweep found stale, with no mirror
test to catch the next drift (design/testing/2026-08-02-bootstrap-docs-sweep.md §3, findings
1-2). Each test below derives its expected number/set from the actual source of truth
(pyproject.toml, doctor.py's own ``Finding(...)`` call sites, the guide's own headings, or
``os.environ.get``/``os.getenv`` call sites in ``src/``) and never hard-codes the count
itself -- a test that pins ``== 15`` would go stale exactly the way the prose it guards did.

Two already-guarded claim classes are the model this file extends (README's own
``test_console_script_count_in_cli_reference_matches_pyproject`` in test_bootstrap_cli.py,
and ``test_docs_bootstrap_guide_mirrors_every_consequence_sentence`` in
test_bootstrap_review.py). README's own test-count sentence (2,339 collected / 2,196 public)
is deliberately NOT guarded here: it changes on every commit that adds or removes a test, so
a guard would fight the suite rather than protect it -- see the sweep doc's §3 finding 4.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from sidegraph import doctor as doctor_module

_ROOT = Path(__file__).resolve().parent.parent

# Small vocabulary for the "N-case checklist" prose, which spells the count out as a word.
# Bounded generously past the current count (8) so a modest future growth in the checklist
# doesn't require touching this list; an out-of-range count fails loudly instead of silently
# mis-deriving a word.
_NUMBER_WORDS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
]


# ---------------------------------------------------------------------------
# 1. installation.md's entry-points table <-> pyproject.toml's [project.scripts]
# ---------------------------------------------------------------------------


def _pyproject_script_names() -> set[str]:
    """Every entry-point name in ``[project.scripts]``, whatever its own name looks like --
    not narrowed to a ``sidegraph-`` prefix. A prefix-scoped pattern only ever proves the
    namespace is right; it can't see a script the table is simply missing, which is the
    actual failure mode this guard exists for."""
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return set(re.findall(r"^([\w-]+)\s*=", block, re.M))


_ENTRY_POINT_ROW_RE = re.compile(r"^\| `([\w-]+)` \|")


def _installation_doc_entry_point_names() -> set[str]:
    doc = _ROOT / "docs" / "getting-started" / "installation.md"
    lines = doc.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Command |"))
    names: set[str] = set()
    for line in lines[start + 2 :]:  # skip the header row and its `|---|---|` separator
        match = _ENTRY_POINT_ROW_RE.match(line)
        if match is None:
            break
        names.add(match.group(1))
    return names


def test_installation_entry_points_table_matches_pyproject_scripts_bidirectionally():
    """Red against unfixed docs: the entry-points table was missing sidegraph-doctor and
    sidegraph-export-okf (13 of 15) because the original sweep only checked
    doc-row-exists-in-pyproject, never the reverse -- a script silently dropped from the
    table would have passed that one-directional check forever. Asserts full set equality,
    both directions: neither a row missing from the table nor a stale row naming a script
    pyproject no longer registers survives. Both sides are also asserted non-empty first --
    an empty-vs-empty comparison from two independently broken parsers would pass this
    equality silently."""
    doc_names = _installation_doc_entry_point_names()
    script_names = _pyproject_script_names()
    assert doc_names, "parsed zero rows from installation.md's entry-points table"
    assert script_names, "parsed zero scripts from pyproject.toml's [project.scripts]"
    assert doc_names == script_names


# ---------------------------------------------------------------------------
# 2. cli.md's sidegraph-doctor finding-code table <-> the codes doctor.py actually emits
# ---------------------------------------------------------------------------


def _doctor_emitted_finding_codes() -> set[str]:
    """Every distinct code string a ``Finding(...)`` call in doctor.py can construct.

    Derived by walking the module's own AST rather than hand-listing the "pinned
    constants" comment block: that block only covers 8 of the 9 real codes (NEVER_SURFACED
    is declared separately, a few lines below it, for its own documented reason), which is
    exactly how the sweep's manual re-derivation under-counted and how code-drift went
    missing from the doc table in the first place. Two argument shapes are resolved: a
    direct ``Finding(SOME_CONSTANT, ...)`` call, and the one indirection doctor.py uses
    (``code = {"degraded": DEGRADED_BINDING, "orphaned": ORPHANED_BINDING}.get(status)``
    followed by ``Finding(code, ...)``) -- both branches of that dict are counted as
    reachable.
    """
    source = inspect.getsource(doctor_module)
    tree = ast.parse(source)

    # name -> every string value that name could hold, built from both direct string
    # assignments and the one dict-`.get()` indirection this module uses.
    name_values: dict[str, set[str]] = {}

    def record(name: str, value_node: ast.AST) -> None:
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            name_values.setdefault(name, set()).add(value_node.value)
        elif isinstance(value_node, ast.Dict):
            for v in value_node.values:
                if isinstance(v, ast.Name) and v.id in name_values:
                    name_values.setdefault(name, set()).update(name_values[v.id])
        elif isinstance(value_node, ast.Call):
            func = value_node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                obj = func.value
                if isinstance(obj, ast.Name) and obj.id in name_values:
                    name_values.setdefault(name, set()).update(name_values[obj.id])

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    record(target.id, node.value)

    codes: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Finding"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                codes.add(first.value)
            elif isinstance(first, ast.Name):
                codes.update(name_values.get(first.id, set()))
    return codes


_FINDING_CODE_ROW_RE = re.compile(r"^\| `([a-z-]+)` \|")


def _cli_doc_finding_codes() -> set[str]:
    doc = _ROOT / "docs" / "reference" / "cli.md"
    lines = doc.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Code | Meaning |"))
    codes: set[str] = set()
    for line in lines[start + 2 :]:  # skip the header row and its `|---|---|` separator
        match = _FINDING_CODE_ROW_RE.match(line)
        if match is None:
            break
        codes.add(match.group(1))
    return codes


def test_doctor_finding_code_table_matches_doctor_py_emitted_codes():
    """Red against unfixed docs: the table was missing code-drift (8 of 9 real codes) --
    the original sweep's own re-derivation command matched dozens of unrelated quoted
    strings and never actually recomputed the 9. Asserts full set equality against codes
    derived from doctor.py's own Finding(...) call sites, not a hand-maintained list. Both
    sides are also asserted non-empty first -- an empty-vs-empty comparison from two
    independently broken parsers would pass this equality silently."""
    doc_codes = _cli_doc_finding_codes()
    emitted_codes = _doctor_emitted_finding_codes()
    assert doc_codes, "parsed zero rows from cli.md's finding-code table"
    assert emitted_codes, "parsed zero Finding(...) codes out of doctor.py"
    assert doc_codes == emitted_codes


# ---------------------------------------------------------------------------
# 3. "N-case checklist" (installation.md, docs/llms.txt) <-> verifying-your-setup.md's
#    actual `## Case` count
# ---------------------------------------------------------------------------


def _verifying_setup_case_count() -> int:
    doc = _ROOT / "docs" / "guides" / "verifying-your-setup.md"
    lines = doc.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if re.match(r"^## Case \d+", line))


def test_case_count_claims_match_verifying_your_setup_guide():
    """Red against a stale count: installation.md and llms.txt both spell out the checklist
    length in prose ("an eight-case checklist" / "an eight-case post-install checklist").
    Derives the real count from verifying-your-setup.md's own `## Case N` headings and
    checks both citing docs spell out the matching word -- a guide that gains or loses a
    case without both citations being updated goes red here instead of drifting silently,
    which is exactly what the sweep found with installation.md's prior "seven-case" text."""
    actual = _verifying_setup_case_count()
    assert actual > 0
    assert actual < len(_NUMBER_WORDS), (
        f"{actual} cases exceeds the small number-word vocabulary this test knows; extend "
        "_NUMBER_WORDS"
    )
    word = _NUMBER_WORDS[actual]

    installation_doc = (_ROOT / "docs" / "getting-started" / "installation.md").read_text(
        encoding="utf-8"
    )
    llms_doc = (_ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")

    assert f"{word}-case checklist" in installation_doc
    assert f"{word}-case post-install checklist" in llms_doc


# ---------------------------------------------------------------------------
# 4. configuration.md's environment-variable count/table <-> the SIDEGRAPH_* names src/
#    actually reads via os.environ.get(...) or os.getenv(...)
# ---------------------------------------------------------------------------

_ENV_READ_RE = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["'](SIDEGRAPH_[A-Z_]+)["']""")


def _src_env_var_names() -> set[str]:
    """Every ``SIDEGRAPH_*`` name actually read via ``os.environ.get(...)`` or
    ``os.getenv(...)`` under src/. Both accessors are covered -- a name that moved (or was
    newly introduced) under the other one must still be caught.

    Deliberately narrower than "every SIDEGRAPH_ substring in src/": that would also catch
    the two viz template placeholders (``__SIDEGRAPH_VIS_JS__``, ``__SIDEGRAPH_GRAPH_JSON__``
    in viz/render.py and viz/template.html), which are string-substitution markers, not
    environment variables, and are correctly excluded from configuration.md.
    """
    names: set[str] = set()
    for path in (_ROOT / "src" / "sidegraph").rglob("*.py"):
        names.update(_ENV_READ_RE.findall(path.read_text(encoding="utf-8")))
    return names


_ENV_VAR_ROW_RE = re.compile(r"^\| `(SIDEGRAPH_[A-Z_]+)` \|")


def _configuration_doc_env_var_names() -> set[str]:
    doc = _ROOT / "docs" / "reference" / "configuration.md"
    lines = doc.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Variable |"))
    names: set[str] = set()
    for line in lines[start + 2 :]:  # skip the header row and its `|---|---|---|` separator
        match = _ENV_VAR_ROW_RE.match(line)
        if match is None:
            break
        names.add(match.group(1))
    return names


def test_environment_variable_claims_match_source_bidirectionally():
    """Red against a stale count: the lead sentence said "eight environment variables" while
    the table itself already had all 9 correct rows, and nothing checked the sentence against
    either the table or the source. Derives the real set from every SIDEGRAPH_* name src/
    actually reads via os.environ.get(...) or os.getenv(...) and asserts full equality
    against the table, both directions, plus checks the lead sentence's word matches the
    table's own row count."""
    src_names = _src_env_var_names()
    doc_names = _configuration_doc_env_var_names()
    assert doc_names == src_names

    actual = len(doc_names)
    assert actual > 0
    assert actual < len(_NUMBER_WORDS), (
        f"{actual} environment variables exceeds the small number-word vocabulary this test "
        "knows; extend _NUMBER_WORDS"
    )
    word = _NUMBER_WORDS[actual]

    lead_sentence = f"Sidegraph has {word} environment variables."
    doc_text = (_ROOT / "docs" / "reference" / "configuration.md").read_text(encoding="utf-8")
    assert lead_sentence in doc_text


# ---------------------------------------------------------------------------
# 5. cli.md's per-command sections <-> the option strings each entry point's parser defines
# ---------------------------------------------------------------------------


def _entry_point_targets() -> dict[str, tuple[str, str]]:
    """``sidegraph-x`` -> (module, function) from ``[project.scripts]``.

    Read from pyproject rather than hard-coded so a renamed or newly added entry point is
    picked up by this guard the same commit it lands, not the next time someone remembers.
    """
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    pairs = re.findall(r'^([\w-]+)\s*=\s*"([\w.]+):(\w+)"', block, re.M)
    return {name: (mod, func) for name, mod, func in pairs}


def _parser_option_strings(module: str, func: str) -> set[str]:
    """Every ``--flag`` literal the named entry point's parser defines.

    Scoped to the entry function's own body first. When that body defines none, it either
    (a) genuinely takes no flags (e.g. ``sidegraph.cli:prepare_commit_msg_main``, a git hook
    reading positional argv, not argparse) or (b) delegates to a helper elsewhere in the
    same module (``sidegraph.bootstrap.cli:main`` -> module-level ``build_parser()``).
    Those two shapes are indistinguishable from "scoped is empty" alone, so this traces ONE
    level of same-module function calls the entry function actually makes and scans only
    those -- never the whole module. A whole-module fallback (git-bindings design wave,
    round 2 fix) wrongly attributed every OTHER command's flags to a genuinely flag-less
    command sharing ``sidegraph.cli`` with a dozen others; scoping to actual callees keeps
    ``sidegraph-bootstrap``'s build_parser() delegation working while correctly reporting
    zero flags for one with none.
    """
    path = _ROOT.joinpath("src", *module.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def flags_under(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if not (isinstance(sub.func, ast.Attribute) and sub.func.attr == "add_argument"):
                continue
            for arg in sub.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    found.add(str(arg.value))
        return found

    def called_names(node: ast.AST) -> set[str]:
        return {
            sub.func.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            scoped = flags_under(node)
            if scoped:
                return scoped
            called = called_names(node)
            delegated: set[str] = set()
            for other in ast.walk(tree):
                if isinstance(other, ast.FunctionDef) and other.name in called:
                    delegated |= flags_under(other)
            return delegated
    return flags_under(tree)


def _cli_doc_sections() -> dict[str, str]:
    """``sidegraph-x`` -> the text of its ``## `sidegraph-x``` section in cli.md."""
    lines = (_ROOT / "docs" / "reference" / "cli.md").read_text(encoding="utf-8").splitlines()
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        heading = re.match(r"^## `(sidegraph-[\w-]+)`", line)
        if heading is not None:
            current = sections.setdefault(heading.group(1), [])
        elif line.startswith("## "):
            current = None  # a non-command section ends the previous command's scope
        elif current is not None:
            current.append(line)
    return {name: "\n".join(body) for name, body in sections.items()}


def test_cli_reference_documents_every_option_each_parser_defines():
    """Red against unfixed docs: ``sidegraph-import --any-doc`` shipped undocumented -- the
    flag, and the ``outside-profile`` skip it controls, appeared nowhere in docs/, so a user
    whose explicit ``--docs`` path fell outside the profile's globs saw "0 imported" with no
    documented explanation. The pre-existing guard for this class
    (``test_resume_flag_is_documented_as_an_idempotent_marker``) only ever asserted against
    ``format_help()``, never against cli.md, which is exactly how cli.md's own ``--resume``
    row stayed stale describing a distinct mode after the help text was corrected.

    Derives the expected flags from each parser's own ``add_argument`` literals, so a flag
    added tomorrow fails this test until cli.md's section for that command mentions it.
    One-directional by design: a doc-only string that no parser defines is a different
    failure, and matching prose that merely *mentions* another command's flag would make a
    two-directional assertion fire constantly.

    Scope, stated so it isn't over-trusted: this asserts the flag appears **somewhere** in
    its command's section -- a usage synopsis or an example counts. Deleting a flag's row
    from the flag table while it survives in the synopsis does NOT go red (verified by
    mutation). It catches the flag documented nowhere, which is the defect it was written
    for; it does not police where in the section the mention lives."""
    sections = _cli_doc_sections()
    assert sections, "parsed zero command sections from cli.md"

    checked = 0
    for name, (module, func) in sorted(_entry_point_targets().items()):
        flags = _parser_option_strings(module, func)
        if not flags:
            continue  # sidegraph-mcp and the three hooks take no arguments
        assert name in sections, f"cli.md has no `## `{name}`` section"
        body = sections[name]
        missing = sorted(f for f in flags if not re.search(rf"{re.escape(f)}\b", body))
        assert not missing, f"{name}: cli.md's section documents none of {missing}"
        checked += 1
    assert checked >= 10, f"only {checked} commands had parser flags -- extraction likely broke"


# ---------------------------------------------------------------------------
# 6. every relative link and heading anchor inside docs/ resolves
# ---------------------------------------------------------------------------


def _heading_anchor(heading: str) -> str:
    """GitHub's slug for a heading: lowercase, drop everything outside word/space/hyphen,
    then replace each space with a hyphen. Runs of spaces are NOT collapsed -- ``Option D
    — from source`` yields ``option-d--from-source`` (two hyphens, the stripped em dash's
    surrounding spaces), and collapsing them would make this guard report working links as
    broken."""
    slug = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return slug.replace(" ", "-")


_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def test_every_relative_doc_link_and_anchor_resolves():
    """Red against unfixed docs: integrations/claude-code.md pointed at
    ``installation.md#option-c--from-source-contributors`` while "from source (contributors)"
    is Option **D** -- Option C is the uvx-from-git path. A wrong-but-plausible anchor lands
    the reader at the top of the page with no error, so nothing but a check like this finds
    it. Covers link targets outside docs/ too (the plugin skills), since those are the links
    most likely to rot when a skill is renamed."""
    docs = sorted((_ROOT / "docs").rglob("*.md"))
    assert docs, "found no markdown under docs/"

    anchors: dict[Path, set[str]] = {}
    for path in _ROOT.rglob("*.md"):
        headings = re.findall(r"^#{1,6}\s+(.*)$", path.read_text(encoding="utf-8"), re.M)
        anchors[path.resolve()] = {_heading_anchor(h) for h in headings}

    broken: list[str] = []
    for path in docs:
        text = path.read_text(encoding="utf-8")
        for match in _MD_LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            line = text[: match.start()].count("\n") + 1
            where = f"{path.relative_to(_ROOT)}:{line}"
            file_part, _, fragment = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path.resolve()
            if not dest.exists():
                broken.append(f"{where}: missing file -> {target}")
            elif fragment and dest.suffix == ".md" and fragment not in anchors.get(dest, set()):
                broken.append(f"{where}: missing anchor -> {target}")
    assert not broken, "broken links:\n" + "\n".join(broken)


# ---------------------------------------------------------------------------
# 7. stability.md's surface inventory <-> what those surfaces actually are
# ---------------------------------------------------------------------------


def _stability_doc() -> str:
    return (_ROOT / "docs" / "reference" / "stability.md").read_text(encoding="utf-8")


def test_stability_page_counts_match_the_surfaces_they_describe():
    """A stability contract with stale numbers is worse than none: it is read exactly when
    someone is deciding what to build on. Every count below is re-derived from the surface
    itself -- pyproject's scripts, each parser's own add_argument literals, server.py's
    ``@mcp.tool`` decorators, the SIDEGRAPH_* names src/ reads -- so the page fails the suite
    the day a surface grows, instead of quietly describing last month's product.

    This test is also the page's own rule applied to itself: a surface is only called
    Committed once something mechanical checks it.
    """
    doc = _stability_doc()

    entry_points = _entry_point_targets()
    flag_total = sum(len(_parser_option_strings(mod, fn)) for mod, fn in entry_points.values())
    assert f"{len(entry_points)} entry points, {flag_total} flags" in doc

    tools = _mcp_tool_names()
    assert f"({len(tools)} tools)" in doc

    env_names = _src_env_var_names()
    assert f"The {len(env_names)} in" in doc


def _mcp_tool_names() -> set[str]:
    """Every function in server.py carrying an ``@mcp.tool`` decorator.

    AST rather than a ``grep -c "@mcp.tool"``: that count returns 25 on this file because one
    occurrence sits in prose, and a stability page that advertised 25 tools would be wrong in
    the direction that matters -- promising a surface that isn't there.
    """
    tree = ast.parse((_ROOT / "src" / "sidegraph" / "server.py").read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Attribute) and d.attr == "tool" for d in node.decorator_list)
    }


def test_stability_page_schema_version_and_upgrade_sets_match_the_code():
    """The store row is the page's strongest promise -- "a future bump migrates or reloads,
    it never orphans a store" -- and it names the exact versions that do each. Derived from
    schema.SCHEMA_VERSION and store's own frozensets so the promise cannot outlive them."""
    from sidegraph.schema import SCHEMA_VERSION
    from sidegraph.store import _MIGRATABLE_SCHEMA_VERSIONS, _RELOADABLE_SCHEMA_VERSIONS

    doc = _stability_doc()
    assert f"**{SCHEMA_VERSION}**" in doc
    for version in _MIGRATABLE_SCHEMA_VERSIONS:
        assert f"`{version}`" in doc, f"migratable {version} missing from stability.md"
    for version in _RELOADABLE_SCHEMA_VERSIONS:
        assert f"`{version}`" in doc, f"reloadable {version} missing from stability.md"


def test_stability_page_lists_every_deprecated_mcp_tool():
    """`ratify_decisions` is the page's evidence that the MCP surface is Provisional -- a tool
    deprecated before 1.0 shipped. Any FUTURE deprecation is the same evidence and must appear
    too, or the page keeps arguing from one stale example."""
    doc_text = (_ROOT / "docs" / "reference" / "mcp-tools.md").read_text(encoding="utf-8")
    deprecated = set(re.findall(r"^## `([a-z_]+)` \(deprecated\)", doc_text, re.M))
    assert deprecated, "parsed zero deprecated tools from mcp-tools.md's headings"
    stability = _stability_doc()
    for name in deprecated:
        assert f"`{name}`" in stability, f"{name} is deprecated but stability.md never says so"


# ---------------------------------------------------------------------------
# 8. "N supported ADR/spec profiles" (installation.md, stability.md) <-> len(PROFILES) in
#    src/sidegraph/profiles.py
# ---------------------------------------------------------------------------


def test_profile_count_claims_match_profiles_module():
    """Red against a stale count: both installation.md's entry-points table
    ("one of six supported ADR/spec profiles") and stability.md's Provisional row ("the six
    names and their ingest globs") spell the profile count out as a word. Derives the real
    count from `PROFILES` itself -- adding or dropping a profile without updating both docs
    fails here instead of drifting silently, the same class this file's other tests guard."""
    from sidegraph.profiles import PROFILES

    actual = len(PROFILES)
    assert actual > 0
    assert actual < len(_NUMBER_WORDS), (
        f"{actual} profiles exceeds the small number-word vocabulary this test knows; extend "
        "_NUMBER_WORDS"
    )
    word = _NUMBER_WORDS[actual]

    installation_doc = (_ROOT / "docs" / "getting-started" / "installation.md").read_text(
        encoding="utf-8"
    )
    stability_doc = _stability_doc()

    assert f"one of {word} supported ADR/spec profiles" in installation_doc
    assert f"the {word} names and their ingest globs" in stability_doc


# ---------------------------------------------------------------------------
# 9. data-model.md's field tables <-> the pydantic models in schema.py
# ---------------------------------------------------------------------------
#
# The docs sweep of 2026-08-04 found five schema fields (`ratified_by`/`ratified_at` on
# Decision, Fact and Domain; `seed_anchors` on Domain) live in the code and absent from
# the public data-model tables — one of them undocumented since the day it shipped. Every
# other guard in this file watches an ENUMERABLE set (entry points, env vars, finding
# codes); model fields are exactly that shape too, and were the one such set with no
# mirror. Prose still needs human review; a missing ROW no longer does.

_DATA_MODEL_DOC = _ROOT / "docs" / "concepts" / "data-model.md"

# Models documented in prose rather than as a field table. `Provenance` is described
# inline under Decision ("`Provenance`: `source` (an open `str` …)"); `Descriptor` is a
# two-field shape spelled out wherever it appears (`{name, file_path?}`). Both are
# deliberate: a table for either would be longer than the sentence that explains it.
# Anything else missing a table is a gap, not a style choice — hence the assertion below.
_PROSE_DOCUMENTED_MODELS = {"Provenance", "Descriptor"}

_DOC_SECTION_RE = re.compile(r"^## `(\w+)`", re.M)
_DOC_FIELD_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.M)


def _schema_models() -> dict[str, type]:
    from pydantic import BaseModel

    from sidegraph import schema as schema_module

    return {
        name: obj
        for name, obj in vars(schema_module).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
    }


def _documented_model_fields() -> dict[str, set[str]]:
    """``{model name: {field names}}`` from data-model.md's per-model tables."""
    text = _DATA_MODEL_DOC.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for section in re.split(r"^## ", text, flags=re.M)[1:]:
        name_match = re.match(r"`(\w+)`", section)
        if not name_match:
            continue
        fields = set(_DOC_FIELD_ROW_RE.findall(section))
        if fields:
            out[name_match.group(1)] = fields
    return out


def test_data_model_tables_match_schema_fields_bidirectionally():
    """Red against the 2026-08-04 state: Fact and Domain each carried schema fields with no
    documented row (`ratified_by`/`ratified_at`, plus Domain's `seed_anchors`). Derives both
    sides — never hard-codes a field list — so adding a field to schema.py without a doc row,
    or leaving a row behind after removing one, fails here."""
    models = _schema_models()
    documented = _documented_model_fields()

    unknown = set(documented) - set(models)
    assert not unknown, f"data-model.md documents non-existent model(s): {sorted(unknown)}"

    for name, doc_fields in sorted(documented.items()):
        real_fields = set(models[name].model_fields)
        missing = real_fields - doc_fields
        stale = doc_fields - real_fields
        assert not missing, f"{name}: schema fields with no row in data-model.md: {sorted(missing)}"
        assert not stale, f"{name}: data-model.md documents removed field(s): {sorted(stale)}"


def test_every_schema_model_is_documented_somewhere():
    """A new record type must not reach the store with no public description at all. Models
    documented in prose instead of a table are named explicitly, so the exception is a
    decision on the record rather than a silent omission."""
    undocumented = (
        set(_schema_models()) - set(_documented_model_fields()) - _PROSE_DOCUMENTED_MODELS
    )
    assert not undocumented, (
        f"schema model(s) with neither a data-model.md table nor a prose-documented "
        f"exception: {sorted(undocumented)}"
    )


# ---------------------------------------------------------------------------
# 10. no document may describe a policy-exempt read surface
# ---------------------------------------------------------------------------


def test_no_doc_claims_the_raw_listings_are_exempt_from_the_proposal_policy():
    """Red against the 2026-08-04 state a cross-family reviewer found: the code applied the
    surfacing window and regulated mode to `retrieve_decisions`/`list_facts`, while
    `stability.md` and `mcp-tools.md` still told a reader those tools "return everything".
    A security control that the shipped documentation describes as bypassable is not a
    control — the reviewer's blocker, and he was right about the docs even though the code
    was already fixed.

    Derives the surface list from the code: any impl that consults `proposal_surfaces`
    must not be described as returning everything regardless of status.
    """
    server_src = (_ROOT / "src" / "sidegraph" / "server.py").read_text(encoding="utf-8")
    assert "proposal_surfaces" in server_src, (
        "the raw-listing policy application vanished from server.py; if that is deliberate, "
        "this guard and the docs it protects must change together"
    )
    banned = (
        "they return everything with its `status`",
        "return everything regardless",
    )
    for rel in ("docs/reference/stability.md", "docs/reference/mcp-tools.md"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{rel} still describes a policy-exempt read surface"


def test_telemetry_retention_described_consistently_across_docs():
    """Red against the same review's minor finding: configuration.md said pruning continues
    when telemetry is off (correct, post-fix) while store-format.md still said the opt-out
    freezes retained events. "A control I cannot determine from shipped documentation is not
    auditable." Both pages now describe the same behaviour as the code."""
    hooks_src = (_ROOT / "src" / "sidegraph" / "host" / "hooks.py").read_text(encoding="utf-8")
    prune_line = next(ln for ln in hooks_src.splitlines() if "prune_telemetry_events()" in ln)
    assert not prune_line.strip().startswith("if "), (
        "pruning is gated again; update the docs and this guard together"
    )
    # Assert the POSITIVE claim rather than banning a word: "freezes" appears legitimately
    # elsewhere (the drift-marker cache), and a word-ban guard would fail on prose that is
    # correct — the same over-broad-matching mistake this review round found in a shipped
    # script.
    for rel in ("docs/reference/configuration.md", "docs/reference/store-format.md"):
        text = (_ROOT / rel).read_text(encoding="utf-8").lower()
        assert "retention is **not** affected" in text or "unconditionally" in text, (
            f"{rel} no longer states that expiry runs regardless of the telemetry opt-out"
        )
        assert "disables\nrecording and this prune together" not in text, (
            f"{rel} still describes the opt-out as freezing retention"
        )


def test_every_doc_showing_a_mutable_install_ref_warns_about_it():
    """Red against the 2026-08-04 state a cross-family security reviewer found: 35 copyable
    `git+…@main` install commands across the shipped docs, zero of them noting that `@main`
    is mutable — while `operations.md` told CI readers to "pin a SHA or a tag, not a
    branch". Advice that never appears where the command is copied is advice nobody follows.

    Derives the file set from the docs themselves, so a NEW page with an unpinned example
    fails here rather than shipping quietly."""
    roots = [_ROOT / "docs", _ROOT / "README.md"]
    offenders = []
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for f in files:
            text = f.read_text(encoding="utf-8")
            if "@main" not in text:
                continue
            if "mutable ref" not in text:
                offenders.append(str(f.relative_to(_ROOT)))
    assert not offenders, (
        f"these docs show a mutable `@main` install ref with no pinning caveat: {offenders}"
    )


def test_public_docs_never_send_a_reader_to_an_internal_design_note() -> None:
    """Red against the 2026-08-04 snapshot build, which shipped three of these.

    `design/` is not on the release allowlist, so a public page citing a file under it
    points its reader at something that does not exist where they are reading. One of the
    three was a markdown link and the snapshot's link checker caught it; the other two were
    bare `code-span` citations, which no link checker can see — that is why this test is
    mechanical over the text rather than over parsed links.

    The rule is narrow on purpose, in two ways. A `.md` under `design/` is a document you
    are telling someone to go open; a *directory* reference is orientation, not a pointer,
    and stays legal — `docs/pilot-kit/README.md` names the bundle's source-repo path
    precisely to explain that it lives in two places. And fenced blocks are skipped: an
    unscoped version of this test flagged three `sidegraph-import --docs design/0002-some-adr.md`
    console samples, where `design/` is the READER's directory and has nothing to do with ours.

    The whitepaper tree is exempt, and that exemption is the point of the distinction. It is a
    provenance document: the claim ledger's Sources column and the bundle's pre-registration
    notes name the internal artifact behind each number ON PURPOSE, and the paper's
    artifact-availability statement says plainly that some of them are not public. Naming an
    unpublishable source is honest disclosure; telling a reader to go open it is the defect.
    Only the snapshot has these files under docs/, so an unexempted version of this test
    passed here and failed the release build — which is where it was caught.

    The bundle's own README is the ONE exception to that exemption, because it is not
    provenance — it is reproduction instructions. It sent readers to an internal testing
    note for the pinned build the cells ran at, while the bundle already ships those pins
    in its pre-registrations. Found by running the shipped script, not by reading.
    """
    offenders: list[str] = []
    pages = [
        p
        for p in sorted((_ROOT / "docs").rglob("*.md"))
        if "whitepaper" not in p.relative_to(_ROOT).parts or p.parent.name == "artifact-bundle"
    ]
    for path in [*pages, _ROOT / "README.md"]:
        text = path.read_text(encoding="utf-8")
        fenced = False
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            for hit in re.findall(r"design/[A-Za-z0-9._/-]+\.md", line):
                offenders.append(f"{path.relative_to(_ROOT)}:{line_no}: {hit}")
    assert not offenders, (
        "public docs cite internal design notes that never ship; cite the published "
        f"whitepaper section instead, or state the figure without a pointer: {offenders}"
    )


def test_readme_states_the_mechanism_and_promises_no_outcome():
    """A sentence of the form "X, so Y doesn't happen" is a prevention claim, and nothing the
    product records can show it (usage-stats spec D2: the journal separates "memory sent the
    agent there" from "the agent was going anyway" for no line of any report, so no sentence
    about the product may assert an effect either). Guarded on the promise's shapes, not on one
    sentence, so a rewording that keeps the promise is caught too."""
    text = (_ROOT / "README.md").read_text()
    promises = [
        r"\bdoesn'?t become\b",
        r"\bwon'?t become\b",
        r"\bdoes not become\b",
        r"\bnever (?:drifts?|regress\w*)\b",
        r"\bprevents?\b",
        r"\bguarantees?\b",
    ]
    hits = [m.group(0) for pat in promises for m in re.finditer(pat, text, re.IGNORECASE)]
    assert hits == [], f"README promises an outcome instead of describing the mechanism: {hits}"
