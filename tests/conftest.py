"""Test helpers: a workspace of real git repos made from tests/fixtures/workspace, and a fake GitHub."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins" / "fab0" / "bin" / "fab0-resolve"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workspace"
RUN = "412"

SPEC = """# Booking extension

Guests extend a booking by one night.

## Scenarios

- S1: a guest extends a booking when the room is free.
"""

TASKS = """- id: T1
  title: Booking API extension
  repo: backend
  files: [src/booking.py, tests/test_booking.py]
  traces_to: [S1]
- id: T2
  title: Booking screen
  repo: home
  files: [web/booking.html]
  depends_on: [T1]
  traces_to: [S1]
"""


def load_resolver():
    """Import the resolver script as a module, for unit tests."""
    loader = importlib.machinery.SourceFileLoader("fab0_resolve", str(SCRIPT))
    spec = importlib.util.spec_from_loader("fab0_resolve", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(path: Path, *args: str) -> str:
    run = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert run.returncode == 0, f"git {' '.join(args)}: {run.stderr}"
    return run.stdout.strip()


class Workspace:
    """Two sibling repos (shop is home, shop-api is backend) and a JSON fake of GitHub."""

    def __init__(self, root: Path):
        shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
        self.root = root
        self.home = root / "shop"
        self.api = root / "shop-api"
        self.gh_path = root / "gh.json"
        self.repos = {"acme/shop": self.home, "acme/shop-api": self.api}
        self.next_number = 413  # issues
        self.next_pr = 88
        for repo in self.repos.values():
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "commit.gpgsign", "false")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "initial")

    # -- fake GitHub

    @property
    def gh(self) -> dict:
        return json.loads(self.gh_path.read_text())

    def save_gh(self, data: dict) -> None:
        self.gh_path.write_text(json.dumps(data, indent=2))

    def open_pr(self, github: str, branch: str) -> int:
        data = self.gh
        number = self.next_pr
        self.next_pr += 1
        data["pulls"].setdefault(github, []).append(
            {"number": number, "branch": branch, "state": "open", "reviews": []}
        )
        self.save_gh(data)
        return number

    def add_review(self, github: str, number: int, state: str, body: str, review_id: int) -> None:
        data = self.gh
        pr = next(p for p in data["pulls"][github] if p["number"] == number)
        pr["reviews"].append({"id": review_id, "state": state, "body": body, "user": "reviewer"})
        self.save_gh(data)

    def merge(self, github: str, branch: str) -> None:
        """Merge the branch into main, as merging its PR on GitHub and pulling would."""
        repo = self.repos[github]
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        data = self.gh
        for pr in data["pulls"].get(github, []):
            if pr["branch"] == branch and pr["state"] == "open":
                pr["state"] = "merged"
        self.save_gh(data)

    def apply_plan(self, ops: list) -> None:
        """Do what the github skill would do with a plan action's ops."""
        data = self.gh
        refs: dict[str, str] = {}

        def ref(r: dict) -> str:
            return f"{r['repo']}#{r['number']}" if r["number"] else refs[r["task"]]

        for op in ops:
            if op["op"] == "create-issue":
                key = f"{op['issue']['repo']}#{self.next_number}"
                self.next_number += 1
                refs[op["issue"]["task"]] = key
                data["issues"][key] = {
                    "title": op["title"],
                    "body": op["body"],
                    "type": op["type"],
                    "state": "open",
                    "assignees": op["assignees"],
                    "sub_issues": [],
                    "blocked_by": [],
                }
            elif op["op"] == "add-sub-issue":
                data["issues"][f"{op['parent']['repo']}#{op['parent']['number']}"]["sub_issues"].append(
                    ref(op["child"])
                )
            elif op["op"] == "add-blocked-by":
                data["issues"][ref(op["issue"])]["blocked_by"].append(ref(op["blocked_by"]))
            elif op["op"] == "remove-blocked-by":
                data["issues"][ref(op["issue"])]["blocked_by"].remove(ref(op["blocked_by"]))
            elif op["op"] == "edit-title":
                data["issues"][ref(op["issue"])]["title"] = op["title"]
            elif op["op"] == "close-issue":
                data["issues"][ref(op["issue"])].update(state="closed", state_reason=op["reason"])
            else:
                raise AssertionError(f"unknown op {op}")
        self.save_gh(data)

    def apply_deliver(self, ops: list) -> int:
        """Do what the github skill would do with a deliver action's ops; returns the PR number."""
        number = None
        for op in ops:
            repo = self.repos[op["repo"]]
            if op["op"] == "branch":
                git(repo, "checkout", "-q", "-B", op["branch"], op["from"])
            elif op["op"] == "append":
                with open(repo / op["path"], "a") as f:
                    f.write(op["text"])
            elif op["op"] == "remove":
                git(repo, "rm", "-r", "-q", op["path"])
            elif op["op"] == "open-pr":
                self.commit(repo, op["title"])
                number = self.open_pr(op["repo"], op["branch"])
        return number

    # -- git

    def checkout(self, repo: Path, branch: str, start_from: str) -> None:
        if start_from == "branch":
            git(repo, "checkout", "-q", branch)
        else:
            git(repo, "checkout", "-q", "-B", branch, "main")

    def write(self, repo: Path, rel: str, text: str) -> None:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, repo: Path, message: str) -> None:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)

    # -- the resolver

    def run(self, *args: str, expect: int | None = 0) -> dict:
        env = {**os.environ, "FAB0_GH_FAKE": str(self.gh_path)}
        run = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.home, env=env, capture_output=True, text=True
        )
        assert expect is None or run.returncode == expect, f"exit {run.returncode}\n{run.stdout}\n{run.stderr}"
        return json.loads(run.stdout)

    def next(self, *extra: str, user: str = "po-user", expect: int = 0) -> dict:
        return self.run("next", RUN, *extra, "--as", user, expect=expect)

    def record(self, stage: str, returned, task: str | None = None, gate: bool = False, expect: int | None = 0) -> dict:
        path = self.root / "return.json"
        path.write_text(returned if isinstance(returned, str) else json.dumps(returned))
        args = ["record", RUN, stage, "--return", str(path)]
        if task:
            args += ["--task", task]
        if gate:
            args.append("--gate")
        return self.run(*args, expect=expect)

    # -- whole steps

    def do_spec(self, spec: str = SPEC, tasks: str = TASKS, merge: bool = True) -> dict:
        """Run the spec stage to its human gate, and merge its PR."""
        res = self.next()
        assert res["kind"] == "stage" and res["stage"] == "spec", res
        self.checkout(self.home, res["branch"], res["start_from"])
        self.write(self.home, "specs/412.md", spec)
        self.write(self.home, "work/412/tasks.yaml", tasks)
        rec = self.record("spec", {"summary": "Wrote the spec and the tasks."})
        assert rec["verified"], rec
        self.commit(self.home, "spec")
        if merge:
            self.open_pr("acme/shop", res["branch"])
            self.merge("acme/shop", res["branch"])
        return res

    def do_plan(self) -> None:
        res = self.next()
        assert res["kind"] == "action" and res["action"] == "plan", res
        self.apply_plan(res["ops"])

    def do_task(self, task: str, user: str, files: dict, merge: bool = True) -> dict:
        """Implement a task, have the gate approve it, and merge its PR."""
        res = self.next("--task", task, user=user)
        assert res["kind"] == "stage" and res["stage"] == "implement", res
        repo = self.repos["acme/shop-api" if res["repo"] == "backend" else "acme/shop"]
        self.checkout(repo, res["branch"], res["start_from"])
        for rel, text in files.items():
            self.write(repo, rel, text)
        assert self.record("implement", {"summary": f"Implemented {task}."}, task=task)["verified"]
        self.commit(repo, f"implement {task}")
        gate = self.next("--task", task, user=user)
        assert gate["kind"] == "gate", gate
        self.record("implement", {"decision": "approve", "reasons": ["Meets S1."]}, task=task, gate=True)
        self.commit(repo, f"gate {task}")
        if merge:
            github = "acme/shop-api" if res["repo"] == "backend" else "acme/shop"
            self.open_pr(github, res["branch"])
            self.merge(github, res["branch"])
        return res


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws")


@pytest.fixture(scope="session")
def resolver():
    return load_resolver()
