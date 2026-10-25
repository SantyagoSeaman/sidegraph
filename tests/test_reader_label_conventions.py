"""The seam names Graphify's label conventions the snapshot matches by (D5.1)."""

from __future__ import annotations

from sidegraph.engine.reader import NodeRef, is_file_node


def _n(name, path, file_type="code"):
    return NodeRef(
        node_id="x", name=name, norm_name=name.lower(), file_type=file_type, file_path=path
    )


def test_a_file_node_is_a_code_or_document_node_named_like_its_file():
    """Red against nothing existing (ImportError)."""
    assert is_file_node(_n("api.py", "src/pay/api.py"))
    assert is_file_node(_n("pay.md", "docs/pay.md", "document"))
    assert not is_file_node(_n("api.py", "src/pay/api.py", "rationale"))
    assert not is_file_node(_n("Invoice", "src/pay/model.py"))
    assert not is_file_node(_n("api.py", None))
