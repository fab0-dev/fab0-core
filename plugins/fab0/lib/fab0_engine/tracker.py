"""Facts read from the tracker: who is running, and the run's item. Read-only; `apply` writes (step R3).

Set FAB0_GH_FAKE=<file.json> to read the facts from a JSON file instead of `gh` (used by the tests):
{"user": "...", "issues": {"owner/repo#7": {"title", "body", "labels": [...], "type", "state"}}}.
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


def make_tracker():
    fake = os.environ.get("FAB0_GH_FAKE")
    return GhFake(fake) if fake else GhCli()
