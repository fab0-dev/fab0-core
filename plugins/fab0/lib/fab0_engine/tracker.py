"""Facts read from the tracker: who is running, the run's item and its children, board fields, pull requests.

Read-only; `apply` writes (step R3). Set FAB0_GH_FAKE=<file.json> to read the facts from a JSON file instead
of `gh` (used by the tests):

    {"user": "...",
     "issues": {"owner/repo#7": {"title", "body", "labels": [...], "type", "state", "assignees": [...],
                                 "comments": [...], "sub_issues": ["owner/repo#8"], "fields": {"Stage": "spec"}}},
     "pulls": {"owner/repo": [{"number", "branch", "base", "state", "draft", "body", "diff",
                               "reviews": [...], "comments": [...]}]}}

A review is {"id", "state", "body", "user"}; a comment is {"id", "body", "user"}.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .errors import EXIT_ERROR, Fail

FIELDS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 20) {
        nodes {
          project { number owner { ... on Organization { login } ... on User { login } } }
          fieldValues(first: 50) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
      }
    }
  }
}
"""


def _issue(d: dict, number: int) -> dict:
    return {
        "number": number,
        "title": d.get("title", ""),
        "body": d.get("body") or "",
        "labels": d.get("labels", []),
        "type": d.get("type"),
        "state": d.get("state", "open"),
        "assignees": d.get("assignees", []),
    }


class GhCli:
    def _gh(self, *args: str) -> str:
        run = subprocess.run(["gh", *args], capture_output=True, text=True)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"gh {' '.join(args[:2])} failed: {run.stderr.strip()}")
        return run.stdout

    def _api(self, path: str):
        return json.loads(self._gh("api", path) or "null")

    def user(self) -> str:
        return self._api("user")["login"]

    @staticmethod
    def _from_api(d: dict) -> dict:
        fields = {
            "title": d["title"],
            "body": d.get("body"),
            "labels": [label["name"] for label in d.get("labels") or []],
            "type": (d.get("type") or {}).get("name"),
            "state": d["state"],
            "assignees": [a["login"] for a in d.get("assignees") or []],
        }
        return _issue(fields, d["number"])

    def issue(self, repo: str, number: int) -> dict:
        return self._from_api(self._api(f"repos/{repo}/issues/{number}"))

    def sub_issues(self, repo: str, number: int) -> list[dict]:
        found = []
        for d in self._api(f"repos/{repo}/issues/{number}/sub_issues?per_page=100"):
            found.append({**self._from_api(d), "repo": d["repository_url"].split("/repos/", 1)[1]})
        return found

    def fields(self, repo: str, number: int, owner: str, project: int) -> dict:
        """The item's field values on one project board, by field name."""
        repo_owner, name = repo.split("/")
        args = ["-f", f"query={FIELDS_QUERY}", "-F", f"owner={repo_owner}", "-F", f"repo={name}"]
        data = json.loads(self._gh("api", "graphql", *args, "-F", f"number={number}"))
        for item in data["data"]["repository"]["issue"]["projectItems"]["nodes"]:
            board = item["project"]
            if board["number"] == project and (board.get("owner") or {}).get("login") == owner:
                values = {}
                for v in item["fieldValues"]["nodes"]:
                    if v and v.get("field"):
                        values[v["field"]["name"]] = next(v[k] for k in ("name", "text", "number", "date") if k in v)
                return values
        return {}

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
        state = "merged" if d.get("merged_at") else d["state"]
        return {"number": d["number"], "state": state, "draft": bool(d.get("draft")), "body": d.get("body") or ""}

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
        return self._gh("pr", "diff", str(number), "--repo", repo)


class GhFake:
    def __init__(self, path: str):
        self.data = json.loads(Path(path).read_text())

    def user(self) -> str:
        return self.data["user"]

    def _raw(self, repo: str, number: int) -> dict:
        d = self.data.get("issues", {}).get(f"{repo}#{number}")
        if d is None:
            raise Fail(EXIT_ERROR, f"no issue {repo}#{number}")
        return d

    def issue(self, repo: str, number: int) -> dict:
        return _issue(self._raw(repo, number), number)

    def sub_issues(self, repo: str, number: int) -> list[dict]:
        found = []
        for ref in self._raw(repo, number).get("sub_issues", []):
            r, n = ref.split("#")
            found.append({**self.issue(r, int(n)), "repo": r})
        return found

    def fields(self, repo: str, number: int, owner: str, project: int) -> dict:
        return self._raw(repo, number).get("fields", {})

    def comments(self, repo: str, number: int) -> list[dict]:
        issue = self.data.get("issues", {}).get(f"{repo}#{number}")
        if issue is not None:
            return issue.get("comments", [])
        pulls = [p for p in self.data.get("pulls", {}).get(repo, []) if p["number"] == number]
        return pulls[0].get("comments", []) if pulls else []

    def pull(self, repo: str, branch: str, base: str) -> dict | None:
        pulls = self.data.get("pulls", {}).get(repo, [])
        found = [p for p in pulls if p["branch"] == branch and p.get("base", base) == base]
        if not found:
            return None
        p = max(found, key=lambda p: p["number"])
        return {
            "number": p["number"],
            "state": p.get("state", "open"),
            "draft": p.get("draft", False),
            "body": p.get("body", ""),
        }

    def _pull(self, repo: str, number: int) -> dict:
        return next(p for p in self.data.get("pulls", {}).get(repo, []) if p["number"] == number)

    def reviews(self, repo: str, number: int) -> list[dict]:
        return self._pull(repo, number).get("reviews", [])

    def diff(self, repo: str, number: int) -> str:
        return self._pull(repo, number).get("diff", "")


def make_tracker():
    fake = os.environ.get("FAB0_GH_FAKE")
    return GhFake(fake) if fake else GhCli()
