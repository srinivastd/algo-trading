"""Capture git commit/branch for paper-trade journal runs (code ↔ PnL reconstruction)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def find_repo_root(start: Path | None = None) -> Path | None:
    p = (start or Path(__file__).resolve()).resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").is_dir():
            return candidate
    return None


def _git_cmd(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=str(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def get_git_provenance(repo_root: Path | None = None) -> dict[str, Any]:
    """
    Snapshot of the working tree at paper-watch start time.

    Stored in ``runs.params_json['code_provenance']`` and indexed columns on ``runs``.
    """
    root = repo_root or find_repo_root(Path(__file__).resolve())
    if root is None:
        return {"available": False, "reason": "not_a_git_repo"}

    commit = _git_cmd(root, "rev-parse", "HEAD")
    if not commit:
        return {"available": False, "reason": "git_unavailable", "repo_root": str(root)}

    branch = _git_cmd(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    subject = _git_cmd(root, "log", "-1", "--format=%s")
    commit_date = _git_cmd(root, "log", "-1", "--format=%cI")
    dirty = bool(_git_cmd(root, "status", "--porcelain"))
    upstream = _git_cmd(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")

    return {
        "available": True,
        "commit": commit,
        "commit_short": commit[:12],
        "branch": branch,
        "upstream": upstream or None,
        "subject": subject or None,
        "commit_date": commit_date or None,
        "dirty": dirty,
        "repo_root": str(root),
    }


def infer_git_provenance_before(iso_timestamp: str, repo_root: Path | None = None) -> dict[str, Any]:
    """
    Best-effort commit for legacy runs that predate ``code_provenance`` capture.

    Uses ``git log --until=<started_at>`` — accurate when the repo history is linear.
    """
    root = repo_root or find_repo_root(Path(__file__).resolve())
    if root is None:
        return {"available": False, "reason": "not_a_git_repo", "inferred": True}

    until = iso_timestamp.replace("T", " ").split(".")[0]
    line = _git_cmd(
        root,
        "log",
        "-1",
        f"--until={until}",
        "--format=%H|%h|%s|%cI",
        "--date=iso-local",
    )
    if not line:
        return {"available": False, "reason": "no_commit_before_timestamp", "inferred": True}

    parts = line.split("|", 3)
    commit = parts[0] if parts else ""
    if not commit:
        return {"available": False, "reason": "parse_failed", "inferred": True}

    return {
        "available": True,
        "commit": commit,
        "commit_short": parts[1] if len(parts) > 1 else commit[:12],
        "subject": parts[2] if len(parts) > 2 else None,
        "commit_date": parts[3] if len(parts) > 3 else None,
        "branch": None,
        "dirty": None,
        "inferred": True,
        "repo_root": str(root),
    }
