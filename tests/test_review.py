"""Review pull requests (MIGRATION.md, M3b; D80): a unit whose outputs are all in the run store is reviewed in a pull
request of the runs repository, on the story fixture with its spec and task list in the store."""

import json
from pathlib import Path

from conftest import STORY_GH, git
from test_run import SPEC, TASKS, finish, handouts, start_work, started, states, sync_children

RUNS = "acme/shop-runs"
BRANCH = "fab0/shop/412/spec"


def in_the_store(f) -> None:
    """The spec becomes a store artifact like the task list, and the spec step writes no repository."""
    f.edit("fab0.yaml", lambda d: d["store"].update(github=RUNS))

    def spec_artifact(d):
        d.pop("file")
        d.update(kind="store", store={"format": "markdown"})

    def spec_step(d):
        d.pop("paths")
        d["repos"] = {"read": ["all"]}
        d["inputs"] = ["intent", "reviews"]

    f.edit("artifacts/spec.yaml", spec_artifact)
    f.edit("steps/spec.yaml", spec_step)


def gh(f) -> dict:
    return json.loads(f.gh_path.read_text())


def review_pulls(f) -> list[dict]:
    return gh(f).get("pulls", {}).get(RUNS, [])


def change_pull(f, **fields) -> None:
    f.gh_edit(lambda d: d["pulls"][RUNS][-1].update(fields))


def add_review(f, state: str, user: str, body: str = "") -> None:
    def change(d):
        reviews = d["pulls"][RUNS][-1].setdefault("reviews", [])
        reviews.append({"id": len(reviews) + 1, "state": state, "user": user, "body": body})

    f.gh_edit(change)


def store_git(f, *args: str) -> str:
    return git(f.store_path(), *args)


def write_spec(f, spec: str = SPEC, tasks: str = TASKS) -> dict:
    """Hand out the spec, write its outputs in the outbox, and record them."""
    h = handouts(f.run("next", "412"))["spec"]
    trees = start_work(f, h) if h["attempt"] == 1 else {"home": Path(h["branches"][-1]["worktree"])}
    finish(f, "412", h, trees, {("out", "spec.md"): spec, ("out", "tasks.yaml"): tasks}, {"summary": "Wrote it."})
    return h


def person_edits(f, tmp: Path, text: str, who: str = "po-user") -> str:
    """A person pushes a commit to the review branch, as on GitHub; returns it."""
    tree = tmp / "edit"
    store_git(f, "worktree", "add", "-q", str(tree), BRANCH)
    (tree / "runs/shop/412/review/spec/spec.md").write_text(text)
    git(tree, "-c", f"user.name={who}", "commit", "-qam", "Tighten the scope")
    store_git(f, "worktree", "remove", str(tree))
    return store_git(f, "rev-parse", BRANCH)


def person_merges(f, who: str) -> None:
    """A person merges the review pull request: the branch into the store's main, and GitHub says who did."""
    store_git(f, "merge", "-q", "--no-ff", "-m", "Merge the review", BRANCH)
    change_pull(f, state="merged", merged_by=who)


def reviewed_story(factory):
    f = factory("story")
    in_the_store(f)
    f.init_git(STORY_GH)
    started(f)
    return f


