"""Subprocess and lifetime behavior for the real portal entry point."""

import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from sidegraph.store import Store

pytestmark = pytest.mark.slow
_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe or signal")


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / ".sidegraph"
    with Store(path):
        pass
    return path


@contextlib.contextmanager
def _child(
    *args: str, stderr: int = subprocess.DEVNULL, **kwargs: object
) -> Iterator[subprocess.Popen[str]]:
    with subprocess.Popen(
        [sys.executable, "-m", "sidegraph.portal", "--no-browser", *args],
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        **kwargs,
    ) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.kill()


def _line(child: subprocess.Popen[str], timeout: float = 10) -> str:
    assert child.stdout is not None
    stream = child.stdout
    lines: queue.SimpleQueue[str] = queue.SimpleQueue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    return lines.get(timeout=timeout).rstrip("\n")


def _url(child: subprocess.Popen[str]) -> tuple[str, str]:
    line = _line(child)
    assert line.startswith("url "), line
    url = urlsplit(line.removeprefix("url "))
    token = url.fragment.removeprefix("t=")
    assert url.hostname == "127.0.0.1" and len(token) == 43
    return f"{url.scheme}://{url.netloc}", token


def _running(pid: int) -> bool:
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return status.returncode == 0 and not status.stdout.lstrip().startswith("Z")


@contextlib.contextmanager
def _stream(origin: str, token: str, timeout: float = 2) -> Iterator[httpx.Response]:
    with httpx.Client(base_url=origin, timeout=timeout, trust_env=False) as client:
        ticket_response = client.post(
            "/api/live-ticket", headers={"Origin": origin, "X-Sidegraph-Token": token}
        )
        assert ticket_response.status_code == 200
        ticket = ticket_response.json()["ticket"]
        with client.stream("GET", "/live", params={"ticket": ticket}) as stream:
            assert stream.status_code == 200
            yield stream


