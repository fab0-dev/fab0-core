"""One repository of the workspace, read through git. The engine reads refs and trees; it writes only files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import EXIT_ERROR, Fail
from .hashing import sha


class Repo:
    def __init__(self, name: str, cfg: dict, home_root: Path, base: str):
        self.name = name
        self.github = cfg["github"]
        self.base = cfg.get("base", base)
        self.path = (home_root / cfg["path"]).resolve()
        self._fresh: dict[str, str | None] = {}
        self._trees: dict[tuple[str, str], set[str]] = {}

    def exists(self) -> bool:
        return (self.path / ".git").exists()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.path), *args], capture_output=True)

    def out(self, *args: str) -> str:
        run = self._git(*args)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"git {' '.join(args)} failed in {self.name}: {run.stderr.decode().strip()}")
        return run.stdout.decode().strip()

    def ref_exists(self, ref: str) -> bool:
        return self.exists() and self._git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode == 0

    def fresh(self, branch: str) -> str | None:
        """The freshest ref of a branch: origin/<branch> when it is at or ahead of the local one, else the local."""
        if branch not in self._fresh:
            local, remote = branch, f"origin/{branch}"
            has_local, has_remote = self.ref_exists(local), self.ref_exists(remote)
            if has_local and has_remote:
                ahead = self._git("merge-base", "--is-ancestor", local, remote).returncode == 0
                self._fresh[branch] = remote if ahead else local
            else:
                self._fresh[branch] = remote if has_remote else local if has_local else None
        return self._fresh[branch]

    def sha(self, ref: str) -> str:
        return self.out("rev-parse", f"{ref}^{{commit}}")

    def is_ancestor(self, a: str, b: str) -> bool:
        return self._git("merge-base", "--is-ancestor", a, b).returncode == 0

    def removed_at(self, ref: str, path: str) -> str | None:
        """The commit before the one that removed `path` on `ref`'s history, if one did."""
        run = self._git("log", "-1", "--format=%H", "--diff-filter=D", ref, "--", path)
        sha = run.stdout.decode().strip()
        return f"{sha}^" if run.returncode == 0 and sha else None

    def read_file(self, ref: str, path: str) -> bytes | None:
        run = self._git("cat-file", "blob", f"{ref}:{path}")
        return run.stdout if run.returncode == 0 else None

    def names(self, ref: str, folder: str) -> set[str]:
        """File names directly in `folder` at `ref`."""
        key = (ref, folder)
        if key not in self._trees:
            listing = self._git("ls-tree", "-z", "--name-only", ref, "--", folder.rstrip("/") + "/")
            self._trees[key] = {Path(n).name for n in listing.stdout.decode().split("\0") if n}
        return self._trees[key]

    def common_dir(self) -> Path:
        return Path(self.out("rev-parse", "--path-format=absolute", "--git-common-dir"))

    def worktrees(self) -> dict[str, Path]:
        """Branch → the checkout that has it, from `git worktree list`."""
        if not self.exists():
            return {}
        out, path = {}, None
        for line in self.out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree "))
            elif line.startswith("branch refs/heads/") and path is not None:
                out[line.removeprefix("branch refs/heads/")] = path
        return out

    def scope_state(self) -> dict:
        """The main checkout's HEAD and a hash of every dirty file, to see changes made while a unit ran."""
        if not self.exists():
            return {"absent": True}
        head = self._git("rev-parse", "-q", "--verify", "HEAD").stdout.decode().strip() or None
        parts = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.decode().split("\0")
        dirty, i = {}, 0
        while i < len(parts):
            item = parts[i]
            i += 1
            if len(item) < 4:
                continue
            code, path = item[:2], item[3:]
            if "R" in code or "C" in code:
                i += 1  # the original path follows a rename or copy
            file = self.path / path
            dirty[path] = sha(file.read_bytes()) if file.is_file() else "deleted"
        return {"head": head, "dirty": dirty}

    def changed_since(self, tree: Path, base: str) -> list[str]:
        """Files a checkout changed since `base`: committed, staged, unstaged and untracked."""

        def lines(*args: str) -> list[str]:
            run = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)
            return [line for line in run.stdout.splitlines() if line]

        return sorted(
            set(lines("diff", "--name-only", base)) | set(lines("ls-files", "--others", "--exclude-standard"))
        )


