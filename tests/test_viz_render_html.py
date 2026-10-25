import re
from importlib import resources

from sidegraph.viz.model import VizGraph, VizNode, VizStats
from sidegraph.viz.render import to_html


def _graph():
    return VizGraph(
        nodes=[
            VizNode(
                id="d1",
                type="decision",
                label="ZZ_UNIQUE_LABEL",
                kind="adr",
                status="accepted",
                detail={},
            )
        ],
        edges=[],
        stats=VizStats(decisions=1),
    )


def test_to_html_is_self_contained():
    html = to_html(_graph())
    # no external script/style resource loads
    assert re.search(r"<script[^>]+\bsrc\s*=", html) is None
    assert re.search(r'<link[^>]+href\s*=\s*["\']https?:', html) is None
    assert "unpkg.com" not in html


def test_to_html_embeds_library_and_data():
    html = to_html(_graph())
    # the vendored library is inlined whole (the template's own script mentions vis.Network
    # and DataSet, so those names alone cannot tell the library from a stub)
    library = resources.files("sidegraph.viz").joinpath("assets", "vis-network.min.js")
    assert library.read_text(encoding="utf-8") in html
    assert "ZZ_UNIQUE_LABEL" in html  # the graph data is inlined
    assert html.lstrip().lower().startswith("<!doctype html")
    # both substitution tokens were actually replaced (not left as literal placeholders) --
    # the assertions above are satisfied by the template's own script even if substitution
    # failed, since the template itself references vis.Network/DataSet.
    assert "__SIDEGRAPH_" not in html


def test_to_html_escapes_script_close_in_data():
    g = VizGraph(
        nodes=[
            VizNode(
                id="x",
                type="decision",
                label="a</script>b",
                kind="adr",
                status="accepted",
                detail={},
            )
        ],
        edges=[],
        stats=VizStats(decisions=1),
    )
    html = to_html(g)
    # the raw closing tag must not appear inside the injected data (would break out of <script>)
    assert "a</script>b" not in html
    assert "a<\\/script>b" in html
