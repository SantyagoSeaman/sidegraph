#!/usr/bin/env python3
"""CI coverage probe: has a green full test matrix already tested this git tree?

Run by the `prior` job of `.github/workflows/ci.yml` on a push and on the `workflow_call`
from `publish.yml`. It answers one of three modes on `$GITHUB_OUTPUT`:

  full     run the whole matrix (the default, and the answer to every failure of the lookup)
  covered  this commit's tree is the tree a green full run already tested: skip the matrix
  light    a commit that changes only the plugin lock files on top of a covered tree

and the url of the run that covered the tree as `covered_by`. One line of explanation goes to
stdout. The script can only remove a skip, never add one: every error (an HTTP error, a
timeout, a malformed answer, an answer over its size cap, a missing variable) is `full`, and it
exits 0 whenever it can catch the error.

A full run that passed uploads an artifact `full-ci-tree-<tree>` holding a small JSON file
(`tree`, `sha`, `run_id`, `event`, `ref`). A tree is covered when such an artifact passes every
check below. The checks exist against accidents, not against a writer who can already run
arbitrary code in the repository: a draft run, a run that tested another tree, a branch with a
modified workflow, a run still in progress, a fork.

  1. the artifact has not expired (the literal `false`);
  2. its run came from this repository, with this repository as the head repository;
  3. the run is not the current one;
  4. the run is a completed, successful `ci.yml` run of a push or a pull request;
  5. the run's jobs, all of them (the answer's `total_count` equals the jobs received), include
     a successful `full-ci` (a draft run names it differently; every job of that name must have
     succeeded) and, for each of `test (`, `sdk-compatibility (`, `windows-core (`, at least one
     job, all successful;
  6. the evidence JSON names this tree, and its `sha` is bound to the run: the run's head commit
     for a push, or a commit whose parents include the run's head commit for a pull request;
  7. the commit `sha` has this tree.

Every commit or tree id read from the API or the evidence is a 40-character lowercase hex string
before it is compared or put into a URL, and every numeric id is a positive JSON integer.

A standalone stdlib script. It ships in the public snapshot, because the public repository's
workflow runs it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO

# A fetch takes the request and a byte cap; it raises on any HTTP or network error and on an
# answer over the cap.
Fetch = Callable[[urllib.request.Request, int], bytes]

JSON_CAP = 2 * 1024 * 1024  # API answers: a merge commit's file list alone runs to ~1 MB
ZIP_CAP = 64 * 1024  # the evidence artifact: a few hundred bytes in practice
MEMBER_CAP = 4096  # the evidence JSON, after decompression
TIMEOUT = 10  # seconds, per request
MODES = ("full", "covered", "light")

DEFAULT_API = "https://api.github.com"
REQUIRED = (
    "GITHUB_REPOSITORY",
    "GITHUB_REPOSITORY_ID",
    "GITHUB_RUN_ID",
    "GITHUB_SHA",
    "GITHUB_REF",
    "GITHUB_EVENT_NAME",
    "GITHUB_TOKEN",
)
CI_WORKFLOW = ".github/workflows/ci.yml"
HEAVY_FAMILIES = ("test (", "sdk-compatibility (", "windows-core (")
PLUGIN_FILES = frozenset({"plugin/sidegraph/pyproject.toml", "plugin/sidegraph/uv.lock"})
LIGHT_STATUSES = ("added", "modified")


_SHA = re.compile(r"[0-9a-f]{40}")
_DIGITS = re.compile(r"[0-9]+")


def _sha(value: object) -> str:
    """A git object id: 40 lowercase hex characters, or ValueError."""
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"not a git object id ({type(value).__name__})")
    return value


def _api_id(value: object) -> int:
    """A numeric id out of API JSON: a positive integer, not a string or a boolean."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"not a positive integer id ({type(value).__name__})")
    return value


def _env_id(text: str) -> int:
    """A numeric id out of the environment: decimal digits, positive, or ValueError."""
    if not _DIGITS.fullmatch(text) or int(text) <= 0:
        raise ValueError("an environment id is not a positive integer")
    return int(text)


def _tree(commit: Any) -> str:
    return _sha(commit["tree"]["sha"])


