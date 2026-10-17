"""The run store: what a run produces for itself, kept out of the product repositories (MIGRATION.md, M1).

One runs repository per product holds, under `runs/<project>/<run>/`:
- objects: immutable bytes named by their hash, `objects/sha256-<hex>.<ext>`. Writing the same content twice
  is a no-op, and a read fails when the content doesn't match its name.
- facts: append-only YAML records, `ledger/<name>.yaml`. Writing a fact whose name exists answers `exists`.
  That is the store's only concurrency control.

Only the engine calls it. The git backend keeps a clone of the runs repository and commits each write; `push`
makes the writes visible to everyone. A push that loses a race fetches, puts its own files back on top of the
remote and pushes again. Nothing in the store is ever changed or removed, so that replay can't conflict, except
on a fact another machine wrote first: the remote's fact stands, and the writer is told.
"""

from __future__ import annotations

import fcntl
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import yaml

from .errors import EXIT_ERROR, Fail
from .hashing import sha

BRANCH = "main"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # a run id or a fact name: one path segment
EXT = re.compile(r"^[a-z0-9]+$")
DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
PUSH_TRIES = 3


class GitStore:
    """The store in a git repository: a clone at `path` of `url`, or a local repository when `url` is None and
    the checkout has no remote."""

    def __init__(self, path: Path, project: str, url: str | None = None):
        self.path = path
        self.project = project
        self.url = url

    # ------------------------------------------------------------ the interface

    def put(self, run: str, data: bytes, ext: str) -> str:
        """Keep `data` as an object of `run`; returns its hash. Idempotent."""
        digest = sha(data)
        if not EXT.match(ext):
            raise Fail(EXIT_ERROR, f"store: '{ext}' is not an object extension")
        rel = f"{self.run_dir(run)}/objects/sha256-{digest.removeprefix('sha256:')}.{ext}"
        with self._locked():
            file = self.path / rel
            if file.exists():
                if sha(file.read_bytes()) != digest:
                    raise Fail(EXIT_ERROR, f"store: {rel} doesn't match its hash")
                return digest
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            self._commit([rel], f"{run}: object {digest[7:19]}")
        return digest

    def get(self, run: str, digest: str) -> bytes:
        """The object of `run` with hash `digest`; fails if it is missing or doesn't match."""
        match = DIGEST.match(digest)
        if not match:
            raise Fail(EXIT_ERROR, f"store: '{digest}' is not a sha256 hash")
        found = sorted((self.path / self.run_dir(run) / "objects").glob(f"sha256-{match[1]}.*"))
        if not found:
            raise Fail(EXIT_ERROR, f"store: run {run} has no object {digest}")
        data = found[0].read_bytes()
        if sha(data) != digest:
            raise Fail(EXIT_ERROR, f"store: {found[0].relative_to(self.path)} doesn't match its hash")
        return data

    def append(self, run: str, name: str, fact: dict) -> str:
        """Write the fact `name` of `run`: `ok`, or `exists` with nothing written."""
        if not NAME.match(name):
            raise Fail(EXIT_ERROR, f"store: '{name}' is not a fact name")
        rel = f"{self.run_dir(run)}/ledger/{name}.yaml"
        with self._locked():
            file = self.path / rel
            if file.exists():
                return "exists"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(yaml.safe_dump(fact, sort_keys=False, allow_unicode=True))
            self._commit([rel], f"{run}: {name}")
        return "ok"

    def facts(self, run: str) -> dict[str, dict]:
        """Every fact of `run`, by name."""
        folder = self.path / self.run_dir(run) / "ledger"
        if not folder.is_dir():
            return {}
        return {f.stem: yaml.safe_load(f.read_text()) for f in sorted(folder.glob("*.yaml"))}

    def runs(self) -> list[str]:
        """Every run of this project that has a fact."""
        folder = self.path / "runs" / self.project
        if not folder.is_dir():
            return []
        return sorted(d.name for d in folder.iterdir() if any((d / "ledger").glob("*.yaml")))

    def sync(self, direction: str) -> list[tuple[str, str]]:
        """`pull` brings in what others pushed; `push` makes local writes visible to everyone. Both return the
        facts, as (run, name), that another machine wrote first: the local ones are replaced by the remote's."""
        if direction not in ("pull", "push"):
            raise Fail(EXIT_ERROR, f"store: sync {direction}? Use pull or push")
        with self._locked():
            return self._pull() if direction == "pull" else self._push()

    # ------------------------------------------------------------ git

    def run_dir(self, run: str) -> str:
        if not NAME.match(run):
            raise Fail(EXIT_ERROR, f"store: '{run}' is not a run id")
        return f"runs/{self.project}/{run}"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.path), *args], capture_output=True)

    def _out(self, *args: str) -> str:
        run = self._run(*args)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"store: git {' '.join(args[:2])} failed: {run.stderr.decode().strip()}")
        return run.stdout.decode().strip()

    def _has(self, ref: str) -> bool:
        return self._run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode == 0

    def _has_remote(self) -> bool:
        return self._run("remote", "get-url", "origin").returncode == 0

    def ensure(self) -> None:
        """Clone the runs repository, or start a local one, if there is no checkout yet."""
        if (self.path / ".git").exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.url:
            run = subprocess.run(["git", "clone", "-q", self.url, str(self.path)], capture_output=True)
            if run.returncode:
                raise Fail(EXIT_ERROR, f"store: cannot clone {self.url}: {run.stderr.decode().strip()}")
            if not self._has("HEAD"):  # an empty repository: the first write starts main
                self._out("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        else:
            self.path.mkdir(exist_ok=True)
            self._out("init", "-q", "-b", BRANCH)

    @contextmanager
    def _locked(self):
        """One writer at a time on this machine: parallel records share the clone."""
        self.ensure()
        lock = Path(self._out("rev-parse", "--absolute-git-dir")) / "fab0-store.lock"
        with open(lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _commit(self, paths: list[str], message: str) -> None:
        self._out("add", "--", *paths)
        self._out("commit", "-q", "--no-verify", "-m", message, "--", *paths)

    def _pull(self) -> list[tuple[str, str]]:
        remote = f"origin/{BRANCH}"
        if not self._has_remote():
            return []
        self._out("fetch", "-q", "origin")
        if not self._has(remote):
            return []  # nobody has pushed yet
        if not self._has("HEAD") or self._run("merge-base", "--is-ancestor", "HEAD", remote).returncode == 0:
            self._out("reset", "-q", "--hard", remote)
            return []
        if self._run("merge-base", "--is-ancestor", remote, "HEAD").returncode == 0:
            return []  # only local writes, not pushed yet
        return self._replay(remote)

    def _replay(self, remote: str) -> list[tuple[str, str]]:
        """Put the files that local commits added back on top of `remote`, in one commit."""
        base = self._run("merge-base", "HEAD", remote).stdout.decode().strip()
        listing = (
            ("diff", "--name-only", "--diff-filter=A", base, "HEAD")
            if base
            else ("ls-tree", "-r", "--name-only", "HEAD")
        )
        mine = {p: self._run("cat-file", "blob", f"HEAD:{p}").stdout for p in self._out(*listing).splitlines() if p}
        self._out("reset", "-q", "--hard", remote)
        lost, kept = [], []
        for rel, data in sorted(mine.items()):
            file = self.path / rel
            if file.exists():
                if file.read_bytes() != data and "/ledger/" in rel:
                    parts = Path(rel).parts  # runs/<project>/<run>/ledger/<name>.yaml
                    lost.append((parts[2], Path(rel).stem))
                continue  # an object of the same name has the same content
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            kept.append(rel)
        if kept:
            self._commit(kept, f"fab0: {len(kept)} writes brought onto {remote}")
        return lost

    def _push(self) -> list[tuple[str, str]]:
        if not self._has_remote():
            return []
        lost: list[tuple[str, str]] = []
        problem = ""
        for _ in range(PUSH_TRIES):
            lost += self._pull()
            if not self._has("HEAD"):
                return lost
            remote = f"origin/{BRANCH}"
            if self._has(remote) and self._run("merge-base", "--is-ancestor", "HEAD", remote).returncode == 0:
                return lost  # nothing left to push
            run = self._run("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
            if run.returncode == 0:
                return lost
            problem = run.stderr.decode().strip()
        raise Fail(EXIT_ERROR, f"store: push refused {PUSH_TRIES} times: {problem}")


def default_project(project: dict) -> str:
    """The home repository's name on GitHub, as a store key: `fab0-dev/docs` → `docs`."""
    name = project["repos"]["home"]["github"].split("/")[1].lower()
    return re.sub(r"[^a-z0-9-]+", "-", name).strip("-") or "home"


def remote_url(home_origin: str | None, slug: str) -> str:
    """The runs repository's URL, over the same protocol as the home repository's remote."""
    if home_origin and (home_origin.startswith("git@") or home_origin.startswith("ssh://")):
        return f"git@github.com:{slug}.git"
    return f"https://github.com/{slug}.git"


def open_store(project: dict, home_root: Path) -> GitStore | None:
    """The project's store, from the `store` block of fab0.yaml; None when it has none. Without `path`, the
    engine keeps its own clone in `<home git dir>/fab0/store`."""
    cfg = project.get("store")
    if cfg is None:
        return None

    def home_git(*args: str) -> str | None:
        run = subprocess.run(["git", "-C", str(home_root), *args], capture_output=True, text=True)
        return run.stdout.strip() if run.returncode == 0 else None

    url = remote_url(home_git("remote", "get-url", "origin"), cfg["github"]) if "github" in cfg else None
    if "path" in cfg:
        path = (home_root / cfg["path"]).resolve()
    else:
        common = home_git("rev-parse", "--path-format=absolute", "--git-common-dir")
        if common is None:
            raise Fail(EXIT_ERROR, f"store: {home_root} is not a git repository")
        path = Path(common) / "fab0" / "store"
    return GitStore(path, cfg.get("project") or default_project(project), url)
