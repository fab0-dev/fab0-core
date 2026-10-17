"""Test helpers: copies of the fixture factories in tests/fixtures/, and the engine on the command line."""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins" / "fab0" / "bin" / "fab0-resolve"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACTORIES = sorted(p.name for p in FIXTURES.iterdir() if (p / ".fab0").is_dir())

sys.path.insert(0, str(ROOT / "plugins" / "fab0" / "lib"))

STORY_GH = {
    "user": "po-user",
    "issues": {"acme/shop#412": {"title": "Extend a booking", "body": "Guests extend a booking.", "type": "Story"}},
}


def reads(calls: list[dict]) -> list[dict]:
    """The calls of the fake tracker that read."""
    from fab0_engine.tracker import WRITES

    return [c for c in calls if c["method"] not in WRITES]


def writes(calls: list[dict]) -> list[dict]:
    from fab0_engine.tracker import WRITES

    return [c for c in calls if c["method"] in WRITES]


def in_flight(calls: list[dict]) -> int:
    """The most calls that were under way at the same time."""
    edges = sorted([(c["start"], 1) for c in calls] + [(c["end"], -1) for c in calls], key=lambda e: (e[0], e[1]))
    most = now = 0
    for _, step in edges:  # a call that ends as another starts doesn't overlap it
        now += step
        most = max(most, now)
    return most


def git(path: Path, *args: str, check: bool = True) -> str:
    run = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert run.returncode == 0 or not check, f"git {' '.join(args)}: {run.stderr}"
    return run.stdout.strip()


