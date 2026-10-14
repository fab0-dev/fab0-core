"""Conflicts, failing checks and claims: what keeps parallel work from colliding, across units, runs and machines."""

import json
from pathlib import Path

from conftest import git
from test_apply import implement, spec_done
from test_gates import record_gate, verdict
from test_run import finish, handouts, states

TWO_TASKS = (
    "- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n"
    "- {id: T5, title: Pricing, repo: backend, files: [src/b.py]}\n"
)


def ready_tasks(f, tasks=None) -> None:
    spec_done(f, **({"tasks": tasks} if tasks else {}))
    f.merge("home", "fab0/412/spec", "fab0/412/base")
    f.run("apply", "412")


def commit_on(f, repo: str, branch: str, files: dict, message: str = "someone else") -> None:
    path = f.repo(repo)
    here = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    git(path, "checkout", "-q", branch)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    git(path, "checkout", "-q", here)


def test_a_conflict_goes_back_to_the_producer_then_to_a_person(story):
    ready_tasks(story)
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    commit_on(story, "backend", "fab0/412/base", {"src/a.py": "x = 2\n"})
    head = git(story.repo("backend"), "rev-parse", "fab0/412/base")
    res = story.run("next", "412")
    assert states(res)["implement-T1"] == ("ready", {"rework": ["conflict"]})
    assert git(story.repo("backend"), "rev-parse", "fab0/412/base") == head, "nothing merged"
    h = handouts(res)["implement-T1"]
    own = h["branches"][-1]
    assert h["attempt"] == 2 and own["sync"] and own["conflicts"] == ["src/a.py"]
    assert "Your branch conflicts with the branch it merges into, in backend: src/a.py." in h["brief"]
    tree = Path(own["worktree"])
    assert git(tree, "merge", "-q", "fab0/412/base", check=False) is not None  # leaves conflict markers
    (tree / "src" / "a.py").write_text("x = 3\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "--no-edit")
    res = finish(story, "412", h, {"backend": tree}, returned={"summary": "Resolved: three."})
    assert res["recorded"]["rework"] == ["conflict"] and res["failed"] == [], "the merge brought nothing unclaimed"
    record_gate(story, "412", "implement-T1", verdict("approve"))
    commit_on(story, "backend", "fab0/412/base", {"src/a.py": "x = 4\n"}, "someone else, again")
    assert states(story.run("status", "412"))["implement-T1"] == (
        "waiting",
        {"escalated": "its branch conflicts with its parent again"},
    )


def test_failing_checks_go_back_once(story):
    ready_tasks(story)
    implement(story)
    story.run("apply", "412")  # its draft pull request
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(checks="fail", failed_checks=["unit tests"]))
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("next", "412")
    assert states(res)["implement-T1"] == ("ready", {"rework": ["checks"]})
    h = handouts(res)["implement-T1"]
    assert "The required checks of acme/shop-api#2 fail: unit tests." in h["brief"]
    tree = Path(h["branches"][-1]["worktree"])
    finish(story, "412", h, {"backend": tree}, {("backend", "src/a.py"): "x = 1  # fixed\n"}, {"summary": "Fixed."})
    record_gate(story, "412", "implement-T1", verdict("approve"))
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"escalated": "its checks fail again"})


def test_apply_brings_a_reworked_branch_up_to_date(story):
    ready_tasks(story, TWO_TASKS)
    t5 = handouts(story.run("next", "412", "--unit", "implement-T5"))["implement-T5"]
    from test_run import start_work

    finish(story, "412", t5, start_work(story, t5), {("backend", "src/b.py"): "y = 1\n"}, {"summary": "T5."})
    record_gate(story, "412", "implement-T5", verdict("approve"))
    implement(story)
    story.run("apply", "412")  # merges T5; opens T1's draft
    review = {"id": 1, "state": "CHANGES_REQUESTED", "user": "be-user", "body": "Name it better."}
    story.gh_edit(
        lambda d: (
            next(p for p in d["pulls"]["acme/shop-api"] if p["branch"].endswith("T1"))
            .setdefault("reviews", [])
            .append(review)
        )
    )
    res = story.run("apply", "412")
    synced = [a for a in res["applied"] if "sync" in a]
    assert synced == [{"unit": "implement-T1", "sync": "merged fab0/412/base into fab0/412/implement-T1 in backend"}]
    assert git(story.repo("backend"), "show", "fab0/412/implement-T1:src/b.py") == "y = 1"
    h = handouts(story.run("next", "412", "--unit", "implement-T1"))["implement-T1"]
    assert "sync" not in h["branches"][-1], "already up to date"


