"""Facts read from the tracker: who is running, the run's item and its children, board fields, pull requests.

Reads serve the engine; writes serve only `apply`. Two layers:

- a backend makes one request to GitHub per call: `GhCli` through `gh`, or `GhFake` on a JSON file. Its reads ask
  for related things together (an item with its sub-issues, relations and board fields; a pull request with its
  reviews and comments), and its writes make only write calls. Board entries (`projectItems`, which GitHub gives
  only to a login with the `project` scope) are asked for only when the project has a board;
- `Memo`, what `make_tracker` returns, is what the engine calls. It answers every read once per evaluation of a
  run (from the answer of a request that held it, when there is one), is safe to call from several threads, and
  lets at most `MAX_IN_FLIGHT` requests run at once, reads and writes together. Nothing it keeps outlives the
  command (P3): `forget` drops it between the rounds of `apply`, and writes never update it.

Set FAB0_GH_FAKE=<file.json> to read and write the facts in a JSON file instead of GitHub (used by the tests):

    {"user": "...",
     "delay": 0.3,
     "issues": {"owner/repo#7": {"title", "body", "labels": [...], "type", "state", "state_reason",
                                 "assignees": [...], "comments": [...], "sub_issues": ["owner/repo#8"],
                                 "blocked_by": ["owner/repo#6"], "fields": {"Stage": "spec"}}},
     "pulls": {"owner/repo": [{"number", "branch", "base", "state", "draft", "body", "diff", "head",
                               "merged_by", "author", "reviews": [...], "comments": [...]}]},
     "boards": {"owner/7": {"fields": {"Stage": {"options": ["spec", "build"]}, "Note": {}}}}}

A pull request's `merged_by` is who merged it, and `author` the login of its head commit's author. A review is
{"id", "state", "body", "user", "at"}; a comment is {"id", "body", "user", "at"}; `at` is optional
(a time, ISO 8601) and orders a pull request's reviews and conversation comments. An issue is on the board when
it has `fields`. Without `boards` every field exists; with it, a field or option that isn't there is refused.

Every call of the fake stands for one request to GitHub. It appends one JSON line per call to `<file>.calls`:
{"method", "args", "start", "end"} (times in seconds since the epoch), and waits `delay` seconds first when the file
has that key, so a test can count and overlap what GitHub would see. `WRITES` names the methods that write.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .errors import EXIT_ERROR, Fail

MAX_IN_FLIGHT = 4  # requests at once: clear of GitHub's secondary rate limits
WRITES = (
    "create_issue",
    "add_sub_issue",
    "add_blocked_by",
    "edit_labels",
    "assign",
    "comment",
    "close",
    "add_to_board",
    "set_field",
    "create_pr",
    "ready_pr",
)

FACTS = """
fragment Facts on Issue {
  databaseId number title body state stateReason
  issueType { name }
  repository { nameWithOwner }
  labels(first: 100) { nodes { name } }
  assignees(first: 100) { nodes { login } }
}
fragment Relation on Issue {
  databaseId number title state stateReason
  repository { nameWithOwner }
}
"""

# The board entries of an item. Only a board needs them, and GitHub refuses `projectItems` to a login without the
# `project` scope, so a query asks for them only when a board field is read.
ENTRY = """
fragment Entry on ProjectV2Item {
  id
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
"""


def item_query(boards: bool) -> str:
    """An item with its relations, and its sub-issues with the same; with board entries when `boards`. Every
    fragment is used, as GitHub requires."""
    entries = "\n      projectItems(first: 20) { nodes { ...Entry } }" if boards else ""
    return (
        """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      ...Facts
      blockedBy(first: 100) { nodes { ...Relation } }"""
        + entries
        + """
      subIssues(first: 100) {
        nodes {
          ...Facts
          blockedBy(first: 100) { nodes { ...Relation } }"""
        + entries.replace("\n      ", "\n          ")
        + """
        }
      }
    }
  }
}
"""
        + FACTS
        + (ENTRY if boards else "")
    )


# the pull requests of a branch with their reviews and conversation comments
PULLS_QUERY = """
query($owner: String!, $repo: String!, $head: String!, $base: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequests(headRefName: $head, baseRefName: $base, states: [OPEN, CLOSED, MERGED], first: 100) {
      nodes {
        number state isDraft body headRefOid
        headRepository { nameWithOwner }
        mergedBy { login __typename }
        commits(last: 1) { nodes { commit { author { user { login } } } } }
        reviews(first: 100) { nodes { databaseId state body submittedAt author { login __typename } } }
        comments(first: 100) { nodes { databaseId body createdAt author { login __typename } } }
      }
    }
  }
}
"""

# a board's id, its fields and their options
BOARD_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on Organization { projectV2(number: $number) { ...Board } }
    ... on User { projectV2(number: $number) { ...Board } }
  }
}
fragment Board on ProjectV2 {
  id
  fields(first: 100) {
    nodes {
      ... on ProjectV2FieldCommon { id name }
      ... on ProjectV2SingleSelectField { id name options { id name } }
    }
  }
}
"""


