"""Artifacts, checks and staleness: what makes a recorded attempt stand, and what hands out the next one."""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import SCRIPT, git, start_docs
from test_run import (
    BRIEF,
    DOCS_GH,
    SPEC,
    TASKS,
    do_spec,
    finish,
    handouts,
    start_work,
    started,
    states,
)

ONE_TASK = "- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n"


def gh(f) -> dict:
    return json.loads(f.gh_path.read_text())


def set_gh(f, data: dict) -> None:
    f.gh_path.write_text(json.dumps(data))


def implement_t1(f, files=None) -> dict:
    t1 = handouts(f.run("next", "412"))["implement-T1"]
    finish(f, "412", t1, start_work(f, t1), files or {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})
    return t1


def test_inputs_and_outputs_are_hashed_in_the_ledger(story):
    do_spec(story, tasks=ONE_TASK)
    rec = handouts(story.run("next", "412"))["implement-T1"]
    res = finish(story, "412", rec, start_work(story, rec), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "D"})
    entry = res["recorded"]
    assert set(entry["inputs"]) == {"spec", "unit", "reviews"}
    assert all(h.startswith("sha256:") for h in entry["inputs"].values())
    assert "verify" not in entry, "implement declares no outputs and stayed in its claims"
    spec = git(story.repo("home"), "show", "fab0/412/spec:work/412/ledger/spec-1.yaml")
    assert "outputs:" in spec and "spec: sha256:" in spec and "verify:" in spec and "spec: pass" in spec


def test_failed_checks_hand_out_a_new_attempt_with_the_findings(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): "# Spec\n\n## Intent\n", ("home", "work/412/tasks.yaml"): "- {id: X}\n"}
    res = finish(story, "412", h, start_work(story, h), files, {"summary": "Wrote it."})
    assert not res["verified"] and res["failed"] == ["spec", "tasks"]
    nxt = story.run("next", "412")
    assert states(nxt)["spec"] == ("ready", {"rework": ["verify"]})
    again = handouts(nxt)["spec"]
    assert again["attempt"] == 2 and again["branches"][-1]["exists"]
    assert "## Rework" in again["brief"] and "missing sections: Scope, Scenarios" in again["brief"]
    assert "'title' is a required property" in again["brief"] and "Wrote it." in again["brief"]


def test_checks_that_keep_failing_stop_the_unit(story):
    started(story)
    bad = {("home", "specs/412.md"): "# no sections\n", ("home", "work/412/tasks.yaml"): ONE_TASK}
    for attempt in (1, 2, 3):
        h = handouts(story.run("next", "412"))["spec"]
        assert h["attempt"] == attempt
        trees = start_work(story, h) if attempt == 1 else {"home": Path(h["branches"][-1]["worktree"])}
        bad[("home", "specs/412.md")] += f"attempt {attempt}\n"
        finish(story, "412", h, trees, bad, {"summary": f"Try {attempt}."})
    assert states(story.run("status", "412"))["spec"] == ("blocked", {"failed": "checks failed 3 times"})