def test_a_store_only_unit_is_reviewed_in_a_pull_request_of_the_runs_repository(factory, tmp_path):
    f = reviewed_story(factory)
    write_spec(f)
    assert states(f.run("status", "412"))["spec"] == ("waiting", {"gate": "human"})
    applied = f.run("apply", "412")["applied"]
    [pr] = review_pulls(f)
    assert {"unit": "spec", "pull": f"opened {RUNS}#{pr['number']}"} in applied
    assert (pr["branch"], pr["base"], pr["draft"]) == (BRANCH, "main", False)
    assert "runs/shop/412/review/spec/" in pr["body"] and "someone in product-owner merges" in pr["body"]
    first = store_git(f, "rev-parse", BRANCH)
    assert store_git(f, "show", f"{first}:runs/shop/412/review/spec/spec.md") == SPEC.strip()
    assert store_git(f, "show", f"{first}:runs/shop/412/review/spec/tasks.yaml") == TASKS.strip()
    assert "review" not in store_git(f, "ls-tree", "--name-only", "HEAD:runs/shop/412"), "main has no copy yet"

    # a Comment review brings a new attempt, with the review and the latest version in its brief
    add_review(f, "COMMENTED", "po-user", "Say what happens on the last night.")
    assert states(f.run("status", "412"))["spec"] == ("ready", {"rework": ["stale"]})
    h = write_spec(f, spec=SPEC + "- S2: the last night\n")
    assert h["attempt"] == 2 and "Say what happens on the last night." in h["brief"]
    assert (
        "Attempt 1 is under Outputs below" in h["brief"] and "Its latest version, `spec` in the run store" in h["brief"]
    )
    applied = f.run("apply", "412")["applied"]
    assert {"unit": "spec", "pull": f"put attempt 2 on {BRANCH}"} in applied and len(review_pulls(f)) == 1
    assert store_git(f, "rev-parse", f"{BRANCH}^") == first, "one commit per attempt, on the same pull request"

    # a person's edit on the branch is recorded as an attempt of that person
    change_pull(f, author="po-user")
    edited = person_edits(f, tmp_path, SPEC + "- S2: the last night, checked out by noon\n")
    applied = f.run("apply", "412")["applied"]
    assert {"unit": "spec", "edit": "recorded po-user's edit as attempt 3"} in applied
    fact = f.fact("412", "spec-3")
    assert (fact["executor"], fact["actor"], fact["edited"]) == (
        "person",
        "po-user",
        {"commit": edited, "pr": f"{RUNS}#{pr['number']}"},
    )
    assert fact["verify"] == {"spec": "pass", "tasks": "pass"} and fact["summary"].startswith("po-user edited spec.md")
    assert fact["outputs"]["tasks"] == f.fact("412", "spec-2")["outputs"]["tasks"]
    assert store_git(f, "rev-parse", BRANCH) == edited, "the edit is the latest version: nothing to put on top"
    again = [a for a in f.run("apply", "412")["applied"] if a.get("unit") == "spec"]
    assert again == [], "the edit is recorded once, and nothing goes on top of it"
    assert states(f.run("status", "412"))["spec"] == ("waiting", {"gate": "human"})

    # the owner's merge passes the gate, and the task list fans out from its object
    person_merges(f, "po-user")
    status = f.run("status", "412")
    assert states(status)["spec"] == ("done", None)
    sync_children(f, TASKS)
    t1 = handouts(f.run("next", "412"))["implement-T1"]
    assert "checked out by noon" in t1["brief"], "the implementation reads the person's version"
    finish(f, "412", t1, start_work(f, t1), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})

    # the product repositories receive only the implementation's changes
    for repo, expected in (("home", set()), ("backend", {"src/a.py"})):
        files = set(git(f.repo(repo), "log", "--all", "--name-only", "--format=").split())
        assert files - {"README.md"} - {p for p in files if p.startswith((".fab0/", ".claude/"))} == expected, repo


def test_only_the_gates_owner_approves_by_merging(factory):
    f = reviewed_story(factory)
    write_spec(f)
    f.run("apply", "412")
    [pr] = review_pulls(f)
    person_merges(f, "be-user")
    reason = {"gate": "human", "merged": "by be-user, who isn't product-owner"}
    assert states(f.run("status", "412"))["spec"] == ("waiting", reason)
    applied = f.run("apply", "412")["applied"]
    assert {"unit": "spec", "pull": f"commented on {RUNS}#{pr['number']}"} in applied
    [note] = review_pulls(f)[0]["comments"]
    assert note["body"].startswith("<!-- fab0:merged spec 1 -->") and "@po-user" in note["body"]
    assert len(review_pulls(f)) == 1, "the outputs are merged: no new pull request"
    assert not any("commented" in str(a) for a in f.run("apply", "412")["applied"]), "said once"
    add_review(f, "APPROVED", "be-user")
    assert states(f.run("status", "412"))["spec"] == ("waiting", reason)
    add_review(f, "APPROVED", "po-user")
    assert states(f.run("status", "412"))["spec"] == ("done", None)


def test_a_merge_of_an_older_attempt_doesnt_pass_the_newer_one(factory):
    f = reviewed_story(factory)
    write_spec(f)
    f.run("apply", "412")
    person_merges(f, "po-user")
    assert states(f.run("status", "412"))["spec"] == ("done", None)
    f.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(body="Guests extend a booking twice."))
    write_spec(f, spec=SPEC + "- S2: twice\n")
    assert states(f.run("status", "412"))["spec"] == ("waiting", {"gate": "human"})
    applied = f.run("apply", "412")["applied"]
    second = review_pulls(f)[-1]
    assert len(review_pulls(f)) == 2 and {"unit": "spec", "pull": f"opened {RUNS}#{second['number']}"} in applied
    assert "S2: twice" in store_git(f, "diff", "main", BRANCH), "the new pull request shows what changed"


def test_validate_asks_for_the_runs_repository_on_github(factory):
    f = factory("story")
    in_the_store(f)
    f.edit("fab0.yaml", lambda d: d["store"].pop("github"))
    f.init_git(STORY_GH)
    res = f.run("validate", expect=3)
    assert any("set store.github" in e["message"] for e in res["errors"])