def scope_diff(before: dict, after: dict) -> list[str]:
    if before.get("absent") or after.get("absent"):
        return [] if before == after else ["the repository appeared or disappeared"]
    changes = []
    if before["head"] != after["head"]:
        changes.append(f"HEAD moved from {before['head']} to {after['head']}")
    for path in sorted(set(before["dirty"]) | set(after["dirty"])):
        if before["dirty"].get(path) != after["dirty"].get(path):
            changes.append(path)
    return changes


class Writer:
    """The few writes `apply` makes in a repository: merge one branch into another, commit a removal, create
    a branch, push. Branches that aren't checked out are changed through plumbing, so nobody's checkout moves;
    a branch that is checked out is changed in its checkout, and only when that checkout is clean."""

    def __init__(self, repo: Repo):
        self.repo = repo

    def git(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        import os

        where = str(cwd or self.repo.path)
        return subprocess.run(
            ["git", "-C", where, *args], capture_output=True, text=True, env={**os.environ, **(env or {})}
        )

    def must(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> str:
        run = self.git(*args, cwd=cwd, env=env)
        if run.returncode:
            raise Fail(EXIT_ERROR, f"git {' '.join(args[:3])} failed in {self.repo.name}: {run.stderr.strip()}")
        return run.stdout.strip()

    def has_remote(self) -> bool:
        return self.git("remote", "get-url", "origin").returncode == 0

    def push(self, branch: str) -> None:
        if self.has_remote():
            self.must("push", "-q", "origin", f"refs/heads/{branch}:refs/heads/{branch}")

    def checkout_of(self, branch: str) -> Path | None:
        return self.repo.worktrees().get(branch)

    def clean(self, tree: Path) -> bool:
        return not self.git("status", "--porcelain", cwd=tree).stdout.strip()

    def set_branch(self, branch: str, sha: str, message: str) -> None:
        """Point `branch` at `sha`, which descends from its head: in its checkout if it has one."""
        tree = self.checkout_of(branch)
        if tree is None:
            self.must("update-ref", "-m", message, f"refs/heads/{branch}", sha)
        elif not self.clean(tree):
            raise Fail(EXIT_ERROR, f"{branch} is checked out with changes in {tree}; commit or stash them first")
        else:
            self.must("merge", "-q", "--ff-only", sha, cwd=tree)

    def create_branch(self, branch: str, start: str) -> None:
        self.must("branch", branch, start)
        self.push(branch)

    def merge(self, head: str, into: str, message: str) -> dict:
        """Merge `head` into the branch `into` with a merge commit. Returns {merged: sha}, {already: True}, or
        {conflict: [files]} with nothing changed."""
        repo = self.repo
        base = repo.fresh(into)
        head_ref = repo.fresh(head) or head
        if base is None:
            raise Fail(EXIT_ERROR, f"{repo.name}: no branch {into} to merge into")
        if repo.is_ancestor(head_ref, base):
            if base != into:  # the remote has it already; bring the local branch along
                self.set_branch(into, repo.sha(base), "fab0: fast-forward")
            return {"already": True}
        run = self.git("merge-tree", "--write-tree", "--name-only", "--no-messages", base, head_ref)
        if run.returncode == 1:
            files = [line for line in run.stdout.splitlines()[1:] if line]
            return {"conflict": files}
        if run.returncode:
            raise Fail(EXIT_ERROR, f"git merge-tree failed in {repo.name}: {run.stderr.strip()}")
        tree = run.stdout.splitlines()[0]
        sha = self.must("commit-tree", tree, "-p", repo.sha(base), "-p", repo.sha(head_ref), "-m", message)
        self.set_branch(into, sha, message)
        self.push(into)
        repo._fresh.clear()
        repo._trees.clear()
        return {"merged": sha}

    def commit(self, branch: str, message: str, remove: list[str] = (), allow_empty: bool = False) -> str | None:
        """A commit on `branch` that removes `remove`, made in a scratch index; None if there was nothing to do."""
        import tempfile

        repo = self.repo
        head = repo.fresh(branch)
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.must("read-tree", head, env=env)
            if remove:
                self.must("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *remove, env=env)
            tree = self.must("write-tree", env=env)
        if tree == self.must("rev-parse", f"{head}^{{tree}}") and not allow_empty:
            return None
        sha = self.must("commit-tree", tree, "-p", repo.sha(head), "-m", message)
        self.set_branch(branch, sha, message)
        self.push(branch)
        repo._fresh.clear()
        repo._trees.clear()
        return sha
