"""Execute the thin OpenCode entry; the SDK double does not certify native host behavior."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(__file__).with_name("opencode_plugin_runner.mjs")
SKILLS = ROOT / "plugin" / "sidegraph" / "skills"


def _entry() -> Path:
    path = ROOT / "index.js"
    assert path.is_file(), "The OpenCode Git package must contain index.js"
    return path


def _fixture(tmp_path, *, crlf=False):
    entry = _entry()
    package = tmp_path / "packagé"
    package.mkdir()
    shutil.copyfile(entry, package / "index.js")
    yaml = {}
    bodies = {}
    names = sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md")) + ["future-skill"]
    for name in names:
        metadata = {"name": f"Display {name}", "description": f"Description {name}"}
        header = json.dumps(metadata)  # JSON is valid YAML; no second YAML parser in the double.
        yaml[header] = metadata
        newline = "\r\n" if crlf else "\n"
        body = f"# {name}{newline}Body café.{newline}--- stays in the body.{newline}"
        path = package / "plugin" / "sidegraph" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes((f"---{newline}{header}{newline}---{newline}" + body).encode())
        bodies[name] = body
    return package, {"yaml": yaml}, bodies


def _run(package, options, tmp_path):
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("OpenCode execution checks require Node in CI")
        pytest.skip("Node is unavailable outside CI")
    config = tmp_path / "runner-input.json"
    config.write_text(json.dumps(options), encoding="utf-8")
    result = subprocess.run(
        [
            node,
            "--no-warnings",
            "--experimental-vm-modules",
            str(RUNNER),
            str(package / "index.js"),
            str(config),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


def test_package_exports_native_entry_at_current_version():
    _entry()
    path = ROOT / "package.json"
    assert path.is_file(), "The Git package requires package.json"
    package = json.loads(path.read_text())
    assert package["name"] == "sidegraph"
    assert package["type"] == "module"
    assert package["exports"] == "./index.js"
    assert (
        package["version"]
        == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    )
    assert not any(name in package for name in ("files", "scripts", "dependencies"))


@pytest.mark.parametrize("crlf", [False, True])
def test_all_skills_and_future_addition_load_with_original_body_and_absolute_path(tmp_path, crlf):
    package, options, bodies = _fixture(tmp_path, crlf=crlf)
    result = _run(package, options, tmp_path)
    assert result.get("error") is None
    assert result["id"] == "sidegraph"
    assert set(result["skills"]) == {f"sidegraph-{name}" for name in bodies}
    for name, body in bodies.items():
        skill = result["skills"][f"sidegraph-{name}"]
        assert skill["name"] == f"Display {name}"
        assert skill["description"] == f"Description {name}"
        assert skill["content"] == body
        assert Path(skill["path"]) == package / "plugin/sidegraph/skills" / name / "SKILL.md"
    assert result["disposed"] == ["mcp", "skill"]


def test_mcp_uses_the_public_argv_and_environment(tmp_path):
    package, options, _ = _fixture(tmp_path)
    result = _run(package, options, tmp_path)
    assert result.get("error") is None
    public = ROOT / "plugin/sidegraph/.mcp.public.json"
    if not public.exists():
        public = ROOT / "plugin/sidegraph/.mcp.json"
    expected = json.loads(public.read_text())["mcpServers"]["sidegraph"]
    assert result["servers"] == {
        "sidegraph": {
            "type": "local",
            "command": [expected["command"], *expected["args"]],
            "environment": expected["env"],
        }
    }


def test_existing_skill_and_disabled_mcp_remain_exactly_as_configured(tmp_path):
    package, options, bodies = _fixture(tmp_path)
    name = next(iter(bodies))
    original = {"id": f"sidegraph-{name}", "name": "Explicit override", "content": "unchanged"}
    disabled = {"type": "local", "command": ["do-not-start"], "disabled": True}
    options.update(skills={original["id"]: original}, servers={"sidegraph": disabled})
    result = _run(package, options, tmp_path)
    assert result.get("error") is None
    assert result["skills"][original["id"]] == original
    assert result["servers"]["sidegraph"] == disabled


def test_folded_header_is_passed_to_native_yaml_without_changing_the_body(tmp_path):
    package, options, _ = _fixture(tmp_path)
    path = package / "plugin/sidegraph/skills/future-skill/SKILL.md"
    header = "name: Folded\ndescription: >-\n  first line\n  second line"
    options["yaml"][header] = {"name": "Folded", "description": "first line second line"}
    body = "# Body\nPreserve café and --- exactly.\n"
    path.write_bytes((f"---\n{header}\n---\n" + body).encode())
    result = _run(package, options, tmp_path)
    assert result.get("error") is None
    skill = result["skills"]["sidegraph-future-skill"]
    assert skill["description"] == "first line second line"
    assert skill["content"] == body


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {},
        {"name": 1, "description": "x"},
        {"name": "", "description": "x"},
        {"name": "x", "description": " "},
    ],
)
def test_invalid_metadata_never_leaves_partial_registrations(tmp_path, metadata):
    package, options, _ = _fixture(tmp_path)
    path = package / "plugin/sidegraph/skills/future-skill/SKILL.md"
    header = json.dumps(metadata)
    options["yaml"][header] = metadata
    path.write_text(f"---\n{header}\n---\nBody\n", encoding="utf-8")
    result = _run(package, options, tmp_path)
    assert result.get("error")
    assert "future-skill" in result["error"]
    assert result["events"] == []


@pytest.mark.parametrize("mode", ["missing-file", "no-frontmatter", "unterminated", "bad-yaml"])
def test_bad_packaged_input_fails_before_registering_anything(tmp_path, mode):
    package, options, _ = _fixture(tmp_path)
    path = package / "plugin/sidegraph/skills/future-skill/SKILL.md"
    if mode == "missing-file":
        path.unlink()
    else:
        path.write_text(
            {
                "no-frontmatter": "Body\n",
                "unterminated": "---\nname: x\n",
                "bad-yaml": "---\nmalformed: [\n---\nBody\n",
            }[mode],
            encoding="utf-8",
        )
    result = _run(package, options, tmp_path)
    assert result.get("error")
    assert "future-skill" in result["error"]
    assert result["events"] == []


def test_failed_second_registration_disposes_the_first(tmp_path):
    package, options, _ = _fixture(tmp_path)
    options["failRegistration"] = "mcp"
    result = _run(package, options, tmp_path)
    assert "registration:mcp" in result["error"]
    assert result["disposed"] == ["skill"]


def test_empty_packaged_skill_directory_does_not_look_like_a_complete_plugin(tmp_path):
    package, options, _ = _fixture(tmp_path)
    directory = package / "plugin/sidegraph/skills"
    for child in directory.iterdir():
        shutil.rmtree(child)
    result = _run(package, options, tmp_path)
    assert result.get("error")
    assert result["events"] == []


def test_cleanup_attempts_both_disposals_when_one_fails(tmp_path):
    package, options, _ = _fixture(tmp_path)
    options["failDispose"] = "mcp"
    result = _run(package, options, tmp_path)
    assert result.get("error") is None
    assert "dispose:mcp" in result["cleanupError"]
    assert result["disposed"] == ["mcp", "skill"]
