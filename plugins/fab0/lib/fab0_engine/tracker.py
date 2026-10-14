"""Facts read from the tracker: who is running, the run's item and its children, board fields, pull requests.

Reads serve the engine; writes serve only `apply`. Set FAB0_GH_FAKE=<file.json> to read and write the facts
in a JSON file instead of GitHub (used by the tests):

    {"user": "...",
     "issues": {"owner/repo#7": {"title", "body", "labels": [...], "type", "state", "state_reason",
                                 "assignees": [...], "comments": [...], "sub_issues": ["owner/repo#8"],
                                 "blocked_by": ["owner/repo#6"], "fields": {"Stage": "spec"}}},
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
        "state_reason": d.get("state_reason"),
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
            "state_reason": d.get("state_reason"),
            "assignees": [a["login"] for a in d.get("assignees") or []],
        }
        return _issue(fields, d["number"])

    @staticmethod
    def _repo_of(d: dict, default: str) -> str:
        url = d.get("repository_url") or ""
        return url.split("/repos/", 1)[1] if "/repos/" in url else default

    def issue(self, repo: str, number: int) -> dict:
        return self._from_api(self._api(f"repos/{repo}/issues/{number}"))

    def sub_issues(self, repo: str, number: int) -> list[dict]:
        found = []
        for d in self._api(f"repos/{repo}/issues/{number}/sub_issues?per_page=100"):
            found.append({**self._from_api(d), "repo": self._repo_of(d, repo)})
        return found

    def blocked_by(self, repo: str, number: int) -> list[dict]:
        """The items this one is blocked by, in any repository and any state."""
        data = self._api(f"repos/{repo}/issues/{number}/dependencies/blocked_by?per_page=100")
        return [{**self._from_api(d), "repo": self._repo_of(d, repo)} for d in data]

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

    def checks(self, repo: str, number: int) -> tuple[str, list[str]]:
        """`pass` (or no checks), `pending` or `fail` for a pull request's required checks, and the failing ones."""
        args = ["gh", "pr", "checks", str(number), "--repo", repo, "--required", "--json", "bucket,name"]
        run = subprocess.run(args, capture_output=True, text=True)
        if run.returncode not in (0, 1, 8) or not run.stdout.strip():
            return "pass", []  # no required checks
        found = json.loads(run.stdout)
        failed = [c["name"] for c in found if c["bucket"] in ("fail", "cancel")]
        if failed:
            return "fail", failed
        return ("pending" if any(c["bucket"] == "pending" for c in found) else "pass"), []

    # ------------------------------------------------------------ writes, for apply

    def create_issue(self, repo: str, title: str, body: str, issue_type: str | None) -> dict:
        args = ["api", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        if issue_type:
            args += ["-f", f"type={issue_type}"]
        d = json.loads(self._gh(*args))
        return {"number": d["number"], "id": d["id"]}

    def add_sub_issue(self, repo: str, parent: int, child: dict) -> None:
        self._gh("api", f"repos/{repo}/issues/{parent}/sub_issues", "-F", f"sub_issue_id={child['id']}")

    def issue_id(self, repo: str, number: int) -> int:
        return self._api(f"repos/{repo}/issues/{number}")["id"]

    def add_blocked_by(self, repo: str, number: int, blocker_repo: str, blocker: int) -> None:
        issue_id = self.issue_id(blocker_repo, blocker)
        self._gh("api", f"repos/{repo}/issues/{number}/dependencies/blocked_by", "-F", f"issue_id={issue_id}")

    def edit_labels(self, repo: str, number: int, add: list, remove: list) -> None:
        args = ["issue", "edit", str(number), "--repo", repo]
        for label in add:
            args += ["--add-label", label]
        for label in remove:
            args += ["--remove-label", label]
        self._gh(*args)

    def assign(self, repo: str, number: int, users: list) -> None:
        self._gh("issue", "edit", str(number), "--repo", repo, "--add-assignee", ",".join(users))

    def comment(self, repo: str, number: int, body: str) -> None:
        self._gh("issue", "comment", str(number), "--repo", repo, "--body", body)

    def close(self, repo: str, number: int) -> None:
        self._gh("issue", "close", str(number), "--repo", repo)

    def set_field(self, repo: str, number: int, owner: str, project: int, field: str, value) -> None:
        url = f"https://github.com/{repo}/issues/{number}"
        item = json.loads(
            self._gh("project", "item-add", str(project), "--owner", owner, "--url", url, "--format", "json")
        )["id"]
        board = json.loads(self._gh("project", "view", str(project), "--owner", owner, "--format", "json"))["id"]
        fields = json.loads(self._gh("project", "field-list", str(project), "--owner", owner, "--format", "json"))
        found = next((f for f in fields["fields"] if f["name"] == field), None)
        if found is None:
            raise Fail(EXIT_ERROR, f"no field {field} on project {owner}/{project}")
        args = ["project", "item-edit", "--id", item, "--project-id", board, "--field-id", found["id"]]
        if "options" in found:
            option = next((o for o in found["options"] if o["name"] == value), None)
            if option is None:
                raise Fail(EXIT_ERROR, f"field {field} has no option {value!r}")
            args += ["--single-select-option-id", option["id"]]
        elif isinstance(value, (int, float)):
            args += ["--number", str(value)]
        else:
            args += ["--text", str(value)]
        self._gh(*args)

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> int:
        args = ["pr", "create", "--repo", repo, "--head", head, "--base", base, "--title", title, "--body", body]
        url = self._gh(*args, *(["--draft"] if draft else [])).strip().splitlines()[-1]
        return int(url.rsplit("/", 1)[1])

    def ready_pr(self, repo: str, number: int) -> None:
        self._gh("pr", "ready", str(number), "--repo", repo)


class GhFake:
    def __init__(self, path: str):
        self.path = path
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

    def blocked_by(self, repo: str, number: int) -> list[dict]:
        found = []
        for ref in self._raw(repo, number).get("blocked_by", []):
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

    def checks(self, repo: str, number: int) -> tuple[str, list[str]]:
        p = self._pull(repo, number)
        return p.get("checks", "pass"), p.get("failed_checks", [])

    # ------------------------------------------------------------ writes, for apply

    def save(self) -> None:
        Path(self.path).write_text(json.dumps(self.data, indent=2))

    def create_issue(self, repo: str, title: str, body: str, issue_type: str | None) -> dict:
        issues = self.data.setdefault("issues", {})
        number = 1 + max((int(r.split("#")[1]) for r in issues if r.startswith(f"{repo}#")), default=0)
        issues[f"{repo}#{number}"] = {"title": title, "body": body, "type": issue_type, "state": "open"}
        self.save()
        return {"number": number, "id": number}

    def add_sub_issue(self, repo: str, parent: int, child: dict) -> None:
        self._raw(repo, parent).setdefault("sub_issues", []).append(f"{repo}#{child['number']}")
        self.save()

    def issue_id(self, repo: str, number: int) -> int:
        return number

    def add_blocked_by(self, repo: str, number: int, blocker_repo: str, blocker: int) -> None:
        blocked_by = self._raw(repo, number).setdefault("blocked_by", [])
        if f"{blocker_repo}#{blocker}" not in blocked_by:
            blocked_by.append(f"{blocker_repo}#{blocker}")
        self.save()

    def edit_labels(self, repo: str, number: int, add: list, remove: list) -> None:
        d = self._raw(repo, number)
        d["labels"] = [x for x in d.get("labels", []) if x not in remove] + [
            x for x in add if x not in d.get("labels", [])
        ]
        self.save()

    def assign(self, repo: str, number: int, users: list) -> None:
        d = self._raw(repo, number)
        d["assignees"] = d.get("assignees", []) + [u for u in users if u not in d.get("assignees", [])]
        self.save()

    def comment(self, repo: str, number: int, body: str) -> None:
        target = self.data.get("issues", {}).get(f"{repo}#{number}")
        if target is None:
            target = self._pull(repo, number)
        comments = target.setdefault("comments", [])
        comments.append({"id": len(comments) + 1, "body": body, "user": self.data["user"]})
        self.save()

    def close(self, repo: str, number: int) -> None:
        self._raw(repo, number)["state"] = "closed"
        self.save()

    def set_field(self, repo: str, number: int, owner: str, project: int, field: str, value) -> None:
        self._raw(repo, number).setdefault("fields", {})[field] = value
        self.save()

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> int:
        pulls = self.data.setdefault("pulls", {}).setdefault(repo, [])
        number = 1 + max((p["number"] for repo_pulls in self.data["pulls"].values() for p in repo_pulls), default=0)
        pulls.append(
            {
                "number": number,
                "branch": head,
                "base": base,
                "title": title,
                "body": body,
                "draft": draft,
                "state": "open",
            }
        )
        self.save()
        return number

    def ready_pr(self, repo: str, number: int) -> None:
        self._pull(repo, number)["draft"] = False
        self.save()

    def note_merged(self, repo: str, head: str, base: str) -> None:
        """GitHub marks a pull request merged when its head reaches its base; the fake is told."""
        for p in self.data.get("pulls", {}).get(repo, []):
            if p["branch"] == head and p.get("base", base) == base and p.get("state", "open") == "open":
                p["state"] = "merged"
        self.save()


def make_tracker():
    fake = os.environ.get("FAB0_GH_FAKE")
    return GhFake(fake) if fake else GhCli()