def test_a_lint_verifier_runs_on_the_written_file(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    h = handouts(f.run("next", "docs-1"))["brief"]
    pages = json.dumps([{"slug": "intro", "file": "docs/intro.md"}])
    files = {("home", "briefs/docs-1.md"): BRIEF + "LINT-FAIL\n", ("home", "work/docs-1/pages.json"): pages}
    res = finish(f, "docs-1", h, start_work(f, h), files, {"summary": "Planned."})
    assert res["failed"] == ["brief"]
    assert "MD000 the test asked this file to fail" in res["recorded"]["verify"]["brief"][0]


def test_a_changed_input_makes_done_work_stale(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    assert states(story.run("status", "412"))["implement-T1"] == ("done", None)
    data = gh(story)
    data["issues"]["acme/shop#412"]["body"] = "Guests extend a booking by up to three nights."
    set_gh(story, data)
    s = states(story.run("status", "412"))
    assert s["spec"] == ("ready", {"rework": ["stale"]}), "the intent changed"
    assert s["foreach-tasks"] == ("blocked", {"order": "spec"}), "what follows waits for the spec again"
    h = handouts(story.run("next", "412", "--unit", "spec"))["spec"]
    assert h["attempt"] == 2 and "These inputs changed since then, so read them again: intent." in h["brief"]
    assert "up to three nights" in h["brief"]
    assert h["branches"][-1]["sync"] and h["branches"][-1]["from"] == "fab0/412/base"
    tree = Path(h["branches"][-1]["worktree"])
    git(tree, "merge", "-q", "fab0/412/base")  # what the session does for `sync`
    files = {("home", "specs/412.md"): SPEC + "- S2: up to three nights\n", ("home", "work/412/tasks.yaml"): ONE_TASK}
    finish(story, "412", h, {"home": tree}, files, {"summary": "Three nights."})
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    assert states(story.run("status", "412"))["implement-T1"] == ("ready", {"rework": ["stale"]})


def test_a_review_asking_for_changes_reworks_the_unit(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    data = gh(story)
    review = {"id": 1, "state": "CHANGES_REQUESTED", "user": "be-user", "body": "Handle a full hotel."}
    data["pulls"]["acme/shop-api"] = [
        {"number": 9, "branch": "fab0/412/implement-T1", "base": "fab0/412/base", "reviews": [review]}
    ]
    set_gh(story, data)
    nxt = story.run("next", "412")
    assert states(nxt)["implement-T1"] == ("ready", {"rework": ["stale"]})
    brief = handouts(nxt)["implement-T1"]["brief"]
    assert "These inputs changed since then, so read them again: reviews." in brief
    assert "**be-user** (changes requested):" in brief and "Handle a full hotel." in brief


def test_an_approving_review_changes_nothing(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    data = gh(story)
    review = {"id": 1, "state": "APPROVED", "user": "be-user", "body": "Fine."}
    data["pulls"]["acme/shop-api"] = [
        {"number": 9, "branch": "fab0/412/implement-T1", "base": "fab0/412/base", "reviews": [review]}
    ]
    set_gh(story, data)
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"gate": "gate"})


def test_a_removed_output_is_written_again(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    trees = start_work(story, h)
    finish(
        story,
        "412",
        h,
        trees,
        {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): TASKS},
        {"summary": "Wrote the spec."},
    )
    git(trees["home"], "rm", "-q", "specs/412.md")
    git(trees["home"], "commit", "-q", "-m", "oops")
    assert states(story.run("status", "412"))["spec"] == ("ready", {"rework": ["missing"]})


def test_an_edit_by_a_person_keeps_the_attempt(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    trees = start_work(story, h)
    finish(
        story,
        "412",
        h,
        trees,
        {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): TASKS},
        {"summary": "Wrote the spec."},
    )
    (trees["home"] / "specs" / "412.md").write_text(SPEC + "- S2: a person added this\n")
    git(trees["home"], "commit", "-qam", "review edit")
    assert states(story.run("status", "412"))["spec"] == ("waiting", {"gate": "human"})


def test_writing_outside_the_claims_is_a_finding(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story, {("backend", "src/a.py"): "x = 1\n", ("backend", "setup.cfg"): "[x]\n"})
    s = states(story.run("status", "412"))
    assert s["implement-T1"] == ("ready", {"rework": ["verify"]})
    brief = handouts(story.run("next", "412"))["implement-T1"]["brief"]
    assert "setup.cfg is outside the claimed paths (src/a.py)" in brief


def test_changing_a_repository_the_unit_may_not_write_refuses_the_record(story):
    do_spec(story, tasks=ONE_TASK)
    t1 = handouts(story.run("next", "412"))["implement-T1"]
    trees = start_work(story, t1)
    (story.repo("home") / "stray.txt").write_text("written by mistake\n")
    res = finish(story, "412", t1, trees, {("backend", "src/a.py"): "1"}, {"summary": "D"}, expect=4)
    assert res["error"] == "implement-T1 changed repositories it may not write"
    assert res["changed"] == {"home": ["stray.txt"]}


def test_questions_wait_until_an_answer_changes_an_input(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    finish(story, "412", h, start_work(story, h), returned={"summary": "?", "questions": ["How many nights?"]})
    assert states(story.run("status", "412"))["spec"] == ("waiting", {"questions": 1})
    data = gh(story)
    data["issues"]["acme/shop#412"]["body"] += "\n\nUp to three nights."
    set_gh(story, data)
    assert states(story.run("status", "412"))["spec"] == ("ready", {"rework": ["stale"]})


def test_a_tool_input_is_a_call_for_the_agent(factory):
    f = factory("triage").init_git(
        {
            "user": "support-user",
            "issues": {"acme/support#7": {"title": "Checkout fails", "body": "500.", "labels": ["triaging"]}},
        }
    )
    brief = handouts(f.run("next", "7"))["classify"]["brief"]
    assert "Call the tool `mcp__tracker__search_issues` with:" in brief
    assert '"query": "Checkout fails"' in brief and '"state": "open"' in brief


def test_a_comment_review_on_the_spec_reworks_it(story):
    """From R6: a step outside foreach reads its own pull request's reviews. With one account for people and
    agents, GitHub allows only a comment review on one's own pull request, so a comment counts."""
    story.edit("steps/spec.yaml", lambda d: d.update(inputs=["intent", "reviews"]))
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): ONE_TASK}
    finish(story, "412", h, start_work(story, h), files, {"summary": "Wrote the spec."})
    assert states(story.run("status", "412"))["spec"] == ("waiting", {"gate": "human"})
    data = gh(story)
    review = {"id": 1, "state": "COMMENTED", "user": "po-user", "body": "Split the work so two tasks run at once."}
    data["pulls"]["acme/shop"].append(
        {"number": 5, "branch": "fab0/412/spec", "base": "fab0/412/base", "reviews": [review]}
    )
    set_gh(story, data)
    nxt = story.run("next", "412")
    assert states(nxt)["spec"] == ("ready", {"rework": ["stale"]})
    assert "Split the work so two tasks run at once." in handouts(nxt)["spec"]["brief"]


def test_a_merge_decides_over_the_reviews_before_it(story):
    """From R6: a product owner answered in a review and merged. The merge decides (D34): the merged spec isn't
    sent back for its own pull request's reviews."""
    story.edit("steps/spec.yaml", lambda d: d.update(inputs=["intent", "reviews"]))
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): ONE_TASK}
    finish(story, "412", h, start_work(story, h), files, {"summary": "Wrote the spec."})
    data = gh(story)
    review = {"id": 1, "state": "COMMENTED", "user": "po-user", "body": "Answer: a demo hotel."}
    data["pulls"]["acme/shop"].append(
        {"number": 5, "branch": "fab0/412/spec", "base": "fab0/412/base", "reviews": [review], "state": "merged"}
    )
    set_gh(story, data)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    assert states(story.run("status", "412"))["spec"] == ("done", None)


