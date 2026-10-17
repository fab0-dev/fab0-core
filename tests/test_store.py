"""The run store's git backend (MIGRATION.md, M1): objects, create-only facts, and two clones racing."""

import threading
from pathlib import Path

import pytest
from conftest import git
from fab0_engine.errors import Fail
from fab0_engine.hashing import sha
from fab0_engine.store import GitStore, default_project, open_store, remote_url


def configure(path: Path) -> None:
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "runs.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    return bare


def clone(tmp_path: Path, origin: Path, name: str) -> GitStore:
    store = GitStore(tmp_path / name, "shop", url=str(origin))
    store.ensure()
    configure(store.path)
    return store


def commits(store: GitStore) -> int:
    return int(git(store.path, "rev-list", "--count", "HEAD"))


def test_put_twice_keeps_one_object(tmp_path, origin):
    store = clone(tmp_path, origin, "a")
    first = store.put("412", b"- id: T1\n", "yaml")
    assert store.put("412", b"- id: T1\n", "yaml") == first == sha(b"- id: T1\n")
    assert commits(store) == 1
    assert [p.name for p in (store.path / "runs/shop/412/objects").iterdir()] == [f"sha256-{first[7:]}.yaml"]
    assert store.get("412", first) == b"- id: T1\n"


def test_a_tampered_object_fails_its_read(tmp_path, origin):
    store = clone(tmp_path, origin, "a")
    digest = store.put("412", b"spec", "md")
    (store.path / f"runs/shop/412/objects/sha256-{digest[7:]}.md").write_bytes(b"other")
    with pytest.raises(Fail, match="doesn't match its hash"):
        store.get("412", digest)
    with pytest.raises(Fail, match="doesn't match its hash"):
        store.put("412", b"spec", "md")
    with pytest.raises(Fail, match="no object"):
        store.get("412", sha(b"never"))


def test_a_fact_is_written_once(tmp_path, origin):
    store = clone(tmp_path, origin, "a")
    assert store.append("412", "spec-1", {"step": "spec", "attempt": 1}) == "ok"
    assert store.append("412", "spec-1", {"step": "spec", "attempt": 9}) == "exists"
    assert store.facts("412") == {"spec-1": {"step": "spec", "attempt": 1}}
    assert store.runs() == ["412"]
    assert store.facts("413") == {}


@pytest.mark.parametrize("run, name", [("../412", "spec-1"), ("412", "a/b"), ("412", "")])
def test_names_are_one_path_segment(tmp_path, origin, run, name):
    store = clone(tmp_path, origin, "a")
    with pytest.raises(Fail):
        store.append(run, name, {})


def test_two_clones_writing_one_fact_get_ok_and_exists(tmp_path, origin):
    a, b = clone(tmp_path, origin, "a"), clone(tmp_path, origin, "b")
    assert a.append("412", "implement-T1-1-claim", {"actor": "alice"}) == "ok"
    assert b.append("412", "implement-T1-1-claim", {"actor": "bob"}) == "ok"  # b hasn't seen a's yet
    b.put("412", b"bob's object", "txt")
    assert a.sync("push") == []
    assert b.sync("push") == [("412", "implement-T1-1-claim")]
    assert b.facts("412")["implement-T1-1-claim"] == {"actor": "alice"}  # the remote's fact stands
    assert b.get("412", sha(b"bob's object")) == b"bob's object"  # b's other writes still went out
    a.sync("pull")
    assert a.get("412", sha(b"bob's object")) == b"bob's object"


def test_a_push_that_loses_a_race_brings_its_writes_on_top(tmp_path, origin):
    a, b = clone(tmp_path, origin, "a"), clone(tmp_path, origin, "b")
    a.append("412", "spec-1", {"by": "a"})
    b.append("412", "implement-T1-1", {"by": "b"})
    b.put("412", b"same", "txt")
    a.put("412", b"same", "txt")  # the same object from both sides doesn't conflict
    assert a.sync("push") == []
    assert b.sync("push") == []
    a.sync("pull")
    assert set(a.facts("412")) == set(b.facts("412")) == {"spec-1", "implement-T1-1"}
    assert git(b.path, "rev-parse", "HEAD") == git(b.path, "rev-parse", "origin/main")


