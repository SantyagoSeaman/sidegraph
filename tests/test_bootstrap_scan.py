from pathlib import Path

from sidegraph.bootstrap.model import Exclusion
from sidegraph.bootstrap.scan import scan_sources
from sidegraph.profiles import get_profile


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_profile_scan_returns_stable_repo_relative_paths(tmp_path: Path) -> None:
    write(tmp_path / "docs/adr/b.md", "# B\n")
    write(tmp_path / "docs/adr/a.md", "# A\n")

    result = scan_sources(tmp_path, get_profile("generic-adr"))

    assert result.files == ("docs/adr/a.md", "docs/adr/b.md")


def test_profile_scan_stops_at_an_excluded_directory(tmp_path: Path) -> None:
    write(tmp_path / "docs/features/build/adr/hidden.md", "# Hidden\n")
    write(tmp_path / "docs/features/payments/adr/safe.md", "# Safe\n")

    result = scan_sources(tmp_path, get_profile("genkovich-sdd"))

    assert result.files == ("docs/features/payments/adr/safe.md",)
    assert result.exclusions == (
        Exclusion(path="docs/features/build", reason="excluded-directory"),
    )


def test_self_referential_and_broken_links_are_excluded(tmp_path: Path) -> None:
    self_referential = tmp_path / "docs/adr/self.md"
    broken = tmp_path / "docs/adr/broken.md"
    self_referential.parent.mkdir(parents=True)
    self_referential.symlink_to("self.md")
    broken.symlink_to("missing.md")

    result = scan_sources(
        tmp_path,
        get_profile("generic-adr"),
        (self_referential, broken),
        included_files=(self_referential,),
    )

    assert result.files == ()
    assert result.exclusions == (
        Exclusion(path="docs/adr/broken.md", reason="outside-repository"),
        Exclusion(path="docs/adr/self.md", reason="outside-repository"),
    )


def test_explicit_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-bootstrap.md"
    outside.write_text("# secret\n", encoding="utf-8")
    link = tmp_path / "docs/adr/link.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    result = scan_sources(tmp_path, get_profile("generic-adr"), (link,))

    assert result.files == ()
    assert [(item.path, item.reason) for item in result.exclusions] == [
        ("docs/adr/link.md", "outside-repository")
    ]


def test_symlink_escape_wins_over_directory_exclusion(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-bootstrap.md"
    outside.write_text("# secret\n", encoding="utf-8")
    link = tmp_path / "build/link.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    result = scan_sources(tmp_path, get_profile("generic-adr"), (link,))

    assert result.exclusions == (Exclusion(path="build/link.md", reason="outside-repository"),)


def test_in_repo_directory_symlink_cycle_is_not_descended_twice(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    write(docs / "adr/safe.md", "# Safe\n")
    (docs / "loop").symlink_to(docs, target_is_directory=True)

    result = scan_sources(tmp_path, get_profile("generic-adr"), (docs,))

    assert result.files == ("docs/adr/safe.md",)


def test_direct_outside_path_does_not_leak_absolute_parent(tmp_path: Path) -> None:
    outside = tmp_path.parent / "private-parent" / "decision.md"
    outside.parent.mkdir(exist_ok=True)
    outside.write_text("# Decision\n", encoding="utf-8")

    result = scan_sources(tmp_path, get_profile("generic-adr"), (outside,))

    assert result.exclusions[0] == Exclusion(
        path="<outside-repository>/decision.md", reason="outside-repository"
    )
    assert str(outside.parent) not in result.model_dump_json()


def test_binary_and_oversize_files_are_reported_not_read(tmp_path: Path) -> None:
    binary = write_bytes(tmp_path / "docs/adr/binary.md", b"a\0b")
    large = write(tmp_path / "docs/adr/large.md", "x" * 33)

    result = scan_sources(tmp_path, get_profile("generic-adr"), (binary, large), max_bytes=32)

    assert [item.reason for item in result.exclusions] == ["binary", "over-size-limit"]


def test_binary_sniff_reads_only_a_prefix(tmp_path: Path, monkeypatch) -> None:
    binary = write_bytes(tmp_path / "build/large.md", b"\0" + b"x" * 1_000_000)

    def forbidden_full_binary_read(self: Path) -> bytes:
        raise AssertionError("binary sniff must not call Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbidden_full_binary_read)

    result = scan_sources(
        tmp_path,
        get_profile("generic-adr"),
        (binary,),
        included_files=(binary,),
        max_bytes=32,
    )

    assert result.exclusions[0].reason == "binary"


def test_include_overrides_one_excluded_text_file_only(tmp_path: Path) -> None:
    generated = write(tmp_path / "build/decision.md", "# Decision\n")
    write(tmp_path / "build/other.md", "# Other\n")

    result = scan_sources(
        tmp_path,
        get_profile("generic-adr"),
        (tmp_path / "build",),
        included_files=(generated,),
    )

    assert result.files == ("build/decision.md",)
    assert "build/other.md" not in result.files


def test_profile_file_symlink_to_resolved_excluded_directory_is_rejected(
    tmp_path: Path,
) -> None:
    hidden = write(tmp_path / "build/hidden.md", "# Hidden\n")
    alias = tmp_path / "docs" / "adr" / "alias.md"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(hidden)

    result = scan_sources(tmp_path, get_profile("generic-adr"))

    assert result.files == ()
    assert result.exclusions == (Exclusion(path="docs/adr/alias.md", reason="excluded-directory"),)


def test_explicit_directory_symlink_to_sidegraph_is_rejected_with_lexical_path(
    tmp_path: Path,
) -> None:
    write(tmp_path / ".sidegraph/private.md", "# Private\n")
    alias = tmp_path / "docs" / "memory"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(tmp_path / ".sidegraph", target_is_directory=True)

    result = scan_sources(tmp_path, get_profile("generic-adr"), (alias,))

    assert result.files == ()
    assert result.exclusions == (Exclusion(path="docs/memory", reason="excluded-directory"),)


def test_exact_file_include_overrides_resolved_excluded_alias(tmp_path: Path) -> None:
    hidden = write(tmp_path / "build/hidden.md", "# Included deliberately\n")
    alias = tmp_path / "docs" / "adr" / "alias.md"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(hidden)

    result = scan_sources(
        tmp_path,
        get_profile("generic-adr"),
        included_files=(alias,),
    )

    assert result.files == ("docs/adr/alias.md",)
