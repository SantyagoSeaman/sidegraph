"""Every atomic write gets its own tmp path (design D5).

_atomic_write_text used a FIXED `<name>.tmp`, so two writers of the same record file shared
one inode: one's open(..., "w") truncation can land inside the other's os.replace window and
commit an EMPTY canonical record. That corruption class is already known-real here -- it is
why _atomic_write_text_race_tolerant has unique names -- and the store's own records were the
one thing still exposed to it.
"""

from __future__ import annotations

from sidegraph.store import _atomic_write_text


def test_two_writes_to_one_target_never_share_a_tmp_path(tmp_path, monkeypatch):
    target = tmp_path / "rec.json"
    seen: list[str] = []

    real = type(target).write_text

    def _spy(self, text, **kwargs):
        if self.name.endswith(".tmp"):
            seen.append(self.name)
        return real(self, text, **kwargs)

    monkeypatch.setattr(type(target), "write_text", _spy)

    _atomic_write_text(target, "one")
    _atomic_write_text(target, "two")

    assert len(seen) == 2
    assert seen[0] != seen[1], f"both writes used the same tmp path: {seen[0]}"
    assert target.read_text() == "two"
