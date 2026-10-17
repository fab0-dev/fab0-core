"""The writer's push brings a branch forward to origin's when origin has it all, and pushes otherwise."""

import subprocess
from pathlib import Path

import pytest

from conftest import git
from fab0_engine.errors import Fail
from fab0_engine.git import Repo, Writer


def commit(path: Path, name: str) -> str:
    (path / name).write_text(name)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", name)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    """A local repo on main with origin, a second clone (`other`) standing for GitHub's other writers, and `feat`
    on origin and locally at the same commit."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    local, other = tmp_path / "local", tmp_path / "other"
    local.mkdir()
    git(local, "init", "-q", "-b", "main")
    for key, val in (("user.email", "t@example.com"), ("user.name", "t")):
        git(local, "config", key, val)
    commit(local, "a")
    git(local, "remote", "add", "origin", str(bare))
    git(local, "branch", "feat")
    git(local, "push", "-q", "origin", "main", "feat")
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    for key, val in (("user.email", "t@example.com"), ("user.name", "t")):
        git(other, "config", key, val)
    git(other, "checkout", "-q", "feat")
    repo = Repo("core", {"github": "acme/core", "path": "local"}, tmp_path, "main")
    return local, other, bare, Writer(repo)


def ahead_on_origin(local, other):
    new = commit(other, "b")
    git(other, "push", "-q", "origin", "feat")
    git(local, "fetch", "-q", "origin")
    return new


def test_s1_s2_behind_branch_not_checked_out_moves_without_push(world):
    local, other, bare, writer = world
    new = ahead_on_origin(local, other)
    writer.push("feat")
    assert git(local, "rev-parse", "feat") == new
    assert git(bare, "rev-parse", "feat") == new


def test_s3_local_ahead_is_pushed(world):
    local, _, bare, writer = world
    git(local, "checkout", "-q", "feat")
    new = commit(local, "c")
    writer.push("feat")
    assert git(bare, "rev-parse", "feat") == new


def test_s4_same_commit_moves_nothing(world):
    local, _, bare, writer = world
    before = git(bare, "rev-parse", "feat")
    writer.push("feat")
    assert git(bare, "rev-parse", "feat") == before == git(local, "rev-parse", "feat")


def test_s5_diverged_fails_naming_repo_and_branch(world):
    local, other, bare, writer = world
    theirs = ahead_on_origin(local, other)
    git(local, "checkout", "-q", "feat")
    mine = commit(local, "c")
    with pytest.raises(Fail) as err:
        writer.push("feat")
    assert "core" in str(err.value) and "feat" in str(err.value)
    assert git(local, "rev-parse", "feat") == mine
    assert git(bare, "rev-parse", "feat") == theirs


def test_s6_clean_checkout_is_fast_forwarded(world):
    local, other, _, writer = world
    git(local, "checkout", "-q", "feat")
    new = ahead_on_origin(local, other)
    writer.push("feat")
    assert git(local, "rev-parse", "HEAD") == git(local, "rev-parse", "feat") == new
    assert (local / "b").exists()


def test_s7_checkout_with_changes_is_left_alone(world):
    local, other, bare, writer = world
    git(local, "checkout", "-q", "feat")
    old = git(local, "rev-parse", "HEAD")
    new = ahead_on_origin(local, other)
    (local / "a").write_text("edited")
    writer.push("feat")
    assert git(local, "rev-parse", "HEAD") == old
    assert (local / "a").read_text() == "edited"
    assert git(local, "status", "--porcelain") == "M a"
    assert git(bare, "rev-parse", "feat") == new


def test_s8_no_origin_does_nothing(world):
    local, _, _, writer = world
    git(local, "remote", "remove", "origin")
    before = git(local, "rev-parse", "feat")
    writer.push("feat")
    assert git(local, "rev-parse", "feat") == before