class Factory:
    """A copy of one fixture factory, as the home repo of a project."""

    def __init__(self, name: str, tmp: Path):
        self.home = tmp / name
        self.gh_path = tmp / "gh.json"
        shutil.copytree(FIXTURES / name, self.home)

    def repo(self, name: str) -> Path:
        return (self.home / self.read("fab0.yaml")["repos"][name]["path"]).resolve()

    def init_git(self, gh: dict | None = None) -> "Factory":
        """Every repository of fab0.yaml as a git repo on main; the others get a README."""
        for name in self.read("fab0.yaml")["repos"]:
            path = self.repo(name)
            if not path.exists():
                path.mkdir(parents=True)
                (path / "README.md").write_text(f"# {name}\n")
            git(path, "init", "-q", "-b", "main")
            git(path, "config", "user.email", "test@example.com")
            git(path, "config", "user.name", "test")
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "start")
        store = self.store_path()
        if not store.exists():  # the run store: a local repository, with no remote
            store.mkdir(parents=True)
            git(store, "init", "-q", "-b", "main")
            git(store, "config", "user.email", "test@example.com")
            git(store, "config", "user.name", "test")
        self.gh_path.write_text(json.dumps(gh or {"user": "tester", "issues": {}}))
        return self

    def store_path(self) -> Path:
        return (self.home / self.read("fab0.yaml")["store"]["path"]).resolve()

    def project_key(self) -> str:
        cfg = self.read("fab0.yaml")
        return cfg["store"].get("project") or cfg["repos"]["home"]["github"].split("/")[1]

    def facts(self, run: str) -> dict:
        """The run's ledger in the run store, by fact name."""
        folder = self.store_path() / "runs" / self.project_key() / run / "ledger"
        return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(folder.glob("*.yaml"))} if folder.is_dir() else {}

    def fact(self, run: str, name: str) -> dict:
        return self.facts(run)[name]

    def put_fact(self, run: str, name: str, fact: dict) -> None:
        """A ledger entry written as `record` would, for a test to set up a run."""
        rel = f"runs/{self.project_key()}/{run}/ledger/{name}.yaml"
        path = self.store_path() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(fact, sort_keys=False))
        git(self.store_path(), "add", rel)
        git(self.store_path(), "commit", "-q", "-m", f"{run}: {name}")

    def branch(self, repo: str, name: str, start: str) -> None:
        git(self.repo(repo), "branch", name, start)

    def checkout(self, repo: str, name: str, start: str | None = None) -> None:
        git(self.repo(repo), "checkout", "-q", *(["-b", name, start] if start else [name]))

    def write(self, repo: str, rel: str, text: str) -> None:
        path = self.repo(repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, repo: str, message: str = "work") -> None:
        git(self.repo(repo), "add", "-A")
        git(self.repo(repo), "commit", "-q", "-m", message)

    def gh_edit(self, change) -> None:
        data = json.loads(self.gh_path.read_text())
        change(data)
        self.gh_path.write_text(json.dumps(data))

    def calls(self) -> list[dict]:
        """What the fake tracker was asked since `reset_calls`, in the order the calls ended: one request each,
        as {"method", "args", "start", "end"}."""
        log = Path(f"{self.gh_path}.calls")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def reset_calls(self) -> None:
        Path(f"{self.gh_path}.calls").unlink(missing_ok=True)

    def delay(self, seconds: float) -> None:
        """Every call of the fake tracker takes this long."""
        self.gh_edit(lambda d: d.update(delay=seconds))

    def merge(self, repo: str, branch: str, into: str) -> None:
        """What merging a pull request does: `branch` into `into`, with the checkout left where it was."""
        path = self.repo(repo)
        here = git(path, "rev-parse", "--abbrev-ref", "HEAD")
        git(path, "checkout", "-q", into)
        git(path, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
        git(path, "checkout", "-q", here)

    def read(self, rel: str):
        return yaml.safe_load((self.home / ".fab0" / rel).read_text())

    def edit(self, rel: str, change) -> None:
        data = self.read(rel)
        change(data)
        (self.home / ".fab0" / rel).write_text(yaml.safe_dump(data, sort_keys=False))

    def github(self) -> Path:
        """Where `https://github.com/` points for git in the engine's commands: a local folder, so no test reaches
        GitHub. A test that needs `owner/repo` to exist puts a bare repository at `<this>/owner/repo.git`."""
        return self.gh_path.parent / "github"

    def env(self) -> dict:
        path = f"{Path(__file__).parent / 'bin'}{os.pathsep}{os.environ['PATH']}"  # stand-in linters
        github = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.github()}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
        }
        return {**os.environ, "FAB0_GH_FAKE": str(self.gh_path), "PATH": path, **github}

    def text(self, *args: str, expect: int | None = 0, cwd: Path | None = None, env: dict | None = None):
        """A command whose success output is text (`log`): the completed process."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd or self.home,
            capture_output=True,
            text=True,
            env=env or self.env(),
        )
        if expect is not None:
            assert result.returncode == expect, result.stdout + result.stderr
        return result

    def follow(self, *args: str) -> "Follower":
        """`fab0-resolve log <args> --follow`, started in the background; the caller stops it with `interrupt()`."""
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "log", *args, "--follow"],
            cwd=self.home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(),
        )
        return Follower(proc)

    def run(self, *args: str, expect: int | None = 0) -> dict:
        env = self.env()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.home, capture_output=True, text=True, env=env
        )
        if expect is not None:
            assert result.returncode == expect, result.stdout + result.stderr
        return json.loads(result.stdout)


class Follower:
    """`fab0-resolve log --follow` running in the background: its lines as they come, and a way to stop it the way a
    person does (Ctrl-C). A test never waits on a timeout to end it."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def next(self, timeout: float = 5.0) -> str | None:
        """The next line, or None when nothing came within `timeout` seconds (or the process ended)."""
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def interrupt(self) -> tuple[int, str]:
        """Ctrl-C: the exit code and what it wrote to stderr."""
        self.proc.send_signal(signal.SIGINT)
        code = self.proc.wait(timeout=10)
        return code, self.proc.stderr.read()


@pytest.fixture
def factory(tmp_path):
    """factory("story") copies that fixture factory into a temporary directory."""
    return lambda name: Factory(name, tmp_path)


@pytest.fixture
def story(factory):
    """The story fixture factory with git repositories, and its run 412 on a fake tracker."""
    return factory("story").init_git(STORY_GH)


def start_story(f) -> None:
    """What `apply` does for the story fixture's onStart: run branches, a draft run pull request, the field."""
    for repo in ("home", "backend"):
        if subprocess.run(["git", "-C", f.repo(repo), "rev-parse", "-q", "--verify", "fab0/412/base"]).returncode:
            f.branch(repo, "fab0/412/base", "main")

    def change(data):
        data.setdefault("pulls", {}).setdefault("acme/shop", []).append(
            {"number": 1, "branch": "fab0/412/base", "base": "main", "draft": True}
        )
        data["issues"]["acme/shop#412"]["fields"] = {"State": "Started"}

    f.gh_edit(change)


def start_docs(f) -> None:
    """The docs fixture's onStart: the run branch and its pull request."""
    f.branch("home", "docs/docs-1", "main")
    f.gh_edit(
        lambda d: (
            d.setdefault("pulls", {})
            .setdefault("acme/docs", [])
            .append({"number": 1, "branch": "docs/docs-1", "base": "main"})
        )
    )