def test_reviews_of_a_merged_pull_request_stay_with_the_unit(story):
    """From R6: an answer given on a pull request merged at an older attempt still reaches the new attempt."""
    story.edit("steps/spec.yaml", lambda d: d.update(inputs=["intent", "reviews"]))
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): ONE_TASK}
    finish(story, "412", h, start_work(story, h), files, {"summary": "Wrote the spec."})
    data = gh(story)
    answer = {"id": 1, "state": "COMMENTED", "user": "po-user", "body": "Answer: a demo hotel."}
    later = {"id": 2, "state": "COMMENTED", "user": "po-user", "body": "And keep it short."}
    data["pulls"]["acme/shop"] += [
        {"number": 5, "branch": "fab0/412/spec", "base": "fab0/412/base", "reviews": [answer], "state": "merged"},
        {"number": 6, "branch": "fab0/412/spec", "base": "fab0/412/base", "reviews": [later]},
    ]
    set_gh(story, data)
    brief = handouts(story.run("next", "412"))["spec"]["brief"]
    assert "Reviews on acme/shop#5, acme/shop#6, oldest first" in brief
    assert brief.index("Answer: a demo hotel.") < brief.index("And keep it short.")


def test_the_main_checkouts_wiring_governs_from_a_worktree(story):
    """From R6: a session that calls the engine from inside a unit's worktree gets the project's wiring, not
    the copy on the unit's branch."""
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    tree = start_work(story, h)["home"]
    story.edit("steps/spec.yaml", lambda d: d.update(inputs=["intent", "reviews"]))
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "next", "412"],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "FAB0_GH_FAKE": str(story.gh_path)},
    )
    brief = next(x for x in json.loads(run.stdout)["handouts"] if x["unit"] == "spec")["brief"]
    assert "### reviews" in brief


def test_an_issue_artifact_reads_a_board_field_and_a_units_own_item(story):
    """From R7: `issue` artifacts read a board field, and with `of: unit` the element's own tracker item."""
    arts = story.home / ".fab0" / "artifacts"
    (arts / "stage.yaml").write_text("artifact: stage\nkind: issue\nissue: {part: field, field: state}\n")
    (arts / "task-item.yaml").write_text("artifact: task-item\nkind: issue\nissue: {of: unit, part: body}\n")
    story.edit("steps/implement.yaml", lambda d: d.update(inputs=d["inputs"] + ["stage", "task-item"]))
    do_spec(story, tasks=ONE_TASK)
    story.gh_edit(lambda d: d["issues"]["acme/shop#500"].update(body="Keep the API stable."))
    brief = handouts(story.run("next", "412"))["implement-T1"]["brief"]
    assert "The State field of the run's tracker item:\n\n````\nStarted" in brief
    assert "The body of acme/shop#500:\n\n````\nKeep the API stable." in brief