def remotes(f) -> None:
    """A bare `origin` for every repository, as GitHub would be."""
    for name in f.read("fab0.yaml")["repos"]:
        path = f.repo(name)
        bare = path.parent / "remotes" / f"{path.name}.git"
        git(path.parent, "init", "-q", "--bare", str(bare))
        git(path, "remote", "add", "origin", str(bare))
        git(path, "push", "-q", "origin", "--all")


def test_a_unit_claimed_on_another_machine_isnt_handed_out(story):
    remotes(story)
    story.run("apply", "412")
    other = story.repo("home").parent / "elsewhere"
    git(story.repo("home").parent, "clone", "-q", str(story.repo("home").parent / "remotes" / "story.git"), str(other))
    git(other, "push", "-q", "origin", "origin/fab0/412/base:refs/heads/fab0/412/spec")
    res = story.run("next", "412")
    assert states(res)["spec"] == ("running", {"claimed": "elsewhere"}) and res["handouts"] == []


def test_next_claims_a_unit_on_the_remote(story):
    remotes(story)
    story.run("apply", "412")
    res = story.run("next", "412")
    assert [h["unit"] for h in res["handouts"]] == ["spec"]
    remote = story.repo("home").parent / "remotes" / "story.git"
    assert git(remote, "rev-parse", "--verify", "refs/heads/fab0/412/spec")
    assert handouts(story.run("next", "412"))["spec"], "the same machine keeps its claim"


def other_run_claims(f, run: str, unit: str, claims: list) -> None:
    """Another run's unit branch, unmerged, whose ledger entry claims `claims`."""
    entry = {
        "step": "implement",
        "unit": unit.rsplit("-", 1)[1],
        "attempt": 1,
        "executor": "agent",
        "actor": "x",
        "branch": f"fab0/{run}/{unit}",
        "base": {"branch": f"fab0/{run}/base", "sha": "0" * 40},
        "claims": claims,
        "gate": {"type": "auto"},
    }
    path = f.repo("backend")
    git(path, "branch", f"fab0/{run}/{unit}", "main")
    commit_on(
        f,
        "backend",
        f"fab0/{run}/{unit}",
        {f"work/{run}/ledger/{unit}-1.yaml": json.dumps(entry), "src/a.py": "x = 9\n"},
        f"{unit} of run {run}",
    )


def test_claims_of_another_open_run_warn(story):
    other_run_claims(story, "413", "implement-T1", ["backend:src/a.py"])
    ready_tasks(story)
    unit = next(u for u in story.run("status", "412")["units"] if u["unit"] == "implement-T1")
    assert unit["state"] == "ready" and unit["warnings"] == ["claims overlap with run 413, implement-T1"]


def test_claims_of_another_open_run_can_block(story):
    story.edit("fab0.yaml", lambda d: d["parallel"].update(cross_run="block"))
    other_run_claims(story, "413", "implement-T1", ["backend:src/**"])
    ready_tasks(story)
    assert states(story.run("status", "412"))["implement-T1"] == ("blocked", {"cross-run": ["run 413, implement-T1"]})
    story.merge("backend", "fab0/413/implement-T1", "main")
    assert states(story.run("status", "412"))["implement-T1"] == ("ready", None), "merged into the base: no claim"


def test_writes_beside_the_repositories_are_reported(story):
    story.run("apply", "412")
    h = handouts(story.run("next", "412"))["spec"]
    from test_run import start_work

    trees = start_work(story, h)
    stray = story.repo("home").parent / "spec-copy.md"
    stray.write_text("written outside every repository\n")
    res = finish(story, "412", h, trees, {("home", "specs/412.md"): "# x\n"}, {"summary": "Spec."})
    assert str(stray) in res["outside"]
