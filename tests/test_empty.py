"""No empty commits and no empty pull requests (MIGRATION.md, M4; D81, D82): a claim is a fact in the run store, a
unit's branch starts with no commit, and a run pull request opens only where the run changed something."""

import json
import subprocess
import sys
import threading

from conftest import SCRIPT, STORY_GH, Factory, git
from fab0_engine.handout import claim
from fab0_engine.schedule import Run
from fab0_engine.tracker import make_tracker
from fab0_engine.wiring import Wiring
from test_conflicts import remotes
from test_gates import record_gate, verdict
from test_review import in_the_store, person_merges, write_spec
from test_run import finish, handouts, start_work, states

ONE_TASK = "- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n"


def gh(f) -> dict:
    return json.loads(f.gh_path.read_text())


def empty_commits(path) -> list[str]:
    """Every commit, on any branch, that changes nothing: one parent, and its parent's tree."""
    found = []
    for line in git(path, "rev-list", "--all", "--parents").splitlines():
        commit, *parents = line.split()
        if len(parents) == 1 and git(path, "rev-parse", f"{commit}^{{tree}}") == git(
            path, "rev-parse", f"{parents[0]}^{{tree}}"
        ):
            found.append(git(path, "log", "-1", "--format=%s", commit))
    return found


def test_a_run_that_only_reads_a_repository_leaves_no_trace_in_it(factory):
    """The spec and the task list are in the store, and the one task is in the backend: the home repository is
    read and never written."""
    f = factory("story")
    in_the_store(f)
    f.init_git(STORY_GH)
    res = f.run("apply", "412")
    assert [a["action"] for a in res["applied"]] == ["git.run-branch", "tracker.field"]
    write_spec(f, tasks=ONE_TASK)
    f.run("apply", "412")  # the spec's review pull request, in the runs repository
    person_merges(f, "po-user")
    f.run("apply", "412")  # the task's sub-issue
    t1 = handouts(f.run("next", "412"))["implement-T1"]
    finish(f, "412", t1, start_work(f, t1), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})
    record_gate(f, "412", "implement-T1", verdict("approve"))
    end = f.run("apply", "412")
    assert states(end)["implement-T1"] == ("done", None) and end["kind"] == "waiting"

    for repo in ("home", "backend"):
        assert empty_commits(f.repo(repo)) == [], repo
    home = f.repo("home")
    assert git(home, "rev-parse", "fab0/412/base") == git(home, "rev-parse", "main"), "home got nothing"
    assert gh(f)["pulls"].get("acme/shop", []) == [], "no pull request in the repository the run only read"
    [run_pull] = [p for p in gh(f)["pulls"]["acme/shop-api"] if p["branch"] == "fab0/412/base"]
    assert run_pull["base"] == "main" and run_pull["body"].endswith("Closes acme/shop#412"), "it closes the run's item"
    assert not run_pull["draft"], "pr.ready marked it ready at the run's end"


def another_machine(f: Factory, where) -> Factory:
    """A second machine: its own clones of the project's repositories and of the run store, from their remotes, on
    the same tracker."""
    other = Factory.__new__(Factory)
    other.home, other.gh_path = where / f.home.name, f.gh_path
    remotes_dir = f.repo("home").parent / "remotes"
    for path in [f.repo(name) for name in f.read("fab0.yaml")["repos"]] + [f.store_path()]:
        target = where / path.name
        git(where.parent, "clone", "-q", str(remotes_dir / f"{path.name}.git"), str(target))
        git(target, "config", "user.email", "b@example.com")
        git(target, "config", "user.name", "b")
    return other


def test_two_clones_racing_for_one_unit_give_one_hand_out(story, tmp_path):
    remotes(story)
    story.run("apply", "412")
    other = another_machine(story, tmp_path / "b")
    results: dict[str, dict] = {}

    def take(name: str, f: Factory) -> None:
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "next", "412"], cwd=f.home, capture_output=True, text=True, env=f.env()
        )
        results[name] = json.loads(done.stdout)

    racers = [threading.Thread(target=take, args=(n, f)) for n, f in (("a", story), ("b", other))]
    for t in racers:
        t.start()
    for t in racers:
        t.join()
    taken = [n for n, res in results.items() if any(h["unit"] == "spec" for h in res["handouts"])]
    assert len(taken) == 1, results
    loser = "b" if taken == ["a"] else "a"
    assert states(results[loser])["spec"] == ("running", {"claimed": "elsewhere"})
    runs = story.repo("home").parent / "remotes" / "runs.git"
    assert "spec-1-claim.yaml" in git(runs, "ls-tree", "-r", "--name-only", "main")


def test_a_claim_pushed_first_elsewhere_wins_over_one_written_here(story, tmp_path, monkeypatch):
    """The race at its narrowest: the other machine pulled the store before this one's claim was pushed, so it
    writes its own claim, and only its push finds out."""
    remotes(story)
    story.run("apply", "412")
    other = another_machine(story, tmp_path / "b")
    assert [h["unit"] for h in story.run("next", "412")["handouts"]] == ["spec"]  # pushed its claim
    monkeypatch.setenv("FAB0_GH_FAKE", str(story.gh_path))
    run = Run(Wiring(other.home), "story", "412", "be-user", make_tracker())
    run.evaluate()  # from its own copy of the store, which hasn't seen the claim
    spec = next(u for u in run.units if u.id == "spec")
    assert spec.state == "ready"
    assert claim(run, spec) is False
    assert (tmp_path / "b" / "runs" / "runs/shop/412/ledger/spec-1-claim.yaml").read_text().startswith("actor: po-user")
    assert not git(other.repo("home"), "branch", "--list", "fab0/412/spec"), "it made no branch"