@_posix
def test_url_line_flushed_then_stdout_detached(store_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    try:
        with _child(
            "--db", str(store_path), "--lifetime-fd", str(read_fd), pass_fds=(read_fd,)
        ) as child:
            origin, _ = _url(child)
            assert _line(child, timeout=2) == ""
            assert child.poll() is None
            with httpx.Client(base_url=origin, timeout=2, trust_env=False) as client:
                for _ in range(5000):
                    assert client.get("/").status_code == 200
    finally:
        os.close(read_fd)
        os.close(write_fd)


@_posix
def test_error_line_when_store_missing(tmp_path: Path) -> None:
    with _child("--db", str(tmp_path / "missing")) as child:
        line = _line(child)
        assert line.startswith("error Sidegraph: no store at "), line
        assert "sidegraph-init" in line
        assert child.wait(timeout=5) != 0
    assert not (tmp_path / "missing").exists()


@_posix
def test_never_creates_store_in_git_root(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with _child(cwd=tmp_path) as child:
        assert _line(child).startswith("error Sidegraph: no store at ")
        assert child.wait(timeout=5) != 0
    assert not (tmp_path / ".sidegraph").exists()


@_posix
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("unrelated_json", [False, True])
def test_existing_empty_directory_is_not_a_store(
    tmp_path: Path, explicit: bool, unrelated_json: bool
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    destination = tmp_path / ".sidegraph"
    destination.mkdir()
    if unrelated_json:
        folder = destination / "entities"
        folder.mkdir()
        (folder / "catalog.json").write_text('{"currency":"GBP"}')
    before = set(destination.rglob("*"))
    args = ("--db", str(destination)) if explicit else ()
    with _child(*args, cwd=tmp_path) as child:
        assert _line(child).startswith("error Sidegraph: no store at ")
        assert child.wait(timeout=5) == 1
    assert set(destination.rglob("*")) == before


@_posix
def test_existing_markerless_store_reports_missing_format(tmp_path: Path) -> None:
    from tests.store_record_support import _decision

    destination = tmp_path / ".sidegraph"
    folder = destination / "decisions"
    folder.mkdir(parents=True)
    decision = _decision()
    (folder / f"{decision.id}.json").write_text(decision.model_dump_json())
    with _child("--db", str(destination)) as child:
        origin, token = _url(child)
        response = httpx.get(
            f"{origin}/api/session", headers={"X-Sidegraph-Token": token}, trust_env=False
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "store_format"
        assert not (destination / "format").exists()


@_posix
def test_unknown_host_family_is_usage_error(store_path: Path) -> None:
    with _child("--db", str(store_path), "--host-family", "unknown") as child:
        assert _line(child) == ""
        assert child.wait(timeout=5) == 2


@_posix
@pytest.mark.parametrize("abrupt", [False, True])
def test_lifetime_pipe_eof_stops_portal(store_path: Path, abrupt: bool) -> None:
    parent_code = """
import os, subprocess, sys

read_fd, write_fd = os.pipe()
child = subprocess.Popen(
    [sys.executable, "-m", "sidegraph.portal", "--no-browser", "--db", sys.argv[1],
     "--lifetime-fd", str(read_fd)],
    pass_fds=(read_fd,), stdout=subprocess.PIPE, text=True,
)
os.close(read_fd)
other = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"], close_fds=False,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(child.pid, other.pid, flush=True)
print(child.stdout.readline().rstrip(), flush=True)
sys.stdin.read()
"""
    with subprocess.Popen(
        [sys.executable, "-c", parent_code, str(store_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as parent:
        portal_pid = other_pid = None
        try:
            portal_pid, other_pid = map(int, _line(parent).split())
            origin, token = _url(parent)
            with _stream(origin, token) as stream:
                deadline = time.monotonic() + 2
                if abrupt:
                    parent.kill()
                else:
                    assert parent.stdin is not None
                    parent.stdin.close()
                parent.wait(timeout=max(0, deadline - time.monotonic()))
                assert "event: closing" in list(stream.iter_lines())
            while _running(portal_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not _running(portal_pid)
            assert time.monotonic() <= deadline
            assert _running(other_pid)
            with pytest.raises(httpx.ConnectError):
                httpx.get(origin, timeout=0.2, trust_env=False)
        finally:
            if parent.poll() is None:
                parent.kill()
            for pid in (portal_pid, other_pid):
                if pid is not None and _running(pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)


@_posix
def test_idle_timeout_sends_closing(store_path: Path) -> None:
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "sidegraph" / "portal.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"idle_minutes": 0.05}', encoding="utf-8")
    with _child("--db", str(store_path), stderr=subprocess.PIPE) as child:
        origin, token = _url(child)
        with httpx.Client(
            base_url=origin,
            headers={"Origin": origin, "X-Sidegraph-Token": token},
            timeout=6,
            trust_env=False,
        ) as client:
            ticket = client.post("/api/live-ticket").json()["ticket"]
            with client.stream("GET", "/live", params={"ticket": ticket}) as stream:
                assert stream.status_code == 200
                deadline_for_refetch = time.monotonic() + 4
                while child.poll() is None and time.monotonic() < deadline_for_refetch:
                    try:
                        assert client.get("/api/session").status_code == 200
                    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                        break
                    time.sleep(0.1)
                assert time.monotonic() < deadline_for_refetch
                assert "event: closing" in list(stream.iter_lines())
        stdout, stderr = child.communicate(timeout=2)
        assert child.returncode == 0
        assert ticket not in stdout + stderr


@_posix
def test_cli_mode_runs_until_sigint(store_path: Path) -> None:
    cwd = store_path.parent / "nested"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q", str(store_path.parent)], check=True)
    with _child(cwd=cwd) as child:
        origin, token = _url(child)
        with _stream(origin, token) as stream:
            assert child.poll() is None
            child.send_signal(signal.SIGINT)
            assert "event: closing" in list(stream.iter_lines())
        assert child.wait(timeout=5) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle inheritance")
def test_lifetime_handle_windows(store_path: Path) -> None:
    import msvcrt

    read_fd, write_fd = os.pipe()
    try:
        read_handle = msvcrt.get_osfhandle(read_fd)
        assert not os.get_inheritable(write_fd)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [read_handle]}
        os.set_handle_inheritable(read_handle, True)
        with _child(
            "--db",
            str(store_path),
            "--lifetime-fd",
            str(read_handle),
            startupinfo=startup,
            close_fds=True,
        ) as child:
            origin, _ = _url(child)
            assert httpx.get(origin, trust_env=False).status_code == 200
            os.close(write_fd)
            write_fd = -1
            assert child.wait(timeout=2) == 0
    finally:
        os.close(read_fd)
        if write_fd != -1:
            os.close(write_fd)


@_posix
def test_refuses_an_index_file_as_the_store(store_path: Path) -> None:
    with _child("--db", str(store_path / "index.db")) as child:
        assert _line(child) == "error Pass the store directory to --db."
        assert child.wait(timeout=5) == 1


@_posix
def test_refuses_port_80_before_binding(store_path: Path) -> None:
    with _child("--db", str(store_path), "--port", "80") as child:
        assert _line(child) == "error Port 80 cannot be used by the portal."
        assert child.wait(timeout=5) == 1


@_posix
def test_unrelated_legacy_filename_does_not_make_a_store(tmp_path: Path) -> None:
    destination = tmp_path / ".sidegraph"
    destination.mkdir()
    (destination / "decisions.db").write_text("unrelated text")
    with _child("--db", str(destination)) as child:
        assert _line(child).startswith("error Sidegraph: no store at ")
        assert child.wait(timeout=5) == 1
