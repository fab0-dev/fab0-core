"""apply: the engine performs actions, merges units and keeps their pull requests, and doing it twice changes
nothing the second time."""

import json

from conftest import git
from test_gates import record_gate, verdict
from test_run import SPEC, actions, finish, handouts, start_work, states

ONE_TASK = "- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n"


def gh(f) -> dict:
    return json.loads(f.gh_path.read_text())


def did(res: dict) -> list:
    return res["applied"]


def test_apply_starts_a_run_and_a_second_apply_changes_nothing(story):
    res = story.run("apply", "412")
    assert [(a["unit"], a["action"]) for a in res["applied"]] == [
        ("onStart-1", "git.run-branch"),
        ("onStart-2", "pr.open-run"),
        ("onStart-3", "tracker.field"),
    ]
    assert res["applied"][0]["did"] == "created fab0/412/base in home, backend"
    pull = gh(story)["pulls"]["acme/shop"][0]
    assert pull["branch"] == "fab0/412/base" and pull["base"] == "main" and pull["draft"]
    assert pull["title"] == "story run 412: Extend a booking" and pull["body"].endswith("Closes #412")
    assert gh(story)["issues"]["acme/shop#412"]["fields"] == {"State": "Started"}
    log = git(story.repo("home"), "log", "--format=%s", "fab0/412/base")
    assert log.splitlines()[0] == "fab0: start run 412 (story)", "an empty commit, so the pull request can open"
    assert states(res)["spec"] == ("ready", None)
    again = story.run("apply", "412")
    assert again["applied"] == []


def spec_done(f, tasks=ONE_TASK, spec=SPEC):
    f.run("apply", "412")
    h = handouts(f.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): spec, ("home", "work/412/tasks.yaml"): tasks}
    finish(f, "412", h, start_work(f, h), files, {"summary": "Wrote the spec."})


def test_a_human_gate_gets_a_ready_pull_request(story):
    spec_done(story)
    res = story.run("apply", "412")
    assert did(res) == [{"unit": "spec", "pull": "opened acme/shop#2"}]
    pull = gh(story)["pulls"]["acme/shop"][1]
    assert (pull["branch"], pull["base"], pull["draft"]) == ("fab0/412/spec", "fab0/412/base", False)
    assert pull["body"].startswith("Wrote the spec.")
    assert story.run("apply", "412")["applied"] == []


