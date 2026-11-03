"""Guard: a stored record model's serialisation changes only together with the store requirement.

An older client rewrites a record through its own model and drops any field it does not know, and
skips a record whose field has a type or value it cannot read. So a release that changes what a
stored model serialises raises ``RECORD_FIELDS_REQUIREMENT`` to itself; the CHANGELOG then tells
every team to run ``sidegraph-require --sidegraph X.Y.Z`` and commit ``.sidegraph/format``.
The committed snapshot ``tests/fixtures/record_fields.json`` pins each model's serialisation
JSON schema (fields, types, defaults, required, aliases) and the constant.
see docs/reference/stability.md ("Mixed versions")

Regenerate the snapshot: ``uv run python tests/test_record_fields.py --write``. It refuses while
the contract changed and the constant is not above the last released version, when the constant
went down, and when there is no snapshot (``--init`` creates the first one).
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_core import to_jsonable_python

from sidegraph import schema
from sidegraph.store_layout import RECORD_FIELDS_REQUIREMENT, release_tuple

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).parent / "fixtures" / "record_fields.json"
_RELEASED_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] — \d{4}-\d{2}-\d{2}", re.MULTILINE)

State = dict[str, Any]
STALE = (
    "tests/fixtures/record_fields.json is stale; regenerate it: "
    "uv run python tests/test_record_fields.py --write"
)


def _factory_defaults(model: type[BaseModel]) -> dict[str, Any]:
    """What each ``default_factory`` field defaults to: the value when two calls agree, else the
    type name (a fresh ULID or a clock reading varies by design)."""
    out: dict[str, Any] = {}
    for name, info in sorted(model.model_fields.items()):
        factory = info.default_factory
        if factory is None:
            continue
        first, second = to_jsonable_python(factory()), to_jsonable_python(factory())
        out[name] = first if first == second else f"<varies: {type(factory()).__name__}>"
    return out


def _contract(model: type[BaseModel]) -> dict[str, Any]:
    """The model's serialisation as the store writers produce it: ``model_dump(mode="json")``
    without ``by_alias``, so keys are field names, plus the factory defaults."""
    schema_ = model.model_json_schema(mode="serialization", by_alias=False)
    schema_["x-default-factories"] = _factory_defaults(model)
    return schema_


def state_of(models: dict[str, type[BaseModel]], requirement: str) -> State:
    return {
        "requirement": requirement,
        "models": {n: _contract(m) for n, m in sorted(models.items())},
    }


def stored_models() -> dict[str, type[BaseModel]]:
    return {
        name: cls
        for name, cls in inspect.getmembers(schema, inspect.isclass)
        if issubclass(cls, BaseModel) and cls.__module__ == schema.__name__
    }


def unsupported(models: dict[str, type[BaseModel]]) -> list[str]:
    """Configurations the fingerprint cannot cover, so the guard refuses them: alias-on
    serialisation (the writers omit ``by_alias`` and would honour it, while the fingerprint is
    taken with ``by_alias=False``) and data-aware default factories (their output depends on
    other fields, which one call cannot capture)."""
    out = []
    for name, model in sorted(models.items()):
        if model.model_config.get("serialize_by_alias"):
            out.append(
                f"{name} sets serialize_by_alias=True: the fingerprint does not cover alias-on "
                "serialization; extend it (key names with aliases applied) before enabling it"
            )
        for field, info in sorted(model.model_fields.items()):
            if info.default_factory_takes_validated_data:
                out.append(
                    f"{name}.{field} has a default_factory that takes validated data: the "
                    "fingerprint cannot cover it; extend it (fingerprint its output for sample "
                    "data) before using one"
                )
    return out


def current_state() -> State:
    return state_of(stored_models(), RECORD_FIELDS_REQUIREMENT)


def last_released() -> str:
    """The newest dated ``## [X.Y.Z] — date`` heading of CHANGELOG.md."""
    found = _RELEASED_RE.search((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert found, "CHANGELOG.md has no dated release heading"
    return found.group(1)


def _above(version: str, other: str) -> bool:
    a, b = release_tuple(version), release_tuple(other)
    assert a is not None and b is not None
    return a > b


def _changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    lines = []
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        if before is None or after is None:
            lines.append(f"  {name}: model {'added' if before is None else 'removed'}")
            continue
        b, a = set(before.get("properties", {})), set(after.get("properties", {}))
        if a != b:
            lines.append(f"  {name}: added {sorted(a - b)}, removed {sorted(b - a)}")
        else:
            lines.append(f"  {name}: a type, default, default factory or constraint changed")
    return lines


LOWERED = (
    "RECORD_FIELDS_REQUIREMENT went down ({old} -> {new}); a requirement is never lowered "
    "(a store may already require {old})"
)


def _lowered(snap: State, now: State) -> bool:
    return _above(snap["requirement"], now["requirement"])


def problem(snap: State, now: State, released: str) -> str | None:
    """Why ``now`` may not stand against the snapshot ``snap``, or ``None``."""
    changed = _changes(snap["models"], now["models"])
    if changed and not _above(now["requirement"], released):
        return (
            "A stored record model's serialisation changed, but RECORD_FIELDS_REQUIREMENT "
            f"({now['requirement']}) is not above the last released version ({released}):\n"
            + "\n".join(changed)
            + "\nAn older client would silently drop the field, or skip the record. Do all three:\n"
            "  1. raise RECORD_FIELDS_REQUIREMENT in src/sidegraph/store_layout.py to the release "
            "that ships the change;\n"
            "  2. regenerate the snapshot: uv run python tests/test_record_fields.py --write;\n"
            "  3. in that release's CHANGELOG entry say that all clients must upgrade, and that "
            "every team runs `sidegraph-require --sidegraph X.Y.Z` and commits "
            "`.sidegraph/format` before anyone on the new version writes.\n"
            "See docs/reference/stability.md (Mixed versions)."
        )
    if _lowered(snap, now):
        return LOWERED.format(old=snap["requirement"], new=now["requirement"])
    if changed or now["requirement"] != snap["requirement"]:
        return STALE
    return None


def test_record_models_change_only_with_the_requirement() -> None:
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert problem(snap, current_state(), last_released()) is None, problem(
        snap, current_state(), last_released()
    )


def _variant(requirement: str = "0.20.0", **changes: Any) -> State:
    fields: dict[str, Any] = {"name": (str, ...), "count": (int, 0)}
    fields.update(changes)
    fields = {k: v for k, v in fields.items() if v is not None}
    return state_of({"M": create_model("M", **fields)}, requirement)


def test_no_stored_model_uses_what_the_fingerprint_cannot_cover() -> None:
    assert unsupported(stored_models()) == []


def test_serialize_by_alias_is_refused() -> None:
    model = create_model(
        "M",
        __config__=ConfigDict(serialize_by_alias=True),
        name=(str, Field(serialization_alias="label")),
    )
    found = unsupported({"M": model})
    assert len(found) == 1 and "serialize_by_alias" in found[0] and "extend it" in found[0]


def test_serialize_by_alias_is_refused_on_an_inheriting_model() -> None:
    base = create_model("B", __config__=ConfigDict(serialize_by_alias=True), name=(str, ...))
    child = create_model("C", __base__=base, extra=(int, 0))
    assert any("C sets serialize_by_alias" in line for line in unsupported({"C": child}))


def test_a_data_aware_default_factory_is_refused() -> None:
    model = create_model(
        "M", name=(str, ...), tags=(list[str], Field(default_factory=lambda data: [data["name"]]))
    )
    found = unsupported({"M": model})
    assert len(found) == 1 and "M.tags" in found[0] and "validated data" in found[0]


def test_a_plain_default_factory_is_still_supported() -> None:
    model = create_model("M", tags=(list[str], Field(default_factory=list)))
    assert unsupported({"M": model}) == []


def test_unchanged_contract_needs_no_raise() -> None:
    assert problem(_variant(), _variant(), "0.20.0") is None


def test_a_new_field_with_the_constant_unchanged_fails() -> None:
    msg = problem(_variant(), _variant(extra=(str | None, None)), "0.20.0")
    assert msg and "added ['extra']" in msg and "sidegraph-require" in msg


def test_a_lowered_constant_fails() -> None:
    snap = _variant("0.22.0")
    msg = problem(snap, _variant("0.1.0", extra=(str | None, None)), "0.21.0")
    assert msg and "not above the last released" in msg


def test_a_second_field_in_the_same_cycle_passes_once_regenerated() -> None:
    first = _variant("0.24.0", a=(str | None, None))
    second = _variant("0.24.0", a=(str | None, None), b=(str | None, None))
    assert problem(first, second, "0.23.0") == STALE
    assert problem(second, second, "0.23.0") is None


def test_an_alias_rename_changes_the_persisted_key() -> None:
    """The writers dump without by_alias: ``label`` with ``alias="name"`` persists as ``label``."""
    now = _variant(name=None, label=(str, Field(alias="name")))
    msg = problem(_variant(), now, "0.20.0")
    assert msg and "added ['label'], removed ['name']" in msg


def test_a_serialization_alias_does_not_change_what_the_writers_dump() -> None:
    now = _variant(name=(str, Field(serialization_alias="label")))
    assert problem(_variant(), now, "0.20.0") is None


def test_a_default_factory_change_is_a_contract_change() -> None:
    before = _variant(tags=(list[str], Field(default_factory=list)))
    after = _variant(tags=(list[str], Field(default_factory=lambda: ["new"])))
    msg = problem(before, after, "0.20.0")
    assert msg and "default factory" in msg


def test_an_existing_factory_backed_field_is_fingerprinted() -> None:
    factories = current_state()["models"]["Domain"]["x-default-factories"]
    assert factories["communities"] == []
    assert factories["domain_id"].startswith("<varies")


def test_a_lowered_requirement_fails_without_any_schema_change() -> None:
    msg = problem(_variant("0.24.0"), _variant("0.17.0"), "0.23.0")
    assert msg and "never lowered" in msg


def test_a_type_change_is_a_contract_change() -> None:
    msg = problem(_variant(), _variant(count=(str, "0")), "0.20.0")
    assert msg and "type, default, default factory or constraint" in msg


def test_the_guard_covers_every_stored_record_type() -> None:
    assert {"Decision", "Fact", "Domain", "Entity", "AnchorBinding", "DriftCheck"} <= set(
        current_state()["models"]
    )


def refusal(snap: State | None, now: State, released: str, init: bool) -> str | None:
    """Why the snapshot may not be (re)written, or ``None``."""
    if snap is None:
        return None if init else "no snapshot to compare with; create the first one with --init"
    if _lowered(snap, now):
        return LOWERED.format(old=snap["requirement"], new=now["requirement"])
    if _changes(snap["models"], now["models"]) and not _above(now["requirement"], released):
        return (
            f"the contract changed but RECORD_FIELDS_REQUIREMENT ({now['requirement']}) is not "
            f"above the last released version ({released}); raise it in "
            "src/sidegraph/store_layout.py first"
        )
    return None


def test_write_refuses_without_a_baseline_unless_init() -> None:
    now = current_state()
    assert "--init" in (refusal(None, now, "0.23.0", init=False) or "")
    assert refusal(None, now, "0.23.0", init=True) is None


def test_write_refuses_a_lowered_requirement_with_unchanged_schema() -> None:
    why = refusal(_variant("0.24.0"), _variant("0.17.0"), "0.23.0", False)
    assert why and "never lowered" in why


def test_write_refuses_a_changed_contract_below_the_release() -> None:
    now = _variant("0.20.0", extra=(str | None, None))
    assert "not above" in (refusal(_variant(), now, "0.23.0", False) or "")


def write(init: bool) -> None:
    if bad := unsupported(stored_models()):
        sys.exit("refusing: " + "; ".join(bad))
    now = current_state()
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else None
    why = refusal(snap, now, last_released(), init)
    if why:
        sys.exit("refusing: " + why)
    SNAPSHOT.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def test_write_refuses_an_unsupported_configuration(tmp_path: Path, monkeypatch: Any) -> None:
    model = create_model("M", __config__=ConfigDict(serialize_by_alias=True), name=(str, ...))
    monkeypatch.setattr(sys.modules[__name__], "stored_models", lambda: {"M": model})
    monkeypatch.setattr(sys.modules[__name__], "SNAPSHOT", tmp_path / "snap.json")
    try:
        write(init=True)
    except SystemExit as exit_:
        assert "serialize_by_alias" in str(exit_)
    else:
        raise AssertionError("write() accepted an unsupported configuration")
    assert not (tmp_path / "snap.json").exists()


if __name__ == "__main__":
    if sys.argv[1:] in (["--write"], ["--init"]):
        write(init=sys.argv[1] == "--init")
    else:
        sys.exit("usage: test_record_fields.py --write | --init")
