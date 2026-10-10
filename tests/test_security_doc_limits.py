"""Document admission rejects whole inputs before parsing or secret processing."""

import json

import pytest

import sidegraph.doc_import as module
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    with Store(tmp_path / "store") as store:
        yield tmp_path, store, GraphifyReader(graph)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("size,skipped", [(127, False), (128, False), (129, True)])
def test_document_byte_admission_before_parser(environment, monkeypatch, dry_run, size, skipped):
    root, store, reader = environment
    doc = root / "large.md"
    doc.write_bytes(b"x" * size)
    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 128, raising=False)
    parsed = []

    def parser(text, *args, **kwargs):
        parsed.append(text)
        return [], None

    monkeypatch.setattr(module, "_parse_decision_docs_with_reason", parser)
    report = module.import_docs(store, reader, [doc], any_doc=True, dry_run=dry_run)
    assert getattr(report, "skipped_oversized", 0) == int(skipped)
    assert getattr(report, "oversized_files", []) == ([str(doc)] if skipped else [])
    assert parsed == ([] if skipped else ["x" * size])
    assert list(store.iter_decisions()) == []


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "tag", ["x" * (256 * 1024 + 1), "invalid\ud800"], ids=["oversize", "surrogate"]
)
def test_operator_tags_reject_before_normalization(environment, monkeypatch, dry_run, tag):
    root, store, reader = environment
    calls = []
    monkeypatch.setattr(module, "normalize_tags", lambda value: (calls.append(value) or [], 0))
    with pytest.raises(ValueError, match="resource limits"):
        module.import_docs(store, reader, [], tags=[tag], any_doc=True, dry_run=dry_run)
    assert calls == []
    assert list(store.iter_decisions()) == []


@pytest.mark.parametrize("parser", [module.parse_decision_doc, module.parse_decision_docs])
def test_exported_text_parser_caps_raw_input_before_redact(monkeypatch, parser):
    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 128, raising=False)
    calls = []
    monkeypatch.setattr(module, "redact", lambda text: (calls.append(text) or text, 0))
    with pytest.raises(ValueError, match="resource limits"):
        parser("x" * 129, "sample.md")
    assert calls == []


@pytest.mark.parametrize("parser", [module.parse_decision_doc, module.parse_decision_docs])
def test_exported_text_parser_surrogate_reject_is_static(parser):
    with pytest.raises(ValueError, match="resource limits") as error:
        parser("privateMarker\ud800", "sample.md")
    assert "privateMarker" not in str(error.value)


def test_import_reads_only_limit_plus_one_from_one_handle(environment, monkeypatch):
    root, store, reader = environment
    doc = root / "large.md"
    doc.write_bytes(b"x" * 1024)
    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 128)
    calls = []
    original = module.os.fdopen

    class Handle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            calls.append(size)
            return self.handle.read(size)

    def opened(fd, *args, **kwargs):
        return Handle(original(fd, *args, **kwargs))

    monkeypatch.setattr(module.os, "fdopen", opened)
    report = module.import_docs(store, reader, [doc], any_doc=True)
    assert report.skipped_oversized == 1
    assert calls == [129]


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_oversized_report_precedes_final_undecodable_block(
    tmp_path, monkeypatch, capsys, dry_run
):
    from sidegraph.cli import import_main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 128)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    large = tmp_path / "large.md"
    large.write_bytes(b"x" * 129)
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    arguments = [
        "--db",
        str(tmp_path / "store"),
        "--graph",
        str(graph),
        "--docs",
        str(tmp_path),
        "--any-doc",
    ]
    if dry_run:
        arguments.append("--dry-run")
    assert import_main(arguments) == 0
    output = capsys.readouterr().out
    assert "1 file(s) skipped: exceeds 8 MiB input limit:" in output
    assert output.index("exceeds 8 MiB") < output.index("not valid UTF-8")
    assert output.rstrip().endswith(str(invalid))
