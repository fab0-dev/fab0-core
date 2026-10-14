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
