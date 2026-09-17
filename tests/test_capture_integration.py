from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.server import (
    _list_proposed_impl,
    _propose_decisions_impl,
    _ratify_decisions_impl,
)
from sidegraph.store import Store

SLICE = Path(__file__).parent / "fixtures" / "bitfinex_slice.json"


def test_full_capture_loop_propose_ratify_retrieve(tmp_path):
    reader = GraphifyReader(SLICE)
    store = Store(tmp_path / "e.db")

    # 1. The agent (nudged by the Stop hook) proposes a distilled gotcha with a secret in it.
    results = _propose_decisions_impl(
        store,
        reader,
        [
            {
                "title": "Rate-limit retries live in the adapter",
                "kind": "gotcha",
                "context": "429 bursts; do not use api_key=sk-live-abc123 directly",
                "choice": "retry with backoff inside BitfinexAdapter",
                "anchors": [{"name": "BitfinexAdapter", "file_path": "adapters/bitfinex.py"}],
            }
        ],
        session_id="sess-e2e",
    )
    assert results[0]["status"] == "written"
    did = results[0]["decision_id"]
    assert results[0]["redactions"] >= 1

    # 2. Nothing un-redacted reached the store (the security gate).
    stored = store.get_decision(did)
    assert "sk-live-abc123" not in stored.context

    # 3. Pending proposal is listed; retrieval shows it tagged [unratified].
    assert did in _list_proposed_impl(store)
    ctx = get_task_context([Seed(file_path="adapters/bitfinex.py")], store, reader)
    rendered = ctx.render()
    assert "Rate-limit retries" in rendered and "[unratified]" in rendered

    # 4. Ratify: the tag disappears; the decision stays mistakes-first.
    assert _ratify_decisions_impl(store, accept=[did])[did] == "accepted"
    ctx2 = get_task_context([Seed(file_path="adapters/bitfinex.py")], store, reader)
    rendered2 = ctx2.render()
    assert "Rate-limit retries" in rendered2
    assert "[unratified]" not in rendered2
    assert any("Rate-limit retries" in m for m in ctx2.mistakes)
