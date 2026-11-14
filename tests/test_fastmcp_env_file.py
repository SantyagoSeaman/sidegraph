"""A ``.env`` file in the working directory must not configure the MCP server.

FastMCP reads ``.env`` once, when it is first imported, so each case runs in a child
interpreter whose working directory holds the file.
# see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D7)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _settings(tmp_path: Path, module: str, env: dict[str, str] | None = None) -> str:
    """Import ``module`` in a child whose working directory is ``tmp_path``; return
    ``"<log_level> <transport>"`` from ``fastmcp.settings``."""
    code = f"import {module}, fastmcp; s = fastmcp.settings; print(s.log_level, s.transport)"
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("FASTMCP_")}
    child_env.update(env or {})
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def dotenv(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("FASTMCP_LOG_LEVEL=CRITICAL\nFASTMCP_TRANSPORT=http\n", encoding="utf-8")
    return path


def test_the_file_is_what_fastmcp_alone_would_read(tmp_path, dotenv):
    assert _settings(tmp_path, "fastmcp") == "CRITICAL http"


@pytest.mark.parametrize("module", ["sidegraph.server", "sidegraph.host.mcp_entry"])
def test_a_dotenv_file_does_not_change_the_server(tmp_path, dotenv, module):
    assert _settings(tmp_path, module) == "INFO stdio"


def test_an_exported_env_file_is_honoured(tmp_path, dotenv):
    env = {"FASTMCP_ENV_FILE": str(dotenv)}
    assert _settings(tmp_path, "sidegraph.server", env) == "CRITICAL http"


def test_exported_settings_still_apply(tmp_path):
    env = {"FASTMCP_LOG_LEVEL": "ERROR"}
    assert _settings(tmp_path, "sidegraph.server", env) == "ERROR stdio"