def _issue(d: dict, number: int) -> dict:
    return {
        "number": number,
        "id": d.get("id", number),
        "title": d.get("title", ""),
        "body": d.get("body") or "",
        "labels": d.get("labels", []),
        "type": d.get("type"),
        "state": d.get("state", "open"),
        "state_reason": d.get("state_reason"),
        "assignees": d.get("assignees", []),
    }


def _login(author: dict | None) -> str | None:
    """A GraphQL author's login as REST gives it: a bot's ends in `[bot]`."""
    if not author:
        return None
    login = author.get("login")
    return f"{login}[bot]" if login and author.get("__typename") == "Bot" and not login.endswith("[bot]") else login


def _nodes(connection: dict | None) -> list:
    return [n for n in (connection or {}).get("nodes") or [] if n]


class GhCli:
    def _gh(self, *args: str) -> str:
        run = subprocess.run(["gh", *args], capture_output=True, text=True)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"gh {' '.join(args[:2])} failed: {run.stderr.strip()}")
        return run.stdout

    def _api(self, path: str):
        return json.loads(self._gh("api", path) or "null")

    def _graphql(self, query: str, **variables):
        args = []
        for name, value in variables.items():
            args += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
        data = json.loads(self._gh("api", "graphql", "-f", f"query={query}", *args))
        if data.get("errors") and not data.get("data"):
            raise Fail(EXIT_ERROR, f"gh api graphql failed: {data['errors'][0].get('message')}")
        return data["data"]

    # ------------------------------------------------------------ reads

    def user(self) -> str:
        return self._api("user")["login"]

    @staticmethod
    def _from_node(n: dict) -> dict:
        """An issue as GraphQL gives it: its facts, and the repository it is in."""
        fields = {
            "id": n.get("databaseId"),
            "title": n["title"],
            "body": n.get("body"),
            "labels": [label["name"] for label in _nodes(n.get("labels"))],
            "type": (n.get("issueType") or {}).get("name"),
            "state": n["state"].lower(),
            "state_reason": (n.get("stateReason") or "").lower() or None,
            "assignees": [a["login"] for a in _nodes(n.get("assignees"))],
        }
        return {**_issue(fields, n["number"]), "repo": (n.get("repository") or {}).get("nameWithOwner")}

    @staticmethod
    def _entries(nodes: list) -> dict:
        """The item's place on each board, by `owner/number`: its id there, and its field values by name."""
        boards = {}
        for entry in nodes:
            board = entry["project"]
            values = {}
            for v in _nodes(entry["fieldValues"]):
                if v.get("field"):
                    values[v["field"]["name"]] = next(v[k] for k in ("name", "text", "number", "date") if k in v)
            boards[f"{(board.get('owner') or {}).get('login')}/{board['number']}"] = {
                "item": entry["id"],
                "fields": values,
            }
        return boards

    def item(self, repo: str, number: int, boards: bool = False) -> dict:
        """An item, its blocked-by items and its sub-issues with theirs: one request. With `boards`, each part
        also has its board entries, which only a login with the `project` scope can read."""
        owner, name = repo.split("/")
        data = self._graphql(item_query(boards), owner=owner, repo=name, number=number)
        node = (data.get("repository") or {}).get("issue")
        if node is None:
            raise Fail(EXIT_ERROR, f"no issue {repo}#{number}")

        def part(n: dict) -> dict:
            found = {
                "issue": self._from_node(n),
                "blocked_by": [self._from_node(b) for b in _nodes(n.get("blockedBy"))],
            }
            if boards:
                found["boards"] = self._entries(_nodes(n.get("projectItems")))
            return found

        found = part(node)
        found["issue"].pop("repo")
        return {**found, "sub_issues": [part(n) for n in _nodes(node.get("subIssues"))]}

    def open_items(self, repo: str, label: str | None, issue_type: str | None) -> list[int]:
        """The numbers of the open issues with this label and of this type, newest first."""
        args = ["issue", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", "number"]
        if label:
            args += ["--label", label]
        if issue_type:
            args += ["--search", f'type:"{issue_type}"']
        return [d["number"] for d in json.loads(self._gh(*args) or "[]")]

    def board(self, owner: str, project: int) -> dict:
        """A board's id and its fields, each with its id and, for a single-select, its options' ids by name."""
        data = self._graphql(BOARD_QUERY, owner=owner, number=project)
        node = ((data.get("repositoryOwner") or {}) or {}).get("projectV2")
        if node is None:
            raise Fail(EXIT_ERROR, f"no project {owner}/{project}")
        fields = {}
        for f in _nodes(node["fields"]):
            options = {o["name"]: o["id"] for o in f["options"]} if "options" in f else None
            fields[f["name"]] = {"id": f["id"], "options": options}
        return {"id": node["id"], "fields": fields}

    def comments(self, repo: str, number: int) -> list[dict]:
        data = self._api(f"repos/{repo}/issues/{number}/comments?per_page=100")
        return [
            {
                "id": d["id"],
                "body": d.get("body") or "",
                "user": (d.get("user") or {}).get("login"),
                "at": d.get("created_at"),
            }
            for d in data
        ]

    def pulls(self, repo: str, branch: str, base: str) -> list[dict]:
        """Every pull request from `branch` into `base`, in any state, oldest first, with its reviews and
        conversation comments: one request."""
        owner, name = repo.split("/")
        data = self._graphql(PULLS_QUERY, owner=owner, repo=name, head=branch, base=base)
        found = []
        for d in _nodes((data.get("repository") or {}).get("pullRequests")):
            if ((d.get("headRepository") or {}).get("nameWithOwner") or "").lower() != repo.lower():
                continue  # from a fork
            found.append(
                {
                    "number": d["number"],
                    "state": d["state"].lower(),
                    "draft": bool(d.get("isDraft")),
                    "head": d.get("headRefOid"),
                    "merged_by": _login(d.get("mergedBy")),
                    "author": next(
                        (
                            ((c["commit"].get("author") or {}).get("user") or {}).get("login")
                            for c in _nodes(d.get("commits"))
                        ),
                        None,
                    ),
                    "body": d.get("body") or "",
                    "reviews": [
                        {
                            "id": r["databaseId"],
                            "state": r["state"],
                            "body": r.get("body") or "",
                            "user": _login(r.get("author")),
                            "at": r.get("submittedAt"),
                        }
                        for r in _nodes(d.get("reviews"))
                    ],
                    "comments": [
                        {
                            "id": c["databaseId"],
                            "body": c.get("body") or "",
                            "user": _login(c.get("author")),
                            "at": c.get("createdAt"),
                        }
                        for c in _nodes(d.get("comments"))
                    ],
                }
            )
        return sorted(found, key=lambda p: p["number"])

    def reviews(self, repo: str, number: int) -> list[dict]:
        data = self._api(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
        return [
            {
                "id": d["id"],
                "state": d["state"],
                "body": d.get("body") or "",
                "user": (d.get("user") or {}).get("login"),
                "at": d.get("submitted_at"),
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

    # ------------------------------------------------------------ writes, for apply: write calls only

    def create_issue(self, repo: str, title: str, body: str, issue_type: str | None) -> dict:
        args = ["api", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        if issue_type:
            args += ["-f", f"type={issue_type}"]
        d = json.loads(self._gh(*args))
        return {"number": d["number"], "id": d["id"]}

    def add_sub_issue(self, repo: str, parent: int, child: dict) -> None:
        self._gh("api", f"repos/{repo}/issues/{parent}/sub_issues", "-F", f"sub_issue_id={child['id']}")

    def add_blocked_by(self, repo: str, number: int, blocker_repo: str, blocker: int, blocker_id: int) -> None:
        self._gh("api", f"repos/{repo}/issues/{number}/dependencies/blocked_by", "-F", f"issue_id={blocker_id}")

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

    def plan_field(self, board: dict, owner: str, project: int, field: str, value) -> list[str]:
        """The arguments that set `field` to `value`, from the board's fields: no call. Refuses what isn't there."""
        found = board["fields"].get(field)
        if found is None:
            raise Fail(EXIT_ERROR, f"no field {field} on project {owner}/{project}")
        args = ["--field-id", found["id"]]
        if found["options"] is not None:
            option = found["options"].get(value)
            if option is None:
                raise Fail(EXIT_ERROR, f"field {field} on project {owner}/{project} has no option {value!r}")
            return args + ["--single-select-option-id", option]
        if isinstance(value, (int, float)):
            return args + ["--number", str(value)]
        return args + ["--text", str(value)]

    def add_to_board(self, repo: str, number: int, owner: str, project: int) -> str:
        url = f"https://github.com/{repo}/issues/{number}"
        added = self._gh("project", "item-add", str(project), "--owner", owner, "--url", url, "--format", "json")
        return json.loads(added)["id"]

    def set_field(self, board: dict, item: str, plan: list[str], value) -> None:
        self._gh("project", "item-edit", "--id", item, "--project-id", board["id"], *plan)

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> int:
        args = ["pr", "create", "--repo", repo, "--head", head, "--base", base, "--title", title, "--body", body]
        url = self._gh(*args, *(["--draft"] if draft else [])).strip().splitlines()[-1]
        return int(url.rsplit("/", 1)[1])

    def ready_pr(self, repo: str, number: int) -> None:
        self._gh("pr", "ready", str(number), "--repo", repo)


def _logged(method):
    """A call of the fake is one request: it waits `delay`, and is logged with the time it took."""

    @functools.wraps(method)
    def wrapper(self, *args):
        start = time.time()
        try:
            if self.delay:
                time.sleep(self.delay)
            return method(self, *args)
        finally:
            line = {"method": method.__name__, "args": list(args), "start": start, "end": time.time()}
            with self._lock:
                with open(f"{self.path}.calls", "a") as log:
                    log.write(json.dumps(line, default=str) + "\n")

    return wrapper


class GhFake:
    def __init__(self, path: str):
        self.path = path
        self.data = json.loads(Path(path).read_text())
        self.delay = float(self.data.get("delay", 0))
        self._lock = threading.RLock()

    def _raw(self, repo: str, number: int) -> dict:
        d = self.data.get("issues", {}).get(f"{repo}#{number}")
        if d is None:
            raise Fail(EXIT_ERROR, f"no issue {repo}#{number}")
        return d

    def _ref(self, ref: str) -> dict:
        r, n = ref.split("#")
        return {**_issue(self._raw(r, int(n)), int(n)), "repo": r}

    def _part(self, ref: str, boards: bool) -> dict:
        raw = self._raw(ref.split("#")[0], int(ref.split("#")[1]))
        found = {"issue": self._ref(ref), "blocked_by": [self._ref(b) for b in raw.get("blocked_by", [])]}
        if boards:
            found["boards"] = {"*": {"item": ref, "fields": raw["fields"]}} if "fields" in raw else {}
        return found

    # ------------------------------------------------------------ reads

    @_logged
    def user(self) -> str:
        return self.data["user"]

    @_logged
    def item(self, repo: str, number: int, boards: bool = False) -> dict:
        self._raw(repo, number)
        found = self._part(f"{repo}#{number}", boards)
        found["issue"].pop("repo")
        children = self._raw(repo, number).get("sub_issues", [])
        return {**found, "sub_issues": [self._part(ref, boards) for ref in children]}

    @_logged
    def open_items(self, repo: str, label: str | None, issue_type: str | None) -> list[int]:
        found = []
        for ref, d in self.data.get("issues", {}).items():
            r, n = ref.split("#")
            if r != repo or d.get("state", "open") != "open":
                continue
            if (not label or label in d.get("labels", [])) and (not issue_type or d.get("type") == issue_type):
                found.append(int(n))
        return sorted(found, reverse=True)

    @_logged
    def board(self, owner: str, project: int) -> dict:
        layout = self.data.get("boards", {}).get(f"{owner}/{project}")
        if layout is None:
            return {"id": f"{owner}/{project}", "fields": None}  # no layout in the file: every field exists
        fields = {}
        for name, f in layout.get("fields", {}).items():
            options = {o: f"{name}:{o}" for o in f["options"]} if "options" in f else None
            fields[name] = {"id": name, "options": options}
        return {"id": f"{owner}/{project}", "fields": fields}

    @_logged
    def comments(self, repo: str, number: int) -> list[dict]:
        issue = self.data.get("issues", {}).get(f"{repo}#{number}")
        if issue is not None:
            return issue.get("comments", [])
        pulls = [p for p in self.data.get("pulls", {}).get(repo, []) if p["number"] == number]
        return pulls[0].get("comments", []) if pulls else []

    @_logged
    def pulls(self, repo: str, branch: str, base: str) -> list[dict]:
        pulls = self.data.get("pulls", {}).get(repo, [])
        found = [p for p in pulls if p["branch"] == branch and p.get("base", base) == base]
        return [
            {
                "number": p["number"],
                "state": p.get("state", "open"),
                "draft": p.get("draft", False),
                "head": p.get("head"),
                "merged_by": p.get("merged_by"),
                "author": p.get("author"),
                "body": p.get("body", ""),
                "reviews": p.get("reviews", []),
                "comments": p.get("comments", []),
            }
            for p in sorted(found, key=lambda p: p["number"])
        ]

    def _pull(self, repo: str, number: int) -> dict:
        return next(p for p in self.data.get("pulls", {}).get(repo, []) if p["number"] == number)

    @_logged
    def reviews(self, repo: str, number: int) -> list[dict]:
        return self._pull(repo, number).get("reviews", [])

    @_logged
    def diff(self, repo: str, number: int) -> str:
        return self._pull(repo, number).get("diff", "")

    @_logged
    def checks(self, repo: str, number: int) -> tuple[str, list[str]]:
        p = self._pull(repo, number)
        return p.get("checks", "pass"), p.get("failed_checks", [])

    # ------------------------------------------------------------ writes, for apply

    def save(self) -> None:
        with self._lock:
            Path(self.path).write_text(json.dumps(self.data, indent=2))

    @_logged
    def create_issue(self, repo: str, title: str, body: str, issue_type: str | None) -> dict:
        with self._lock:
            issues = self.data.setdefault("issues", {})
            number = 1 + max((int(r.split("#")[1]) for r in issues if r.startswith(f"{repo}#")), default=0)
            issues[f"{repo}#{number}"] = {"title": title, "body": body, "type": issue_type, "state": "open"}
            self.save()
        return {"number": number, "id": number}

    @_logged
    def add_sub_issue(self, repo: str, parent: int, child: dict) -> None:
        with self._lock:
            self._raw(repo, parent).setdefault("sub_issues", []).append(f"{repo}#{child['number']}")
            self.save()

    @_logged
    def add_blocked_by(self, repo: str, number: int, blocker_repo: str, blocker: int, blocker_id: int) -> None:
        with self._lock:
            blocked_by = self._raw(repo, number).setdefault("blocked_by", [])
            if f"{blocker_repo}#{blocker}" not in blocked_by:
                blocked_by.append(f"{blocker_repo}#{blocker}")
            self.save()

    @_logged
    def edit_labels(self, repo: str, number: int, add: list, remove: list) -> None:
        with self._lock:
            d = self._raw(repo, number)
            d["labels"] = [x for x in d.get("labels", []) if x not in remove] + [
                x for x in add if x not in d.get("labels", [])
            ]
            self.save()

    @_logged
    def assign(self, repo: str, number: int, users: list) -> None:
        with self._lock:
            d = self._raw(repo, number)
            d["assignees"] = d.get("assignees", []) + [u for u in users if u not in d.get("assignees", [])]
            self.save()

    @_logged
    def comment(self, repo: str, number: int, body: str) -> None:
        with self._lock:
            target = self.data.get("issues", {}).get(f"{repo}#{number}")
            if target is None:
                target = self._pull(repo, number)
            comments = target.setdefault("comments", [])
            comments.append({"id": len(comments) + 1, "body": body, "user": self.data["user"]})
            self.save()

    @_logged
    def close(self, repo: str, number: int) -> None:
        with self._lock:
            self._raw(repo, number)["state"] = "closed"
            self.save()

    def plan_field(self, board: dict, owner: str, project: int, field: str, value) -> str:
        if board["fields"] is None:
            return field
        found = board["fields"].get(field)
        if found is None:
            raise Fail(EXIT_ERROR, f"no field {field} on project {owner}/{project}")
        if found["options"] is not None and value not in found["options"]:
            raise Fail(EXIT_ERROR, f"field {field} on project {owner}/{project} has no option {value!r}")
        return field

    @_logged
    def add_to_board(self, repo: str, number: int, owner: str, project: int) -> str:
        with self._lock:
            self._raw(repo, number).setdefault("fields", {})
            self.save()
        return f"{repo}#{number}"

    @_logged
    def set_field(self, board: dict, item: str, plan: str, value) -> None:
        with self._lock:
            repo, number = item.split("#")
            self._raw(repo, int(number)).setdefault("fields", {})[plan] = value
            self.save()

    @_logged
    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> int:
        with self._lock:
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

    @_logged
    def ready_pr(self, repo: str, number: int) -> None:
        with self._lock:
            self._pull(repo, number)["draft"] = False
            self.save()

    def note_merged(self, repo: str, head: str, base: str) -> None:
        """GitHub marks a pull request merged when its head reaches its base; the fake is told."""
        with self._lock:
            for p in self.data.get("pulls", {}).get(repo, []):
                if p["branch"] == head and p.get("base", base) == base and p.get("state", "open") == "open":
                    p["state"] = "merged"
            self.save()


class _Cell:
    """One read: made by one thread, awaited by the others."""

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error: BaseException | None = None


class Memo:
    """What the engine calls. Each read is made once per evaluation: the first caller makes it, a concurrent
    caller of the same read waits for that answer. `item` reads answer `issue`, `sub_issues`, `blocked_by` and
    `fields` of the item and of its sub-issues; `pulls` answers `pull`, `reviews` and `comments` of its pull
    requests. Returned values are copies, so no caller changes what another reads."""

    def __init__(self, backend, boards: bool = False):
        """`boards`: the project has a board, so reads of an item ask for its board entries from the start."""
        self.backend = backend
        self._lock = threading.Lock()
        self._cells: dict[tuple, _Cell] = {}
        self._added: dict[tuple, str] = {}
        self._slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self._boards = boards  # whether item reads ask for board entries

    def forget(self) -> None:
        """Drop what was read, for the next round of `apply`. A board's layout is no answer to anything the
        engine decides: it stays, so the board is read once per command."""
        with self._lock:
            self._cells = {k: c for k, c in self._cells.items() if k[0] == "board"}
            self._added.clear()

    def _call(self, name: str, *args):
        with self._slots:
            return getattr(self.backend, name)(*args)

    def _once(self, key: tuple, fetch):
        with self._lock:
            cell = self._cells.get(key)
            mine = cell is None
            if mine:
                cell = self._cells[key] = _Cell()
        if mine:
            try:
                cell.value = fetch()
            except BaseException as exc:
                cell.error = exc
            finally:
                cell.done.set()
        else:
            cell.done.wait()
        if cell.error is not None:
            raise cell.error
        return cell.value

    def _seed(self, key: tuple, value) -> None:
        """An answer that came with another read, unless the read itself is already under way."""
        with self._lock:
            if key not in self._cells:
                cell = self._cells[key] = _Cell()
                cell.value = value
                cell.done.set()

    def _read(self, name: str, *args):
        return self._once((name, *args), lambda: self._call(name, *args))

    # ------------------------------------------------------------ reads

    def user(self) -> str:
        return self._read("user")

    def open_items(self, repo: str, label: str | None, issue_type: str | None) -> list[int]:
        return copy.deepcopy(self._read("open_items", repo, label, issue_type))

    def _read_item(self, repo: str, number: int, boards: bool) -> dict:
        """One request for an item and its sub-issues, whose answers also serve each sub-issue's own reads."""
        found = self._call("item", repo, number, boards)
        if boards:
            self._seed(("boards", repo, number), found["boards"])
        for child in found["sub_issues"]:
            issue = child["issue"]
            where = (issue["repo"], issue["number"])
            if boards:
                self._seed(("boards", *where), child["boards"])
            self._seed(("item", *where), {"issue": _without(issue, "repo"), "blocked_by": child["blocked_by"]})
        return found

    def _item(self, repo: str, number: int) -> dict:
        return self._once(("item", repo, number), lambda: self._read_item(repo, number, self._boards))

    def issue(self, repo: str, number: int) -> dict:
        return copy.deepcopy(self._item(repo, number)["issue"])

    def sub_issues(self, repo: str, number: int) -> list[dict]:
        found = self._item(repo, number)
        if "sub_issues" not in found:  # an item that came with its parent's read
            found = self._once(("item+", repo, number), lambda: self._read_item(repo, number, self._boards))
        return copy.deepcopy([c["issue"] for c in found["sub_issues"]])

    def blocked_by(self, repo: str, number: int) -> list[dict]:
        """The items this one is blocked by, in any repository and any state."""
        return copy.deepcopy(self._item(repo, number)["blocked_by"])

    def _board_entry(self, repo: str, number: int, owner: str, project: int) -> dict | None:
        """The item's place on the board. Board entries are asked for when a board field is first read, and from
        then on with every read of an item: a factory without a board never asks, and needs no `project` scope."""

        def fetch() -> dict:
            self._boards = True
            return self._read_item(repo, number, True)["boards"]

        boards = self._once(("boards", repo, number), fetch)
        return boards.get(f"{owner}/{project}", boards.get("*"))

    def fields(self, repo: str, number: int, owner: str, project: int) -> dict:
        """The item's field values on one project board, by field name."""
        entry = self._board_entry(repo, number, owner, project)
        return copy.deepcopy(entry["fields"]) if entry else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        return copy.deepcopy(self._read("comments", repo, number))

    def _pulls(self, repo: str, branch: str, base: str) -> list[dict]:
        def fetch() -> list[dict]:
            found = self._call("pulls", repo, branch, base)
            for p in found:
                self._seed(("reviews", repo, p["number"]), p["reviews"])
                self._seed(("comments", repo, p["number"]), p["comments"])
            return found

        return self._once(("pulls", repo, branch, base), fetch)

    def pulls(self, repo: str, branch: str, base: str) -> list[dict]:
        """Every pull request from `branch` into `base`, in any state, oldest first."""
        return [
            {k: copy.deepcopy(v) for k, v in p.items() if k not in ("reviews", "comments")}
            for p in self._pulls(repo, branch, base)
        ]

    def pull(self, repo: str, branch: str, base: str) -> dict | None:
        """The newest pull request from `branch` into `base`, in any state."""
        found = self.pulls(repo, branch, base)
        return found[-1] if found else None

    def reviews(self, repo: str, number: int) -> list[dict]:
        return copy.deepcopy(self._read("reviews", repo, number))

    def diff(self, repo: str, number: int) -> str:
        return self._read("diff", repo, number)

    def checks(self, repo: str, number: int) -> tuple[str, list[str]]:
        """`pass` (or no checks), `pending` or `fail` for a pull request's required checks, and the failing ones."""
        return copy.deepcopy(self._read("checks", repo, number))

    def board(self, owner: str, project: int) -> dict:
        return self._read("board", owner, project)

    # ------------------------------------------------------------ writes: write calls only, no update of what was read.
    # They pass `_call` too, so at most MAX_IN_FLIGHT requests run at once, reads and writes together.

    def create_issue(self, repo: str, title: str, body: str, issue_type: str | None) -> dict:
        return self._call("create_issue", repo, title, body, issue_type)

    def add_sub_issue(self, repo: str, parent: int, child: dict) -> None:
        self._call("add_sub_issue", repo, parent, child)

    def add_blocked_by(self, repo: str, number: int, blocker_repo: str, blocker: int, blocker_id: int | None = None):
        """`blocker_id` is the blocker's id, as the read or the write that made it gave it."""
        if blocker_id is None:
            blocker_id = self.issue(blocker_repo, blocker)["id"]
        self._call("add_blocked_by", repo, number, blocker_repo, blocker, blocker_id)

    def edit_labels(self, repo: str, number: int, add: list, remove: list) -> None:
        self._call("edit_labels", repo, number, add, remove)

    def assign(self, repo: str, number: int, users: list) -> None:
        self._call("assign", repo, number, users)

    def comment(self, repo: str, number: int, body: str) -> None:
        self._call("comment", repo, number, body)

    def close(self, repo: str, number: int) -> None:
        self._call("close", repo, number)

    def set_field(self, repo: str, number: int, owner: str, project: int, field: str, value) -> None:
        """Set a board field: one write when the item is on the board, two when it isn't (it is added first). The
        field and its option are checked before anything is written."""
        board = self.board(owner, project)
        plan = self.backend.plan_field(board, owner, project, field, value)
        entry = self._board_entry(repo, number, owner, project)
        key = (repo, number, owner, project)
        with self._lock:
            item = (entry or {}).get("item") or self._added.get(key)
        if item is None:
            item = self._call("add_to_board", repo, number, owner, project)
            with self._lock:
                self._added[key] = item
        self._call("set_field", board, item, plan, value)

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> int:
        return self._call("create_pr", repo, head, base, title, body, draft)

    def ready_pr(self, repo: str, number: int) -> None:
        self._call("ready_pr", repo, number)

    def note_merged(self, repo: str, head: str, base: str) -> None:
        """Only the fake needs telling that a pull request merged: GitHub does it itself."""
        if hasattr(self.backend, "note_merged"):
            self.backend.note_merged(repo, head, base)


def _without(d: dict, key: str) -> dict:
    return {k: v for k, v in d.items() if k != key}


def make_tracker(boards: bool = False) -> Memo:
    """The tracker the engine reads. Pass `boards=True` when the project has a board (`tracker.project` or a
    field is configured): the run's item then comes with its board entries in the one request. Without it, a
    board field read asks for them in a second request, and a factory with no board never asks."""
    fake = os.environ.get("FAB0_GH_FAKE")
    return Memo(GhFake(fake) if fake else GhCli(), boards)
