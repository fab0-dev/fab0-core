"""One repository of the workspace, read through git. The engine reads refs and trees; it writes only files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import EXIT_ERROR, Fail


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