def test_after_the_merge_apply_makes_the_sub_issues(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    res = story.run("apply", "412")
    assert did(res) == [{"unit": "tasks--onStart-1", "action": "tracker.child.sync", "did": "created #413"}]
    data = gh(story)
    assert data["issues"]["acme/shop#413"]["title"] == "Booking API"
    assert data["issues"]["acme/shop#413"]["type"] == "Task"
    assert data["issues"]["acme/shop#412"]["sub_issues"] == ["acme/shop#413"]
    assert states(res)["implement-T1"] == ("ready", None)
    assert story.run("apply", "412")["applied"] == []


def implement(f) -> dict:
    h = handouts(f.run("next", "412", "--unit", "implement-T1"))["implement-T1"]
    finish(f, "412", h, start_work(f, h), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Implemented."})
    return h


def test_an_auto_gate_merges_after_approval_and_its_checks(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    res = story.run("apply", "412")
    assert did(res) == [{"unit": "implement-T1", "pull": "opened acme/shop-api#2"}]
    assert gh(story)["pulls"]["acme/shop-api"][0]["draft"], "a draft until its gate approves"
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(checks="pending"))
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("apply", "412")
    assert did(res) == [{"unit": "implement-T1", "merge": "waiting for acme/shop-api#2's required checks"}]
    assert states(res)["implement-T1"] == ("waiting", {"gate": "merge"})
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(checks="pass"))
    res = story.run("apply", "412")
    assert did(res)[0] == {"unit": "implement-T1", "merge": "merged into backend:fab0/412/base"}
    assert states(res)["implement-T1"] == ("done", None)
    assert gh(story)["pulls"]["acme/shop-api"][0]["state"] == "merged"
    assert gh(story)["pulls"]["acme/shop-api"][0]["draft"] is False, "from R6: marked ready once approved"
    assert git(story.repo("backend"), "show", "fab0/412/base:src/a.py") == "x = 1"


def test_an_ungated_unit_merges_without_a_pull_request(story):
    spec_done(story, spec=SPEC + "\n## Docs\n\nA note.\n")
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    story.run("apply", "412")
    note = handouts(story.run("next", "412"))["docs-note"]
    finish(story, "412", note, start_work(story, note), {("home", "docs/notes/412.md"): "x"}, {"summary": "Noted."})
    pulls_before = len(gh(story)["pulls"]["acme/shop"])
    res = story.run("apply", "412")
    assert {"unit": "docs-note", "merge": "merged into home:fab0/412/base"} in did(res)
    assert len(gh(story)["pulls"]["acme/shop"]) == pulls_before, "D28: no pull request"


def test_the_end_of_a_run(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("apply", "412")
    ended = [(a["unit"], a.get("action"), a.get("did")) for a in res["applied"] if a["unit"].startswith("onEnd")]
    assert ended == [
        ("onEnd-2", "git.remove-run-artifacts", "removed home:work/412/tasks.yaml"),
        ("onEnd-3", "pr.ready", "marked acme/shop#1 ready; opened acme/shop-api#2"),
        ("onEnd-4", "tracker.close", "closed"),
    ]
    assert res["kind"] == "waiting" and states(res)["implement-T1"] == ("done", None)
    assert actions(res)["onEnd-1"] == ("git.sync-base", "done", None)
    assert story.run("apply", "412")["applied"] == []


def test_questions_are_posted_once(story):
    story.run("apply", "412")
    h = handouts(story.run("next", "412"))["spec"]
    finish(story, "412", h, start_work(story, h), returned={"summary": "?", "questions": ["How many nights?"]})
    res = story.run("apply", "412")
    assert did(res) == [
        {"unit": "spec", "pull": "opened acme/shop#2"},
        {"unit": "spec", "pull": "commented on acme/shop#2"},
    ]
    comment = gh(story)["pulls"]["acme/shop"][1]["comments"][0]["body"]
    assert comment.startswith("<!-- fab0:questions spec 1 -->") and "- How many nights?" in comment
    assert story.run("apply", "412")["applied"] == []


def test_a_pull_request_a_person_marked_ready_stays_ready(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    story.run("apply", "412")
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(draft=False))
    story.run("apply", "412")
    assert gh(story)["pulls"]["acme/shop-api"][0]["draft"] is False, "from #17: never turned back into a draft"


def test_apply_on_the_ledger_branch_labels_the_item(factory):
    gh_data = {
        "user": "u",
        "issues": {"acme/support#7": {"title": "Checkout fails", "body": "x", "labels": ["needs-triage"]}},
    }
    f = factory("triage").init_git(gh_data)
    res = f.run("apply", "7")
    assert did(res) == [{"unit": "onStart-1", "action": "tracker.label", "did": "labelled"}]
    assert gh(f)["issues"]["acme/support#7"]["labels"] == ["triaging"]
    assert f.run("apply", "7")["applied"] == []


def test_a_new_attempt_after_a_merged_pull_request_gets_a_new_one(story):
    """From R6: a person merged the spec's pull request at attempt 1, then the spec was reworked. The merged pull
    request can't carry the new attempt, so apply opens another."""
    spec_done(story)
    story.run("apply", "412")
    story.gh_edit(lambda d: d["pulls"]["acme/shop"][1].update(state="merged"))
    res = story.run("apply", "412")
    assert did(res) == [{"unit": "spec", "pull": "opened acme/shop#3"}]
    assert story.run("apply", "412")["applied"] == []


def test_a_foreach_closes_its_items_at_its_end(story):
    """From R6: task pull requests merge into the run branch, which closes no issue, so the foreach does."""
    close = [{"tracker.child.close": {"type": "Task"}}]
    story.edit("pipelines/story.yaml", lambda d: d["steps"][1].update(onEnd=close))
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    assert actions(story.run("status", "412"))["tasks--onEnd-1"][1] == "blocked", "not before its units are done"
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("apply", "412")
    assert {"unit": "tasks--onEnd-1", "action": "tracker.child.close", "did": "closed #413"} in did(res)
    assert gh(story)["issues"]["acme/shop#413"]["state"] == "closed"
    assert story.run("apply", "412")["applied"] == []


def test_flat_branching_merges_into_the_base_only_when_allowed(story):
    """From R8 (found by the docs factory's planner): with flat branching a unit's parent is the base branch, so
    an ungated unit waits for a person there, unless git.auto_merge allows the base."""
    story.edit("fab0.yaml", lambda d: d["git"].update(branching="flat", auto_merge={"into": ["run"]}))
    story.edit("pipelines/story.yaml", lambda d: d["steps"][0].update(gate="none"))
    story.edit("pipelines/story.yaml", lambda d: d["steps"][1]["steps"][0]["gate"].update(merge="human"))
    spec_done(story)
    res = story.run("apply", "412")
    assert states(res)["spec"] == ("waiting", {"gate": "human"})
    assert did(res) == [{"unit": "spec", "pull": "opened acme/shop#1"}], "a person merges it into main"
    story.edit("fab0.yaml", lambda d: d["git"].update(auto_merge={"into": ["run", "base"], "allow_base": True}))
    story.commit("home", "allow merges into the base")  # apply doesn't merge into a checkout with changes
    res = story.run("apply", "412")
    assert {"unit": "spec", "merge": "merged into home:main"} in did(res)
