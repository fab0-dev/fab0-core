"""Unit tests of the resolver's pure functions."""


def task(tid, repo, files, depends_on=()):
    return {"id": tid, "title": tid, "repo": repo, "files": list(files), "depends_on": list(depends_on)}


def test_lanes_group_tasks_that_share_files_in_one_repo(resolver):
    tasks = [
        task("T1", "backend", ["src/a.py"]),
        task("T2", "backend", ["src/b.py"]),
        task("T3", "backend", ["src/a.py", "src/c.py"]),
        task("T4", "web", ["src/a.py"]),
    ]
    assert resolver.lanes(tasks) == [["T1", "T3"], ["T2"], ["T4"]]


def test_a_directory_covers_its_files(resolver):
    tasks = [task("T1", "backend", ["src/"]), task("T2", "backend", ["src/x/y.py"]), task("T3", "backend", ["srcx"])]
    assert resolver.lanes(tasks) == [["T1", "T2"], ["T3"]]


def test_ordered_tasks_share_a_lane(resolver):
    tasks = [task("T1", "backend", ["a"]), task("T2", "web", ["b"], depends_on=["T1"])]
    assert resolver.lanes(tasks) == [["T1", "T2"]]


def test_task_rules(resolver):
    repos = {"home": {}, "backend": {}}
    assert resolver.task_rules([task("T1", "home", ["a"]), task("T2", "backend", ["b"], ["T1"])], repos) == []
    errors = resolver.task_rules(
        [
            task("T1", "home", ["a"], ["T3"]),
            task("T1", "home", ["a"]),
            task("T2", "mobile", ["b"], ["T9"]),
            task("T3", "home", ["c"], ["T1"]),
        ],
        repos,
    )
    assert "duplicate task ids: T1" in errors
    assert "T2: repo 'mobile' is not in fab0.yaml" in errors
    assert "T2: depends_on T9, which is not a task" in errors
    assert any(e.startswith("depends_on has a cycle: T1 → T3 → T1") for e in errors)


def test_merge_patch_follows_rfc_7396(resolver):
    target = {"a": 1, "b": {"c": 2, "d": 3}}
    assert resolver.merge_patch(target, {"b": {"c": None, "e": 4}, "f": [1]}) == {
        "a": 1,
        "b": {"d": 3, "e": 4},
        "f": [1],
    }
    assert target == {"a": 1, "b": {"c": 2, "d": 3}}


def test_hashes_are_stable_and_unpadded_names(resolver):
    assert resolver.sha_content(b"x") == resolver.sha(b"x")
    assert resolver.sha_content({"b": b"2", "a": b"1"}) == resolver.sha_content({"a": b"1", "b": b"2"})
    assert resolver.sha_text("a\r\nb") == resolver.sha_text("a\nb")
    entry = {"stage": "implement", "task": "T12", "attempt": 10, "gate": {"type": "agent", "decision": "approve"}}
    assert resolver.entry_name(entry) == "implement-T12-10-gate.yaml"


def test_slugs(resolver):
    assert resolver.slugify("Booking extension: one night!") == "booking-extension-one-night"
    assert resolver.slugify("!!!") == "item"
    assert len(resolver.slugify("x" * 100)) == 40