def test_the_first_write_into_an_empty_remote(tmp_path, origin):
    a = clone(tmp_path, origin, "a")
    assert a.sync("pull") == [] and a.sync("push") == []  # nothing yet, nothing to do
    a.append("412", "spec-1", {})
    assert a.sync("push") == []
    assert clone(tmp_path, origin, "b").facts("412") == {"spec-1": {}}


def test_parallel_writers_on_one_machine(tmp_path, origin):
    a = clone(tmp_path, origin, "a")
    results = []

    def write(n: int) -> None:
        results.append(GitStore(a.path, "shop").append("412", f"implement-T{n}-1", {"n": n}))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["ok"] * 8
    assert len(a.facts("412")) == 8 and commits(a) == 8


def test_a_local_store_needs_no_remote(tmp_path):
    store = GitStore(tmp_path / "runs", "shop")
    store.ensure()
    configure(store.path)
    store.append("412", "spec-1", {})
    assert store.sync("push") == [] and store.sync("pull") == []
    assert store.runs() == ["412"]


def test_projects_share_one_runs_repository(tmp_path, origin):
    shop = clone(tmp_path, origin, "a")
    docs = GitStore(shop.path, "docs")
    shop.append("8", "spec-1", {"of": "shop"})
    docs.append("8", "brief-1", {"of": "docs"})
    assert shop.facts("8") == {"spec-1": {"of": "shop"}} and docs.facts("8") == {"brief-1": {"of": "docs"}}


def test_the_store_block(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    git(home, "init", "-q", "-b", "main")
    git(home, "remote", "add", "origin", "git@github.com:acme/Shop.App.git")
    project = {"repos": {"home": {"path": ".", "github": "acme/Shop.App"}}}
    assert open_store(project, home) is None
    store = open_store({**project, "store": {"github": "acme/runs"}}, home)
    assert store.path.resolve() == (home / ".git" / "fab0" / "store").resolve()
    assert store.url == "git@github.com:acme/runs.git" and store.project == "shop-app"
    store = open_store({**project, "store": {"path": "../runs", "project": "docs"}}, home)
    assert store.path == (tmp_path / "runs").resolve() and store.url is None and store.project == "docs"
    assert default_project({"repos": {"home": {"github": "fab0-dev/docs"}}}) == "docs"
    assert remote_url("https://github.com/acme/shop.git", "acme/runs") == "https://github.com/acme/runs.git"
    assert remote_url(None, "acme/runs") == "https://github.com/acme/runs.git"


def test_a_review_branch_takes_each_version_and_leaves_main_alone(tmp_path, origin):
    store = clone(tmp_path, origin, "a")
    store.append("412", "spec-1", {"step": "spec", "attempt": 1})
    store.sync("push")
    main = git(store.path, "rev-parse", "HEAD")
    first = store.review("412", "spec", {"spec.md": b"# One\n", "tasks.yaml": b"[]\n"}, "spec attempt 1")
    assert store.review("412", "spec", {"spec.md": b"# One\n", "tasks.yaml": b"[]\n"}, "again") is None
    assert git(store.path, "rev-parse", "HEAD") == main and not (store.path / "runs/shop/412/review").exists()
    branch = "fab0/shop/412/spec"
    assert git(origin, "rev-parse", branch) == first == store.review_head("412", "spec")
    assert store.by_fab0(first) and git(origin, "rev-parse", f"{first}^") == main, "a new branch starts from main"

    other = clone(tmp_path, origin, "b")  # a person edits the spec on the branch, from their own clone
    git(other.path, "checkout", "-q", branch)
    (other.path / "runs/shop/412/review/spec/spec.md").write_text("# One, edited\n")
    git(other.path, "commit", "-qam", "Edit the spec")
    git(other.path, "push", "-q", "origin", branch)
    edited = git(other.path, "rev-parse", "HEAD")
    assert not other.by_fab0(edited) and other.author(edited) == "test"

    second = store.review("412", "spec", {"spec.md": b"# Two\n"}, "spec attempt 2")
    assert git(origin, "rev-parse", f"{second}^") == edited, "built on the person's commit, never forced"
    assert store.review_files("412", "spec", second) == {"spec.md": b"# Two\n"}, "a dropped output goes"
    assert store.review_files("412", "spec", edited)["spec.md"] == b"# One, edited\n"
    assert store.review_files("412", "spec") == {}, "main has no review copy until the pull request merges"
