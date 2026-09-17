"""Read-only, repository-confined source discovery for Bootstrap previews."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

from sidegraph.bootstrap.model import Exclusion, ScanResult
from sidegraph.profiles import FlowProfile

DEFAULT_MAX_BYTES = 512_000
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".sidegraph",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "graphify-out",
        "_build",
        "coverage",
    }
)


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return f"<outside-repository>/{path.name}"


def _lexical_excluded_directory(root: Path, path: Path) -> Path | None:
    try:
        parts = path.absolute().relative_to(root.absolute()).parts
    except ValueError:
        return None
    candidate = root.absolute()
    for index, part in enumerate(parts, start=1):
        candidate = candidate / part
        if part in EXCLUDED_DIRS and candidate.is_dir():
            return root.absolute().joinpath(*parts[:index])
    return None


def _excluded_directory(root: Path, path: Path) -> Path | None:
    lexical = _lexical_excluded_directory(root, path)
    if lexical is not None:
        return lexical
    resolved = _resolve(path)
    resolved_root = _resolve(root)
    if resolved is None or resolved_root is None:
        return None
    if _lexical_excluded_directory(resolved_root, resolved) is not None:
        return path.absolute()
    return None


def _accept_file(
    root: Path, path: Path, *, included: set[Path], max_bytes: int
) -> tuple[str | None, Exclusion | None]:
    rel = _display_path(root, path)
    if not _inside(root, path):
        return None, Exclusion(path=rel, reason="outside-repository")
    try:
        size = path.stat().st_size
    except OSError:
        return None, Exclusion(path=rel, reason="unreadable")
    resolved = _resolve(path)
    if resolved is None:
        return None, Exclusion(path=rel, reason="outside-repository")
    override = resolved in included
    if size > max_bytes and not override:
        return None, Exclusion(path=rel, reason="over-size-limit")
    try:
        with path.open("rb") as handle:
            prefix = handle.read(4096)
        if b"\0" in prefix:
            return None, Exclusion(path=rel, reason="binary")
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, Exclusion(path=rel, reason="unreadable")
    return rel, None


def _discover_directory(
    root: Path, directory: Path, visited: set[Path] | None = None
) -> tuple[list[Path], list[Exclusion]]:
    if not _inside(root, directory):
        return [directory], []
    excluded = _excluded_directory(root, directory)
    if excluded is not None:
        return [], [Exclusion(path=_display_path(root, excluded), reason="excluded-directory")]
    if visited is None:
        visited = set()
    resolved = _resolve(directory)
    if resolved is None:
        return [directory], []
    if resolved in visited:
        return [], []
    visited.add(resolved)

    files: list[Path] = []
    exclusions: list[Exclusion] = []
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError:
        return [], [Exclusion(path=_display_path(root, directory), reason="unreadable")]

    for child in children:
        try:
            is_directory = child.is_dir()
        except OSError:
            files.append(child)
            continue
        if is_directory:
            nested_files, nested_exclusions = _discover_directory(root, child, visited)
            files.extend(nested_files)
            exclusions.extend(nested_exclusions)
        else:
            files.append(child)
    return files, exclusions


def _discover_profile_pattern(
    root: Path,
    directory: Path,
    pattern: tuple[str, ...],
    index: int = 0,
    visited: set[tuple[Path, int]] | None = None,
) -> tuple[list[Path], list[Exclusion]]:
    if not _inside(root, directory):
        return [directory], []
    excluded = _excluded_directory(root, directory)
    if excluded is not None:
        return [], [Exclusion(path=_display_path(root, excluded), reason="excluded-directory")]
    if visited is None:
        visited = set()
    resolved = _resolve(directory)
    if resolved is None:
        return [directory], []
    state = (resolved, index)
    if state in visited:
        return [], []
    visited.add(state)

    if index == len(pattern):
        return [directory], []
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError:
        return [], [Exclusion(path=_display_path(root, directory), reason="unreadable")]

    files: list[Path] = []
    exclusions: list[Exclusion] = []
    part = pattern[index]
    if part == "**":
        nested_files, nested_exclusions = _discover_profile_pattern(
            root, directory, pattern, index + 1, visited
        )
        files.extend(nested_files)
        exclusions.extend(nested_exclusions)
        for child in children:
            try:
                is_directory = child.is_dir()
            except OSError:
                is_directory = False
            if is_directory:
                nested_files, nested_exclusions = _discover_profile_pattern(
                    root, child, pattern, index, visited
                )
                files.extend(nested_files)
                exclusions.extend(nested_exclusions)
        return files, exclusions

    for child in children:
        if not fnmatchcase(child.name, part):
            continue
        if index == len(pattern) - 1:
            files.append(child)
            continue
        try:
            is_directory = child.is_dir()
        except OSError:
            is_directory = False
        if is_directory:
            nested_files, nested_exclusions = _discover_profile_pattern(
                root, child, pattern, index + 1, visited
            )
            files.extend(nested_files)
            exclusions.extend(nested_exclusions)
    return files, exclusions


def _normalise_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def scan_sources(
    root: Path,
    profile: FlowProfile,
    explicit_paths: tuple[Path, ...] = (),
    included_files: tuple[Path, ...] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ScanResult:
    """Discover eligible profile and explicit sources without reading outside ``root``."""
    root = root.absolute()
    supplied = tuple(_normalise_path(root, path) for path in explicit_paths)
    included_paths = tuple(_normalise_path(root, path) for path in included_files)
    included: set[Path] = set()
    for path in included_paths:
        resolved = _resolve(path)
        if resolved is not None:
            included.add(resolved)
    candidates: list[Path] = []
    exclusions: list[Exclusion] = []

    for pattern in profile.ingest_globs:
        discovered_files, pattern_exclusions = _discover_profile_pattern(
            root, root, Path(pattern).parts
        )
        candidates.extend(discovered_files)
        exclusions.extend(pattern_exclusions)
    for path in supplied:
        try:
            is_directory = path.is_dir()
        except OSError:
            is_directory = False
        if is_directory:
            discovered_files, directory_exclusions = _discover_directory(root, path)
            candidates.extend(discovered_files)
            exclusions.extend(directory_exclusions)
        else:
            candidates.append(path)
    candidates.extend(included_paths)

    accepted_files: set[str] = set()
    for path in sorted(set(candidates), key=lambda candidate: candidate.as_posix()):
        if not _inside(root, path):
            exclusions.append(
                Exclusion(path=_display_path(root, path), reason="outside-repository")
            )
            continue
        resolved = _resolve(path)
        if resolved is None:
            exclusions.append(
                Exclusion(path=_display_path(root, path), reason="outside-repository")
            )
            continue
        excluded = _excluded_directory(root, path)
        if excluded is not None and resolved not in included:
            exclusions.append(
                Exclusion(path=_display_path(root, path), reason="excluded-directory")
            )
            continue
        accepted, exclusion = _accept_file(root, path, included=included, max_bytes=max_bytes)
        if accepted is not None:
            accepted_files.add(accepted)
        if exclusion is not None:
            exclusions.append(exclusion)

    unique_exclusions = {(item.path, item.reason): item for item in exclusions}
    return ScanResult(
        root=root.resolve().as_posix(),
        files=tuple(sorted(accepted_files)),
        exclusions=tuple(
            item for _, item in sorted(unique_exclusions.items(), key=lambda entry: entry[0])
        ),
        max_bytes=max_bytes,
    )
