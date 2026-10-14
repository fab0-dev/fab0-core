"""Facts read from the tracker: who is running, and the run's item. Read-only; `apply` writes (step R3).

Set FAB0_GH_FAKE=<file.json> to read the facts from a JSON file instead of `gh` (used by the tests):

    {"user": "...",
     "issues": {"owner/repo#7": {"title", "body", "labels": [...], "type", "state", "comments": [...]}},
     "pulls": {"owner/repo": [{"number", "branch", "base", "state", "body", "diff", "reviews": [...]}]}}

A review is {"id", "state", "body", "user"}; a comment is {"id", "body", "user"}.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .errors import EXIT_ERROR, Fail


class GhCli:
    def _api(self, path: str):
        run = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"gh api {path} failed: {run.stderr.strip()}")
        return json.loads(run.stdout or "null")

    def user(self) -> str:
        return self._api("user")["login"]

    def issue(self, repo: str, number: int) -> dict:
        d = self._api(f"repos/{repo}/issues/{number}")
        return {
            "number": d["number"],
            "title": d["title"],
            "body": d.get("body") or "",
            "labels": [label["name"] for label in d.get("labels") or []],
            "type": (d.get("type") or {}).get("name"),
            "state": d["state"],
        }

    def comments(self, repo: str, number: int) -> list[dict]:
        data = self._api(f"repos/{repo}/issues/{number}/comments?per_page=100")
        return [{"id": d["id"], "body": d.get("body") or "", "user": (d.get("user") or {}).get("login")} for d in data]

    def pull(self, repo: str, branch: str, base: str) -> dict | None:
        """The newest pull request from `branch` into `base`, in any state."""
        owner = repo.split("/")[0]
        data = self._api(f"repos/{repo}/pulls?state=all&head={owner}:{branch}&base={base}&per_page=100")
        if not data:
            return None
        d = max(data, key=lambda p: p["number"])
        return {
            "number": d["number"],
            "state": "merged" if d.get("merged_at") else d["state"],
            "body": d.get("body") or "",
        }

    def reviews(self, repo: str, number: int) -> list[dict]:
        data = self._api(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
        return [
            {
                "id": d["id"],
                "state": d["state"],
                "body": d.get("body") or "",
                "user": (d.get("user") or {}).get("login"),
            }
            for d in data
        ]

    def diff(self, repo: str, number: int) -> str:
        run = subprocess.run(["gh", "pr", "diff", str(number), "--repo", repo], capture_output=True, text=True)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"gh pr diff {number} --repo {repo} failed: {run.stderr.strip()}")
        return run.stdout


class GhFake:
    def __init__(self, path: str):
        self.data = json.loads(Path(path).read_text())

    def user(self) -> str:
        return self.data["user"]

    def issue(self, repo: str, number: int) -> dict:
        d = self.data.get("issues", {}).get(f"{repo}#{number}")
        if d is None:
            raise Fail(EXIT_ERROR, f"no issue {repo}#{number}")
        return {
            "number": number,
            "title": d.get("title", ""),
            "body": d.get("body", ""),
            "labels": d.get("labels", []),
            "type": d.get("type"),
            "state": d.get("state", "open"),
        }

    def comments(self, repo: str, number: int) -> list[dict]:
        return self.data.get("issues", {}).get(f"{repo}#{number}", {}).get("comments", [])

    def _pulls(self, repo: str, branch: str, base: str) -> list[dict]:
        pulls = self.data.get("pulls", {}).get(repo, [])
        return [p for p in pulls if p["branch"] == branch and p.get("base", base) == base]

    def pull(self, repo: str, branch: str, base: str) -> dict | None:
        found = self._pulls(repo, branch, base)
        if not found:
            return None
        p = max(found, key=lambda p: p["number"])
        return {"number": p["number"], "state": p.get("state", "open"), "body": p.get("body", "")}

    def _pull(self, repo: str, number: int) -> dict:
        return next(p for p in self.data.get("pulls", {}).get(repo, []) if p["number"] == number)

    def reviews(self, repo: str, number: int) -> list[dict]:
        return self._pull(repo, number).get("reviews", [])

    def diff(self, repo: str, number: int) -> str:
        return self._pull(repo, number).get("diff", "")


def make_tracker():
    fake = os.environ.get("FAB0_GH_FAKE")
    return GhFake(fake) if fake else GhCli()