def _parents(commit: Any) -> list[str]:
    parents = commit["parents"]
    if not isinstance(parents, list):
        raise ValueError("a commit's parents are not a list")
    return [_sha(p["sha"]) for p in parents]


@dataclass(frozen=True)
class Decision:
    """The probe's answer: `mode` is one of MODES, `covered_by` a run url or ''."""

    mode: str
    covered_by: str
    reason: str


def api_request(url: str, token: str) -> urllib.request.Request:
    """A GitHub API request. The token is an unredirected header: the artifact download
    answers with a redirect to blob storage, and `urllib` copies only the ordinary headers
    onto the redirected request, so the token never leaves api.github.com.
    """
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    return req


def _evidence(data: bytes) -> dict[str, Any]:
    """The evidence JSON out of the artifact zip: one `.json` member, read to MEMBER_CAP."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if len(names) != 1 or not names[0].endswith(".json"):
            raise ValueError("the evidence zip must hold exactly one .json member")
        with zf.open(names[0]) as member:
            raw = member.read(MEMBER_CAP + 1)
    if len(raw) > MEMBER_CAP:
        raise ValueError("the evidence JSON is over its size cap")
    evidence = json.loads(raw)
    if not isinstance(evidence, dict):
        raise ValueError("the evidence JSON is not an object")
    return evidence


class _Lookup:
    """The API reads for one run, over an injected `fetch`."""

    def __init__(self, env: Mapping[str, str], fetch: Fetch) -> None:
        self.fetch = fetch
        self.api = (env.get("GITHUB_API_URL") or DEFAULT_API).rstrip("/")
        self.repo = env["GITHUB_REPOSITORY"]
        self.repo_id = _env_id(env["GITHUB_REPOSITORY_ID"])
        self.run_id = _env_id(env["GITHUB_RUN_ID"])
        self.sha = _sha(env["GITHUB_SHA"])
        self.ref = env["GITHUB_REF"]
        self.token = env["GITHUB_TOKEN"]

    def url(self, path: str) -> str:
        return f"{self.api}/repos/{self.repo}/{path}"

    def get_json(self, url: str) -> Any:
        return json.loads(self.fetch(api_request(url, self.token), JSON_CAP))

    def commit(self, sha: str) -> Any:
        return self.get_json(self.url(f"git/commits/{_sha(sha)}"))

    def covered_by(self, tree: str) -> str | None:
        """The url of a run whose evidence covers `tree`, or None. First page, 100 artifacts."""
        query = urllib.parse.urlencode({"name": f"full-ci-tree-{tree}", "per_page": 100})
        listing = self.get_json(self.url(f"actions/artifacts?{query}"))
        for artifact in listing["artifacts"]:
            try:
                run_url = self._passes(artifact, tree)
            except Exception:  # noqa: BLE001 — one unreadable artifact must not hide the next
                continue
            if run_url:
                return run_url
        return None

    def _passes(self, artifact: Mapping[str, Any], tree: str) -> str | None:
        """Rules 1-7 for one artifact; the covering run's url, or None."""
        # 1. not expired
        if artifact["expired"] is not False:
            return None
        # 2. this repository, and not a fork's head
        origin = artifact["workflow_run"]
        if _api_id(origin["head_repository_id"]) != self.repo_id:
            return None
        if _api_id(origin["repository_id"]) != self.repo_id:
            return None
        # 3. not the current run
        run_id = _api_id(origin["id"])
        if run_id == self.run_id:
            return None
        # 4. a completed, successful ci.yml run of a push or a pull request
        run = self.get_json(self.url(f"actions/runs/{run_id}"))
        if run["path"] != CI_WORKFLOW:
            return None
        if run["status"] != "completed" or run["conclusion"] != "success":
            return None
        if run["event"] not in ("push", "pull_request"):
            return None
        # 5. the gate and every heavy family passed
        answer = self.get_json(self.url(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100"))
        jobs = answer["jobs"]
        # Only the first page is read: a run with more jobs than came back is not fully seen.
        if not isinstance(jobs, list) or type(answer["total_count"]) is not int:
            return None
        if answer["total_count"] != len(jobs):
            return None
        gates = [j for j in jobs if j["name"] == "full-ci"]
        if not gates or any(j["conclusion"] != "success" for j in gates):
            return None
        for prefix in HEAVY_FAMILIES:
            family = [j for j in jobs if j["name"].startswith(prefix)]
            if not family or any(j["conclusion"] != "success" for j in family):
                return None
        # 6. the evidence names this tree and a commit bound to the run
        data = self.fetch(api_request(artifact["archive_download_url"], self.token), ZIP_CAP)
        evidence = _evidence(data)
        if _sha(evidence["tree"]) != tree:
            return None
        sha = _sha(evidence["sha"])
        head_sha = _sha(run["head_sha"])
        commit = self.commit(sha)
        if run["event"] == "push":
            if sha != head_sha:
                return None
        elif head_sha not in _parents(commit):
            return None
        # 7. that commit has this tree
        if _tree(commit) != tree:
            return None
        html_url = run["html_url"]
        if not isinstance(html_url, str) or not html_url.startswith("https://"):
            return None
        return html_url


def _plugin_lock_change(files: list[Mapping[str, Any]]) -> bool:
    """Exactly the two plugin lock files, each added or modified."""
    return (
        len(files) == len(PLUGIN_FILES)
        and {f["filename"] for f in files} == PLUGIN_FILES
        and all(f["status"] in LIGHT_STATUSES for f in files)
    )


def _decide(env: Mapping[str, str], fetch: Fetch) -> Decision:
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        return Decision("full", "", f"missing environment: {', '.join(missing)}")
    if env["GITHUB_EVENT_NAME"] == "pull_request":
        return Decision("full", "", "pull request: the full matrix runs")
    look = _Lookup(env, fetch)
    own = look.commit(look.sha)
    tree = _tree(own)
    run_url = look.covered_by(tree)
    if run_url:
        return Decision(
            "covered", run_url, f"tree {tree} already passed the full matrix: {run_url}"
        )
    parents = _parents(own)
    on_branch = look.ref == "refs/heads/main" or look.ref.startswith("refs/heads/release/")
    if on_branch and len(parents) == 1:
        files = look.get_json(look.url(f"commits/{look.sha}?per_page=3"))["files"]
        if _plugin_lock_change(files):
            parent_tree = _tree(look.commit(parents[0]))
            run_url = look.covered_by(parent_tree)
            if run_url:
                return Decision(
                    "light",
                    run_url,
                    f"only the plugin lock files changed on tree {parent_tree}: {run_url}",
                )
    return Decision("full", "", f"tree {tree} has no passing full run")


def decide(env: Mapping[str, str], fetch: Fetch) -> Decision:
    """Decide the mode for the run described by `env`. Never raises: any error is `full`."""
    try:
        return _decide(env, fetch)
    except Exception as exc:  # noqa: BLE001 — every failure of the lookup means "run it all"
        return Decision("full", "", f"coverage lookup failed ({type(exc).__name__}: {exc})")


def _urlopen_fetch(req: urllib.request.Request, cap: int) -> bytes:
    """The real `Fetch`: one request, 10 s, at most `cap` bytes."""
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data: bytes = resp.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"answer is over the {cap}-byte cap")
    return data


def _one_line(text: str) -> str:
    """Collapse whitespace so a value cannot start a new output or workflow-command line."""
    return " ".join(text.split())


def _say(text: str, stream: TextIO) -> None:
    """Print one line; a closed or broken stream must not turn the probe into a failure."""
    with contextlib.suppress(OSError):
        print(text, file=stream)


def main() -> int:
    """Probe, then append `mode` and `covered_by` to `$GITHUB_OUTPUT`; always exits 0.

    Both outputs go out in one `write`, so a failed write leaves no `mode=covered` behind
    without its `covered_by`; with no output written the workflow's own default is `full`.
    """
    decision = decide(os.environ, _urlopen_fetch)
    mode, covered_by = decision.mode, decision.covered_by
    if mode not in MODES or covered_by != _one_line(covered_by):
        mode, covered_by = "full", ""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        try:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"mode={mode}\ncovered_by={covered_by}\n")
        except OSError as exc:
            _say(f"could not write GITHUB_OUTPUT ({type(exc).__name__})", sys.stderr)
    _say(_one_line(decision.reason), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
