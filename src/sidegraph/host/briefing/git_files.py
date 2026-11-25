"""Find the working set without opening the store.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (D6).
"""

import subprocess
import time
from pathlib import Path

from ...gitenv import run_git

_BASE_REFS = (
    "refs/remotes/origin/HEAD",
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
    "refs/heads/main",
    "refs/heads/master",
)


def changed_files(root: Path, store_dir: Path) -> tuple[str, ...]:
    """Return changed paths relative to the anchor root within a two-second deadline."""
    root = root.resolve()
    store_dir = store_dir.resolve()
    deadline = time.monotonic() + 2.0

    def run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, 2.0)
        return run_git(args, root, capture_output=True, timeout=min(1.0, remaining))

    try:
        refs = run(["for-each-ref", "--format=%(refname) %(symref)", *_BASE_REFS])
        if refs.returncode:
            return ()
        listed = {}
        for line in refs.stdout.decode("utf-8", "surrogateescape").splitlines():
            name, _, target = line.partition(" ")
            listed[name] = target
        base = listed.get(_BASE_REFS[0]) or next(
            (ref for ref in _BASE_REFS[1:] if ref in listed), None
        )
        diff = ["diff", "--name-only", "-z", "--no-renames", "--relative"]
        paths: set[str] = set()
        if base:
            branch = run([*diff, f"{base}...HEAD"])
            if not branch.returncode:
                paths.update(branch.stdout.decode("utf-8", "surrogateescape").split("\0"))
        for args in (
            [*diff, "--cached"],
            diff,
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ):
            listing = run(args)
            if listing.returncode:
                return ()
            paths.update(listing.stdout.decode("utf-8", "surrogateescape").split("\0"))
    except (OSError, subprocess.SubprocessError):
        return ()
    if time.monotonic() >= deadline:
        return ()
    return tuple(
        sorted(path for path in paths if path and not (root / path).is_relative_to(store_dir))
    )
