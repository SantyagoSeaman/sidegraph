"""Domain authoring CLI rejects bad fields without value-reflecting validation errors."""

import pytest

from sidegraph.cli import domains_main

MARKER = "opaque" + "S3Probe"


@pytest.mark.parametrize("argument", ["--slug", "--parent"])
def test_domain_cli_diagnostic_omits_unknown_or_malformed_identifiers(tmp_path, capsys, argument):
    path = tmp_path / "store"
    args = [
        "add",
        "--db",
        str(path),
        "--slug",
        "ordinary",
        "--title",
        "ordinary",
        "--summary",
        "ordinary",
    ]
    if argument == "--slug":
        args[4] = MARKER.upper()
    else:
        args.extend([argument, MARKER])
    result = domains_main(args)
    # Invalid slug is rejected before the CLI creates a store at all.
    if argument == "--slug":
        assert not path.exists()
    assert result == 1
    output = capsys.readouterr()
    assert MARKER.casefold() not in (output.out + output.err).casefold()
    assert "slug" in output.out if argument == "--slug" else "parent" in output.out


def test_domain_cli_caught_store_failure_omits_the_cause(tmp_path, monkeypatch, capsys):
    from sidegraph.store import Store

    path = tmp_path / "store"
    with Store(path):
        pass
    before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*.json")}

    def fail(self, domain):
        raise ValueError(MARKER)

    monkeypatch.setattr(Store, "add_domain", fail)
    result = domains_main(
        [
            "add",
            "--db",
            str(path),
            "--slug",
            "ordinary",
            "--title",
            "ordinary",
            "--summary",
            "ordinary",
        ]
    )
    assert {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*.json")} == before
    with Store(path) as reopened:
        assert list(reopened.iter_domains()) == []
    assert result == 0  # Existing CLI duplicate/refusal branch semantics.
    output = capsys.readouterr()
    assert MARKER not in output.out + output.err
    assert output.out == "proposed 0 domain(s) (skipped: 1 existing)\n"
