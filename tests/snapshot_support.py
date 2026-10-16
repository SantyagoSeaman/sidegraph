"""Shared fixtures for the domain-membership and domain-snapshot tests.

``legacy_graph()`` is 0.9.6-shaped: four communities, no stamp, no mention edge.
``mention_graph()`` is the same tree after a 0.9.90 rebuild: a heading links to the code it
names, and Leiden merged the payment code, its document, an unrelated module and a new symbol
of core's file into community 1; a new file joined core's community 2. Graphs are written with
``tests.graph_fixtures.write_graph(tmp_path, name, data)``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import fastmcp

import sidegraph.server as server_module
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor, Domain, DomainStatus, Provenance
from sidegraph.store import MEMBERSHIP_STAMP_KEY, Store
from sidegraph.sync import membership_stamp

PAY = (
    ("model_py", "model.py", "code", "src/pay/model.py"),
    ("model_invoice", "Invoice", "code", "src/pay/model.py"),
    ("api_py", "api.py", "code", "src/pay/api.py"),
    ("api_charge", ".charge()", "code", "src/pay/api.py"),
    ("api_rationale_3", "Charges are idempotent.", "rationale", "src/pay/api.py"),
)
STUB = ("stub_basemodel", "BaseModel", "code", "")
CORE = (
    ("util_py", "util.py", "code", "src/core/util.py"),
    ("util_helper", "helper()", "code", "src/core/util.py"),
)
DOCS = (
    ("pay_md", "pay.md", "document", "docs/pay.md"),
    ("pay_md_charging", "Charging", "document", "docs/pay.md"),
)
LONELY = (
    ("lonely_py", "lonely.py", "code", "src/misc/lonely.py"),
    ("lonely_fn", "lonely()", "code", "src/misc/lonely.py"),
)
EXTRA = ("util_extra", ".extra()", "code", "src/core/util.py")
NEW = (
    ("new_py", "new.py", "code", "src/core/new.py"),
    ("new_fresh", "fresh()", "code", "src/core/new.py"),
)
HELPERS = ("misc_helpers", "helpers.py", "code", "src/misc/helpers.py")
TOOL = ("pay_tool", "tool.py", "code", "src/pay/tool.py")

PAYMENTS_IDS = {n[0] for n in PAY} | {STUB[0]}
CORE_IDS = {n[0] for n in CORE}
DOCS_IDS = {n[0] for n in DOCS}
LONELY_IDS = {n[0] for n in LONELY}
NEW_IDS = {n[0] for n in NEW}

_LINKS = [
    {"source": "model_py", "target": "model_invoice", "relation": "contains"},
    {"source": "api_py", "target": "api_charge", "relation": "contains"},
    {"source": "api_rationale_3", "target": "api_charge", "relation": "rationale_for"},
    {"source": "util_py", "target": "util_helper", "relation": "contains"},
    {"source": "pay_md", "target": "pay_md_charging", "relation": "contains"},
    {"source": "lonely_py", "target": "lonely_fn", "relation": "contains"},
]


def nodes_of(rows, community: int | None) -> list[dict]:
    out = []
    for nid, label, ftype, path in rows:
        node = {
            "id": nid,
            "label": label,
            "norm_label": label.lower(),
            "file_type": ftype,
            "source_file": path,
        }
        if community is not None:
            node["community"] = community
        out.append(node)
    return out


def legacy_graph() -> dict:
    nodes = (
        nodes_of(PAY, 1)
        + nodes_of((STUB,), 1)
        + nodes_of(CORE, 2)
        + nodes_of(DOCS, 3)
        + nodes_of(LONELY, 4)
    )
    return {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "links": list(_LINKS),
    }


def mention_graph() -> dict:
    nodes = (
        nodes_of(PAY, 1)
        + nodes_of((STUB,), 1)
        + nodes_of(DOCS, 1)
        + nodes_of(LONELY, 1)
        + nodes_of((EXTRA,), 1)
        + nodes_of(CORE, 2)
        + nodes_of(NEW, 2)
    )
    links = [
        *_LINKS,
        {"source": "util_py", "target": "util_extra", "relation": "contains"},
        {"source": "new_py", "target": "new_fresh", "relation": "contains"},
        {"source": "lonely_fn", "target": "model_invoice", "relation": "calls"},
        # Graphify >= 0.9.62: a markdown code span naming a code node becomes this edge, and
        # its `context` names the span (what engine_generation() counts as a mention edge).
        {
            "source": "pay_md_charging",
            "target": "model_invoice",
            "relation": "references",
            "context": "Invoice",
        },
    ]
    return {
        "directed": False,
        "multigraph": False,
        "graph": {"schema_version": 1, "graphify_version": "0.9.90"},
        "nodes": nodes,
        "links": links,
    }


def accept(
    store: Store, slug: str, *, seeds=(), prefixes=(), supersedes: Domain | None = None
) -> Domain:
    draft = Domain(
        slug=slug,
        title=slug.title(),
        summary=f"{slug} summary",
        seed_anchors=[Descriptor(name=n, file_path=p) for n, p in seeds],
        path_prefixes=list(prefixes),
        supersedes=supersedes.domain_id if supersedes else None,
        provenance=Provenance(source="manual"),
    )
    added = (
        store.supersede_domain(supersedes.domain_id, draft)
        if supersedes
        else store.add_domain(draft)
    )
    store.ratify_domains(accept=[added.domain_id])
    domain = store.get_domain(added.domain_id)
    assert domain is not None and domain.status == DomainStatus.ACCEPTED
    return domain


def standard_domains(store: Store) -> dict[str, Domain]:
    return {
        "payments": accept(
            store, "payments", seeds=[("Invoice", "src/pay/model.py")], prefixes=["src/pay"]
        ),
        "core": accept(store, "core", seeds=[("helper", "src/core/util.py")]),
        "docs": accept(store, "docs", prefixes=["docs"]),
    }


def members(store: Store) -> dict[str, set[str]]:
    return {
        d.slug: store.domain_member_ids(d.domain_id)
        for d in store.iter_domains(status=DomainStatus.ACCEPTED)
    }


def seed_members(store: Store, reader: GraphifyReader, domain: Domain, ids: set[str]) -> None:
    """Hand-set one domain's rows, stamped as current, so a reader that rebuilds a stale index
    (``ensure_domain_membership``) keeps them."""
    by_id = {n.node_id: n for n in reader.list_nodes()}
    store.replace_domain_members(
        domain.domain_id, [(i, by_id[i].file_path or None, by_id[i].community) for i in ids]
    )
    store.set_meta(MEMBERSHIP_STAMP_KEY, membership_stamp(store, reader))


def touch_a_record(root: Path) -> None:
    """What a pull that changes a record looks like to the digest: the next open, or a running
    server's next tool call, reloads the index."""
    path = sorted((root / "domains").glob("*.json"))[0]
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def call_tool(tool: str, arguments: dict):
    async def _run():
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(_run())


def tier1_names(store: Store, record_id: str) -> list[str]:
    return sorted(
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(record_id)
        if b.tier == 1
    )
