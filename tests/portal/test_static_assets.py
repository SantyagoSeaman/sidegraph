"""Static front-end assets obey the page contract, the CSP and the API route table.

See design/superpowers/specs/2026-11-17-portal-design.md (D3, D13). Each front-end part adds
its page's cases in its own test function, so parallel parts never edit the same function.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from sidegraph.portal import contract

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "sidegraph" / "portal" / "static"
VENDOR = STATIC / "vendor"

SHELL_FILES = (
    "index.html",
    "app.js",
    "api.js",
    "live.js",
    "theme.js",
    "ui.js",
    "styles.css",
    "components/header.js",
    "components/footer.js",
    "components/banners.js",
    "components/auth-pages.js",
    "components/status-word.js",
    "components/use-resource.js",
    "vendor/preact.mjs",
    "vendor/hooks.mjs",
    "vendor/htm.mjs",
    "vendor/LICENSES.txt",
)
# Namespace URIs that Preact compares against; they are identifiers, never fetched.
NAMESPACE_URIS = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/1999/xhtml",
)


def _own_files() -> list[Path]:
    """Every static file that is ours: the vendored libraries are excluded."""
    return sorted(
        path for path in STATIC.rglob("*") if path.is_file() and VENDOR not in path.parents
    )


def _own_js() -> list[Path]:
    return [path for path in _own_files() if path.suffix == ".js"]


def _route_patterns() -> list[tuple[str, re.Pattern[str]]]:
    return [
        (route.method, re.compile(re.sub(r"\{[^/}]+\}", "[^/]+", route.path)))
        for route in contract.ROUTES
        if route.path.startswith("/api/")
    ]


def test_shell_files_present() -> None:
    missing = [name for name in SHELL_FILES if not (STATIC / name).is_file()]
    assert not missing, missing


def test_shell_index_html_is_csp_safe() -> None:
    class Scan(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.html_class = ""
            self.scripts: list[dict[str, str | None]] = []
            self.handlers: list[str] = []
            self.refs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            found = dict(attrs)
            if tag == "html":
                self.html_class = found.get("class") or ""
            if tag == "script":
                self.scripts.append(found)
            self.handlers += [name for name in found if name.startswith("on")]
            self.refs += [
                value for name, value in found.items() if name in ("src", "href") and value
            ]

    scan = Scan()
    scan.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert scan.html_class == contract.THEME_MARKER
    assert scan.scripts, "the shell loads its module"
    assert all(script.get("src") and script.get("type") == "module" for script in scan.scripts)
    assert not scan.handlers
    assert all(ref.startswith("/static/") for ref in scan.refs), scan.refs


def test_shell_api_paths_match_the_contract() -> None:
    routes = _route_patterns()
    call = re.compile(r"""\b(get|post|useResource)\(\s*(['"`])(/api/(?:(?!\2).)*)\2""")
    literal = re.compile(r"""(['"`])(/api/(?:(?!\1).)*)\1""")
    seen: set[str] = set()
    for path in _own_js():
        source = path.read_text(encoding="utf-8")
        for match in literal.finditer(source):
            api_path = re.sub(r"\$\{[^}]*\}", "x", match.group(2)).split("?")[0]
            assert any(p.fullmatch(api_path) for _, p in routes), (path.name, api_path)
            seen.add(api_path)
        for match in call.finditer(source):
            api_path = re.sub(r"\$\{[^}]*\}", "x", match.group(3)).split("?")[0]
            method = "POST" if match.group(1) == "post" else "GET"
            assert any(m == method and p.fullmatch(api_path) for m, p in routes), (
                path.name,
                method,
                api_path,
            )
    assert {"/api/session", "/api/settings", "/api/live-ticket", "/api/queue"} <= seen
    streams = re.findall(r"new EventSource\(`(/[^?`]*)", (STATIC / "live.js").read_text("utf-8"))
    assert streams
    assert all(("GET", path) in {(r.method, r.path) for r in contract.ROUTES} for path in streams)


def test_shell_markup_is_never_html_strings() -> None:
    for path in _own_files():
        text = path.read_text(encoding="utf-8")
        assert "innerHTML" not in text, path.name
        assert "dangerouslySetInnerHTML" not in text, path.name
        assert "outerHTML" not in text, path.name
        assert "document.write" not in text, path.name
        assert "eval(" not in text, path.name


def test_shell_has_no_external_url() -> None:
    url = re.compile(r"(?:https?|wss?|ftp)://|@import|(?<![:\w])//[a-z0-9.-]+\.[a-z]{2,}/")
    for path in _own_files():
        assert not url.search(path.read_text(encoding="utf-8")), path.name
    for path in sorted(VENDOR.rglob("*")):
        name = path.relative_to(VENDOR).as_posix()
        if not path.is_file() or name == "LICENSES.txt":
            continue
        text = path.read_text(encoding="utf-8")
        if name == "preact.mjs":
            for uri in NAMESPACE_URIS:
                text = text.replace(f'"{uri}"', '""')
        assert not url.search(text), path.name


def test_shell_carries_no_mock_only_code_or_linear_ids() -> None:
    for path in _own_files():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"__mock|mock-token|portal_mock", text), path.name
        assert not re.search(r"\b(?:SG|SAN)-\d+\b", text), path.name


def test_shell_navigation_pages_exist() -> None:
    """Every page the shell registers is a module; sub-pages a page loads itself are its own."""
    registered = set(re.findall(r"\./pages/([\w-]+)\.js", (STATIC / "app.js").read_text("utf-8")))
    on_disk = {path.stem for path in (STATIC / "pages").glob("*.js")}
    assert registered <= on_disk
    assert {"graph", "records", "queue", "agent-view"} <= registered


def test_shell_vendor_versions_and_licenses_are_recorded() -> None:
    licenses = (VENDOR / "LICENSES.txt").read_text(encoding="utf-8")
    assert "preact 11.1.1" in licenses
    assert "htm 3.1.1" in licenses
    assert "MIT License" in licenses
    assert "Apache License" in licenses
    # The vendored hooks bundle reaches Preact through a relative path, not a bare name.
    hooks = (VENDOR / "hooks.mjs").read_text(encoding="utf-8")
    assert 'from"./preact.mjs"' in hooks


def test_shell_vendor_directory_is_excluded_from_formatters() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    exclude = re.search(r"^exclude: '(.*)'$", config, re.MULTILINE)
    assert exclude
    assert re.search(exclude.group(1), "src/sidegraph/portal/static/vendor/preact.mjs")
    assert not re.search(exclude.group(1), "src/sidegraph/portal/static/app.js")


def _graph_sources() -> dict[str, str]:
    names = ("pages/graph.js", "components/search-box.js", "components/header.js")
    return {name: (STATIC / name).read_text(encoding="utf-8") for name in names}


def test_graph_page_reads_the_graph_search_and_card_routes() -> None:
    """The graph page and the header search call only contract routes, with contract inputs."""
    sources = _graph_sources()
    graph, search, header = (
        sources["pages/graph.js"],
        sources["components/search-box.js"],
        sources["components/header.js"],
    )
    assert "not built yet" not in graph
    assert re.search(r"useResource\(\s*['\"]/api/graph['\"]", graph)
    assert re.search(r"useResource\(\s*`/api/records/\$\{[^}]+\}`", graph)
    assert re.search(r"get\(\s*['\"]/api/search['\"]", search)
    assert re.search(r"useResource\(\s*['\"]/api/graph['\"],\s*\{\s*params:\s*\{\s*history", graph)
    assert re.search(r"get\(\s*['\"]/api/search['\"],\s*\{\s*q,\s*limit", search)
    # The dropdown belongs to the graph page (D4); other pages have their own search.
    assert re.search(r"route\.page\s*===\s*['\"]graph['\"]\s*&&.*SearchBox", header)


def test_graph_page_loads_vis_network_from_the_contract_path_only() -> None:
    """vis-network is served from VIS_NETWORK_PATH; the page neither vendors nor inlines it."""
    graph = (STATIC / "pages" / "graph.js").read_text(encoding="utf-8")
    assert f'"{contract.VIS_NETWORK_PATH}"' in graph
    assert not list(VENDOR.glob("vis-network*"))
    assert 'createElement("script")' in graph


def test_graph_page_dropdown_keyboard_and_labels() -> None:
    """The search dropdown is a labelled combobox driven by the arrow keys, Enter and Escape."""
    search = (STATIC / "components" / "search-box.js").read_text(encoding="utf-8")
    for needle in (
        'role="combobox"',
        'role="listbox"',
        'role="option"',
        "ArrowDown",
        "ArrowUp",
        "Enter",
        "Escape",
        "aria-activedescendant",
        "<mark",
    ):
        assert needle in search, needle
    assert "open_id" in search


def test_graph_page_keyboard_paths() -> None:
    """A canvas has no keyboard path, so a labelled select picks a node; the list closes on blur."""
    graph = (STATIC / "pages" / "graph.js").read_text(encoding="utf-8")
    search = (STATIC / "components" / "search-box.js").read_text(encoding="utf-8")
    assert re.search(r'class="graph-inspect[^"]*">\s*Inspect\s*<select', graph)
    assert re.search(r"graph-inspect.*?onChange=\$\{[^}]*setSelectedId", graph, re.DOTALL)
    assert re.search(r"aria-describedby=\$\{tip \? tipId", graph)
    assert "${!certified &&" in graph
    # The wiring, not just the names: handlers attached to the box, options out of the tab order.
    assert re.search(r'class="search"[^>]*onFocusOut=\$\{onFocusOut\}', search)
    assert re.search(r'class="search"[^>]*onKeyDown=\$\{onBoxKeyDown\}', search)
    assert re.search(r'role="option"[^>]*tabindex="-1"', search)
    assert re.search(r"limit:\s*COUNT_CAP\s*\+\s*1", search)
    assert re.search(
        r'class="search-list"[^>]*onMouseDown=\$\{\(event\) => event\.preventDefault\(\)\}', search
    )
    assert re.search(r'querySelector\("input"\)\.focus\(\);\s*\}\s*dismiss\(\)', search)
    assert re.search(
        r"if \(pending\.current\) stale\.current = true;\s*else run\(query, false\)", search
    )
    assert "getElementById(`search-option-${active}`)?.scrollIntoView" in search
    assert "hit.id === activeId.current" in search
    assert "const lower = text.toLowerCase();" in search
    assert re.search(r'\.vis-network"\)\?\.setAttribute\("tabindex", "-1"\)', graph)
    # Only options sit inside the listbox; hints and the "All in Records" link do not.
    listbox = search[search.index('role="listbox"') :]
    assert listbox.index("search-foot") > listbox.index("</div>`")


def test_records_page_and_card_use_the_contract() -> None:
    """F3: the records table, the card and the code snippets call only contract routes."""
    page = (STATIC / "pages" / "records.js").read_text(encoding="utf-8")
    card = (STATIC / "components" / "record-card.js").read_text(encoding="utf-8")
    anchors = (STATIC / "components" / "anchor-snippet.js").read_text(encoding="utf-8")
    assert "not built yet" not in page
    assert '"/api/records"' in page
    assert re.search(r"`/api/records/\$\{[^}]+\}`", card)
    assert re.search(r"`/api/records/\$\{[^}]+\}/anchors/\$\{[^}]+\}/snippet`", anchors)
    # The editor link is built from the anchor's absolute path and the session's template only.
    assert "editor_path" in anchors and "editor_url" in anchors
    assert "{path}" in anchors and "{line}" in anchors
    # The page sends the filters the contract names to the records route, and no others.
    declared = re.search(r"const FILTERS = \[([^\]]*)\]", page)
    assert declared
    assert re.findall(r'"(\w+)"', declared.group(1)) == [
        "status",
        "kind",
        "domain",
        "layer",
        "anchors",
    ]
    assert 'query.get("q")' in page and 'query.get("history")' in page


def test_records_search_opens_a_unique_id_prefix() -> None:
    """The Records search asks /api/search like the header dropdown; open_id opens the card."""
    page, search = _static("pages/records.js", "components/search-box.js")
    # One navigation, shared with the dropdown: the page imports it instead of copying it.
    assert re.search(
        r'import \{[^}]*\bopen as openRecord\b[^}]*\} from "../components/search-box\.js"', page
    )
    assert re.search(r"export function open\(id\)", search)
    # The server decides what a unique prefix is (D4); the page only follows `open_id`.
    assert re.search(r'get\(\s*"/api/search"', page)
    # Only an answer about the text still in the box moves the page, and Back returns to that text.
    assert re.search(
        r"//[^\n]*\s*if \(!found\.open_id \|\| typed\.current !== q"
        r' \|\| location\.hash\.split\("\?"\)\[0\] !== "#/records"\) return;\s*//[^\n]*\s*'
        r"writeFilters\(\{ \.\.\.readFilters\(\), q \}\);\s*openRecord\(found\.open_id\);",
        page,
    )
    # The lookup is cancelled when the page goes, and short text is not looked up at all.
    assert re.search(
        r"clearTimeout\(lookupTimer\.current\);\s*lookup\.current\?\.abort\(\);\s*\},\s*\[\],", page
    )
    assert re.search(r"get\(\s*\"/api/search\",[^;]*signal: abort\.signal", page)
    assert re.search(r"if \(q\.trim\(\)\.length < 8\) return;", page)
    # Other text keeps filtering: the table filter is applied without waiting for the lookup.
    assert re.search(r"setTimeout\(\(\) => change\(\{ q: text \}\), 250\)", page)
    # Only typing looks for a record, after its own pause: a filter change cannot cancel it,
    # and a return to the page (text restored from the address) does not start it.
    assert re.search(r"onInput=\$\{\(event\) => type\(event\.target\.value\)\}", page)
    assert re.search(r"lookupTimer\.current = setTimeout\(\(\) => findRecord\(value\), 250\)", page)
    assert page.count("findRecord(") == 1  # the one call, from typing


def test_queue_page_uses_the_contract() -> None:
    """F4: the queue page reads the queue, previews and applies marked verdicts, and no more."""
    page = (STATIC / "pages" / "queue.js").read_text(encoding="utf-8")
    assert "not built yet" not in page
    item = (STATIC / "components" / "queue-item.js").read_text(encoding="utf-8")
    assert re.search(r"""useResource\(\s*"/api/queue"\s*,""", page)
    # Preview and Apply take the marked ids as {accept, reject}; Apply is a POST of its own.
    for route in ("preview", "apply"):
        assert re.search(
            rf"""post\(\s*"/api/queue/{route}"\s*,\s*\{{\s*accept,\s*reject\s*\}}""", page
        ), route
    # "Edit and accept" is the edit page of the draft, not a mark and not a request of this page.
    assert re.search(r"#/edit/\$\{[^}]+\}\?mode=accept", item)
    # Nested evidence follows its decision's verdict: its component renders no control (T-R5).
    evidence = re.search(r"function Evidence\(.*?\n\}\n", item, re.DOTALL)
    assert evidence, "the nested evidence is its own component"
    assert not re.search(r"<(button|input|select|textarea|a)\b|<\$\{", evidence.group(0))
    assert "onClick" not in evidence.group(0)


def _static(*names: str) -> list[str]:
    return [(STATIC / name).read_text(encoding="utf-8") for name in names]


def test_edit_view_is_registered_and_reachable() -> None:
    """F5: one hidden page entry serves #/edit/<id> and its accept mode; the card links to it."""
    app, card = _static("app.js", "components/record-card.js")
    assert re.search(r'key: "edit",[^\n]*hidden: true[^\n]*\./pages/edit\.js', app)
    assert re.search(r"pages=\$\{PAGES\.filter\(\(page\) => !page\.hidden\)\}", app)
    # The edit view has no nav entry of its own; the Records entry stays current.
    assert re.search(r'route\.page === "edit" \? \{ \.\.\.route, page: "records" \}', app)
    assert re.search(r"`#/edit/\$\{encodeURIComponent\(id\)\}`", card)
    assert "Edit as new version" in card


def test_edit_page_calls_only_contract_routes_with_contract_bodies() -> None:
    """F5: reads the card, writes the three operation routes, sends the contract's body keys."""
    (edit,) = _static("pages/edit.js")
    assert re.search(r"useResource\(\s*`/api/records/\$\{[^}]+\}`", edit)
    for route in ("edit", "edit-and-accept", "save-as-new"):
        assert re.search(rf"/api/records/\$\{{[^}}]+\}}/{route}`", edit), route
    for key in ("op_key", "fields", "carry_facts", "keep_facts"):
        assert re.search(rf"\b{key}\b", edit), key
    # Every decision and fact field the contract lists is editable.
    for field in ("title", "kind", "layer", "context", "choice", "rejected", "consequences"):
        assert re.search(rf'"{field}"', edit), field
    assert '"statement"' in edit and '"source"' in edit


def test_edit_page_operation_key_and_double_submit() -> None:
    """F5: the key is minted once per request and reused on retry; a second click sends nothing."""
    (edit,) = _static("pages/edit.js")
    assert edit.count("crypto.randomUUID()") == 1
    # The key is kept with the request it belongs to; the same request reuses it.
    assert re.search(r"attempt\.current\?\.body\s*===\s*body", edit)
    # The guard is a ref set before the request, so two clicks in one tick cannot both pass.
    assert re.search(r"if \(sending\.current\) return;\s*sending\.current = true;", edit)
    assert re.search(r"finally \{\s*sending\.current = false;", edit)


def test_edit_page_unknown_outcome_retries_the_same_request() -> None:
    """F5: a lost answer locks the form and offers Retry with the kept request, never a new key."""
    edit, result = _static("pages/edit.js", "components/operation-result.js")
    assert re.search(r"retry: true,\s*\};", edit)
    # A busy answer to a retry does not settle the first try; only a result or another answer does.
    assert re.search(
        r'\["network", "server"\]\.includes\(error\.code\) \|\|'
        r" \(unresolved && INCONCLUSIVE\.includes\(error\.code\)"
        r' && error\.details\?\.reason !== "no_change"\)',
        edit,
    )
    assert re.search(
        r"const retry = \(\) => send\(attempt\.current\.route, attempt\.current\.payload\)", edit
    )
    # A failed retry keeps the parts already written on screen.
    assert "result: previous?.result" in edit and "The last try failed" in result
    # The text and the selection cannot change while a request is out or unanswered.
    assert "const locked = sendingNow || Boolean(failure?.retry);" in edit
    assert edit.count("locked=${locked}") == 2 and "disabled: locked" in edit


def test_edit_page_no_field_change_save_rules() -> None:
    """F5: a save with no field change is only for an accepted record that drops evidence."""
    (edit,) = _static("pages/edit.js")
    assert 'const allowsNoFieldChange = mode === "version" && notKept.length > 0;' in edit
    assert "changed.length === 0 && !allowsNoFieldChange" in edit
    # Save as new is checked like Save: no empty required field, no unchanged draft.
    assert re.search(r"const saveAsNew = \(\) => \{\s*if \(problem\) return;", edit)


def test_edit_page_nothing_changed_hint_and_disabled_save() -> None:
    """A new version with no field or evidence change is not saved; the page says what to do."""
    (edit,) = _static("pages/edit.js")
    hint = "Nothing changed. To confirm that this record still holds, record a drift check instead."
    assert f'const NO_CHANGE = "{hint}";' in edit
    # The hint sits next to the Save button, which is disabled while the problem stands.
    assert re.search(r'"Change at least one field to save\. [^"]*"\s*:\s*NO_CHANGE', edit)
    assert re.search(
        r"aria-disabled=\$\{Boolean\(problem\) \|\| sendingNow\}\s+onClick=\$\{save\}", edit
    )
    assert re.search(
        r'problem && !failure\?\.retry && html`<span class="muted" role="status">', edit
    )
    # A server `no_change` refusal has no wording here: the generic refusal shows its message.
    api = (STATIC / "api.js").read_text(encoding="utf-8")
    assert re.search(
        r'code === "refused"\) return REFUSED_REASONS\[details\.reason\] \?\? error\.message', api
    )
    assert "no_change" not in api
    # A `no_change` refusal answers a retry too: nothing was written, so it is not "unresolved".
    assert 'error.details?.reason !== "no_change"' in edit


def test_edit_page_nested_evidence_is_not_edited_alone() -> None:
    """F5: a proposed fact is edited and accepted only when the queue lists it by itself."""
    (edit,) = _static("pages/edit.js")
    assert re.search(r'useResource\(\s*"/api/queue"', edit)
    assert "queue.facts.some((item) => item.record.id === id)" in edit


def test_edit_page_wording_and_recoveries() -> None:
    """F5: the banner sentence, the drift sentence, the refusals with links, the recoveries."""
    edit, result = _static("pages/edit.js", "components/operation-result.js")
    assert "Saving does not overwrite this record. It creates a new version." in edit
    assert "saving records that this text holds for the current code" in edit.lower()
    # An untouched drifted record is not invited to save: the notice points to the drift check.
    assert re.search(
        r'nothingChanged && mode === "version"\s*\? "This record is marked for content review\. '
        r'To confirm that it still holds, record a drift check\."',
        edit,
    )
    for code in ("not_current", "pending_successor", "pending_evidence", "evidence_conflict"):
        assert code in edit, code
    assert "Compare" in edit and "Start again" in edit
    assert "Save your text as a new record" in edit
    assert "offer_save_as_new" in edit
    assert "done now" in result and "failed" in result and "Retry" in result
    # Records written in one operation share their first eight characters: ids are shown whole.
    assert "part.id.slice" not in result


def test_agent_view_page_calls_the_three_agent_routes() -> None:
    """F6: the catalog is read, a run is a POST with exactly the contract's body keys."""
    (page,) = _static("pages/agent-view.js")
    assert "not built yet" not in page
    assert re.search(r"useResource\(\s*['\"]/api/agent-view/catalog['\"]", page)
    assert re.search(r'post\(\s*"/api/agent-view/tool",\s*request\.body\s*\)', page)
    assert re.search(r'post\(\s*"/api/agent-view/hook",\s*request\.body\s*\)', page)
    # Each route is tied to its own body: {name, args, host_family?} for a tool,
    # {event, params, host_family?} for a hook, chosen by the same flag.
    assert re.search(
        r'route: tool \? "tool" : "hook",\s*body: tool'
        r"\s*\? \{ name, args: read\.args, host_family: host \}"
        r"\s*: \{ event: name, params: read\.args, host_family: host \},",
        page,
    )
    assert re.search(
        r'request\.route === "tool"\s*\? await post\("/api/agent-view/tool", request\.body\)'
        r'\s*: await post\("/api/agent-view/hook", request\.body\)',
        page,
    )
    # The page names no surface itself: tools, events and parameters all come from the catalog.
    assert not re.search(r"['\"](retrieve_decisions|get_task_context|query_decisions)['\"]", page)


def test_agent_view_page_renders_every_emulation_field() -> None:
    """F6: size, budget, cut, assumptions, firing conditions, the notes and the id links."""
    (page,) = _static("pages/agent-view.js")
    for expression in (
        "count(chars)",
        "budget != null &&",
        "${cut ?",
        "cut && html",
        "assumptions.map(",
        "<h3>Emitted only when</h3>",
        "not_emitted_reason ?",
        "ids=${ids_in_text}",
        "moved > 0 &&",
        "prepared.skipped_reason &&",
        "surface.wired_on.includes(host)",
        "Not wired on ${host}",
        "catalog.host_families.map(",
    ):
        assert expression in page, expression
    assert re.search(r"firing_conditions\.length > 0 &&\s*html`", page)
    assert "Exact text" in page and "Readable" in page
    assert "Not counted in sidegraph-stats" in page
    assert "ran the same sync the agent's next call would run" in page
    assert "No sync was run" in page
    assert re.search(r"`#/records/\$\{encodeURIComponent\([^)]+\)\}`", page)
    # The default host is the session's, then Claude Code (D10), never the catalog's order alone.
    assert re.search(
        r'\[session\.host_family, "claude"\]\.find\('
        r"\(family\) => catalog\.host_families\.includes\(family\)\)",
        page,
    )


def test_agent_view_page_run_is_guarded_and_keeps_its_last_result() -> None:
    """F6: one run at a time; a failed run keeps the last result and retries its own request."""
    (page,) = _static("pages/agent-view.js")
    assert re.search(r"if \(running\.current\) return;\s*running\.current = true;", page)
    assert re.search(r"finally \{\s*running\.current = false;", page)
    assert re.search(r"catch \(error\) \{\s*setFailure\(\{ error, request \}\);", page)
    assert re.search(
        r"setOutcome\(\{ label: request\.label, response \}\);\s*setFailure\(null\);", page
    )
    assert 'Failed: <span class="mono av-call">' in page
    # Each tab builds its own request, and an edit clears the form's last problem.
    assert 'const tool = kind === "tools";' in page
    assert re.search(
        r"setDrafts\(\{ \.\.\.drafts, \[draftKey\]: next \}\);\s*setProblem\(null\);", page
    )
    assert "setOutcome(null)" not in page
    assert "onRetry=${() => send(failure.request)}" in page
    # Opening the page, before any run, must not scroll toward the empty result.
    assert re.search(
        r"if \(result\.current && \(outcome \|\| failure\)\)"
        r" result\.current\.scrollIntoView\([^)]*\);"
        r"\s*\}, \[outcome, failure\]\);",
        page,
    )
    assert 'role="tablist"' in page and 'role="tab"' in page and "aria-selected" in page
    assert "ArrowRight" in page and "ArrowLeft" in page
    # Emulating "already shown in this session" is out of scope (non-goal): no such control.
    assert "already shown" not in page.lower()


def test_agent_view_page_form_sends_only_what_the_catalog_schema_allows() -> None:
    """F6: integers are whole, required text is not blank, JSON has the schema's container."""
    (page,) = _static("pages/agent-view.js")
    assert 'const whole = jsonVariant(property).type === "integer";' in page
    # 5 and 5.0 are whole numbers; a fraction or a value Number() would round is not.
    assert re.search(
        r"whole && !\(/\^\[\+-\]\?\\d\+\(\\\.0\+\)\?\$/\.test\(value\.trim\(\)\)"
        r" && Number\.isSafeInteger\(number\)\)",
        page,
    )
    assert "(schema.required ?? []).includes(name) && !(name in args)" in page
    assert re.search(r'else if \(value\.trim\(\) !== ""\)', page)
    # JSON values are checked against the schema down to a list's items; null leaves a field out.
    assert "value.every((item) => matches(item, schema.items))" in page
    assert re.search(r"if \(!matches\(parsed, property\)\) return \{ problem", page)
    assert "if (parsed !== null) args[name] = parsed;" in page
    # Lines go as typed (a leading space makes another path); only blank lines are dropped.
    assert 'value.split("\\n").filter((line) => line.trim() !== "")' in page
    assert ".map((line) => line.trim())" not in page
    # Enter in a one-line field leaves a newline at an end of the text: the ends are cut.
    assert 'value.replace(/^\\n+|\\n+$/g, "")' in page
    # Free text may hold several lines (an Agent prompt), so it is a textarea, not a one-line input.
    assert 'type="text"' not in page
    assert re.search(r'<textarea \.\.\.\$\{props\} class=\$\{`av-input\$\{kind === "text"', page)


def test_graph_page_lays_out_once_then_stands_still() -> None:
    """The layout is computed before the graph is shown, then physics stops."""
    graph = (STATIC / "pages" / "graph.js").read_text(encoding="utf-8")
    assert "stabilization: false" not in graph
    assert re.search(r"stabilization:\s*\{[^}]*iterations:\s*LAYOUT_ITERATIONS", graph)
    # Physics stops in the completion handler, with the notice: a drag moves one node only.
    assert re.search(
        r'network\.on\("stabilizationIterationsDone", \(\) => \{\s*'
        r"network\.setOptions\(\{\s*physics:\s*false\s*\}\);\s*setLaying\(false\);",
        graph,
    )
    # The library stabilizes by itself only the data a network is built with.
    assert "network.stabilize(placed.length ? LIVE_ITERATIONS : LAYOUT_ITERATIONS)" in graph
    # The view is framed once, and after a filter change; a live data change keeps the view.
    assert re.search(r"stabilization:\s*\{[^}]*fit:\s*false", graph)
    assert "if (frameNext.current) network.fit();" in graph
    assert "const refiltered = layoutKey !== laidKey.current;" in graph
    assert re.search(r"if \(relayout\) \{\s*if \(refiltered\) frameNext\.current = true;", graph)
    # A filter change (other node or edge ids, in any order) lays out again; the same data not.
    assert re.search(r"nodes\.map\(\(node\) => node\.id\)\.sort\(\)", graph)
    assert re.search(r"if \(relayout\) \{[^}]*setLaying\(true\);", graph)
    # A live change pins the nodes already placed for its layout and frees them afterwards.
    assert "const placed = relayout && !refiltered ? nodeSet.getIds() : [];" in graph
    assert "({ id, fixed: true })" in graph and "({ id: node.id, fixed: false })" in graph
    assert "Laying out the graph…" in graph
    # Room for the labelled boxes: full overlap avoidance and a long spring.
    assert re.search(r"avoidOverlap:\s*1[,\s]", graph)


def test_stopped_portal_shows_one_banner() -> None:
    """A portal that stops while the tab is hidden gets one banner, not three notices."""
    live, banners, use_resource = (
        (STATIC / name).read_text(encoding="utf-8")
        for name in ("live.js", "components/banners.js", "components/use-resource.js")
    )
    # Consecutive failed reconnects mean the process is gone; an open stream clears the count.
    assert re.search(r"failures >= STOP_AFTER_FAILURES \? \"stopped\" : \"reconnecting\"", live)
    assert re.search(r"stream\.onopen = \(\) => \{\s*failures = 0;", live)
    text = (
        "The portal has stopped. Open it again from your agent (open_portal) "
        "or run sidegraph-portal."
    )
    assert text in banners
    # A closing event and failed reconnects end in the same state; only a refused token closes.
    assert re.search(r'addEventListener\("closing", \(\) => stop\("stopped"\)\)', live)
    # While it is up, it is the only shell banner, and request errors do not repeat it.
    assert re.search(
        r'liveState === "stopped"\)\s*\{[^}]*return html`<div class="banners">', banners
    )
    assert 'const gone = ["reconnecting", "stopped"].includes(useLiveState());' in banners
    assert re.search(r'if \(gone && \(error\?\.code === "network"', banners)
    assert "useLiveState" in banners and "export function useLiveState" in use_resource
    # The state is read again after subscribing: it can change before the effect runs.
    assert re.search(r"onLive\([^;]*\);\s*//[^\n]*\n\s*setState\(liveState\(\)\);", use_resource)


def test_error_notice_is_silent_for_a_failed_page_load_while_the_portal_is_gone() -> None:
    """A lazy page module that fails to load is a TypeError, not an ApiError: it is no news."""
    banners = (STATIC / "components" / "banners.js").read_text(encoding="utf-8")
    assert re.search(r'import \{[^}]*\bApiError\b[^}]*\} from "../api.js";', banners)
    assert "!(error instanceof ApiError)" in banners
    assert re.search(
        r"if \(gone && \([^)]*!\(error instanceof ApiError\)\)\) return null;", banners
    )


def test_graph_page_lays_out_a_live_change_with_a_short_pass() -> None:
    """Nodes already placed stay pinned; only the new ones get a short pass."""
    graph = (STATIC / "pages" / "graph.js").read_text(encoding="utf-8")
    assert re.search(r"const LIVE_ITERATIONS = \d+;", graph)
    assert "network.stabilize(placed.length ? LIVE_ITERATIONS : LAYOUT_ITERATIONS)" in graph


def test_edit_and_accept_keeps_the_evidence_the_card_names() -> None:
    """The Keep list is the card's `keepable_evidence`; the page does not guess exclusivity."""
    (edit,) = _static("pages/edit.js")
    assert "card.keepable_evidence" in edit
    assert "supports.every" not in edit


def test_anchor_to_the_end_of_its_file_offers_more_lines() -> None:
    """An anchor with no `end` has no known span: a full first page offers "Show more lines"."""
    (anchors,) = _static("components/anchor-snippet.js")
    assert "const FIRST_PAGE = 12;" in anchors
    assert "const more = unbounded ? lines.length >= FIRST_PAGE : span > FIRST_PAGE;" in anchors
    assert "const unbounded = anchor.end == null;" in anchors
    assert "Show more lines" in anchors


def test_records_history_filter_has_no_dropped_status() -> None:
    """Only domains are dropped, and domains are not rows of the records table."""
    (page,) = _static("pages/records.js")
    declared = re.search(r"const HISTORY_STATUSES = \[([^\]]*)\]", page)
    assert declared
    assert re.findall(r'"(\w+)"', declared.group(1)) == ["superseded", "rejected", "deprecated"]


def test_graph_search_follows_the_history_setting() -> None:
    """The header search on the graph page asks with the page's "Superseded / history" choice."""
    graph, search = _static("pages/graph.js", "components/search-box.js")
    assert re.search(r"import \{[^}]*\bsetSearchHistory\b[^}]*\} from", graph)
    assert re.search(r"setSearchHistory\(history\)", graph)
    # The setting is read when the request goes out, so an earlier re-ask cannot use an old one.
    assert "history: historyNow.current" in search and "historyNow.current = history;" in search
    # A change of the setting refreshes an open search like a live change does.
    assert "useEffect(refresh, [history]);" in search


def test_record_card_shows_the_ratification_time_without_a_git_name() -> None:
    """`ratified_at` with no `ratified_by` reads "Accepted <when>"."""
    (card,) = _static("components/record-card.js")
    assert "record.ratified_at" in card
    assert re.search(r": record\.ratified_at\s*\? when\(record\.ratified_at\)", card)


def test_search_link_and_old_bookmarks_follow_the_history_setting() -> None:
    """ "All in Records" keeps the history tick; a status the table does not offer is dropped."""
    search, page, edit = _static("components/search-box.js", "pages/records.js", "pages/edit.js")
    assert '${history ? "&history=true" : ""}' in search
    assert re.search(
        r"\(filters\.history \? \[\.\.\.OPEN_STATUSES, \.\.\.HISTORY_STATUSES\] : OPEN_STATUSES\)"
        r"\.includes\(filters\.status\)",
        page,
    )
    # A fact that is not proposed is not said to support another proposal.
    assert 'fact.status === "proposed" ? "Also supports another proposal' in edit
