"""Artifacts, checks and staleness: what makes a recorded attempt stand, and what hands out the next one."""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import SCRIPT, git, start_docs
from fab0_engine.hashing import sha_json
from fab0_engine.schedule import Ctx, Run
from fab0_engine.wiring import Wiring
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
    sync_children,
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
    spec = story.fact("412", "spec-1")
    assert spec["outputs"]["spec"].startswith("sha256:") and spec["verify"]["spec"] == "pass"
    assert spec["heads"] == {"home": git(story.repo("home"), "rev-parse", "fab0/412/spec")}, "the commit it made"
    assert entry["heads"] == {"backend": git(story.repo("backend"), "rev-parse", "fab0/412/implement-T1")}
    assert not git(story.repo("home"), "ls-tree", "-r", "--name-only", "fab0/412/spec", "--", "work/412/ledger"), (
        "the ledger isn't in the product repository"
    )


def test_failed_checks_hand_out_a_new_attempt_with_the_findings(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): "# Spec\n\n## Intent\n", ("out", "tasks.yaml"): "- {id: X}\n"}
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
    bad = {("home", "specs/412.md"): "# no sections\n", ("out", "tasks.yaml"): ONE_TASK}
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
    files = {("home", "briefs/docs-1.md"): BRIEF + "LINT-FAIL\n", ("out", "pages.json"): pages}
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
    files = {("home", "specs/412.md"): SPEC + "- S2: up to three nights\n", ("out", "tasks.yaml"): ONE_TASK}
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


def _pull_with(story, **extra):
    data = gh(story)
    pull = {"number": 9, "branch": "fab0/412/implement-T1", "base": "fab0/412/base"}
    pull.update(extra)
    data["pulls"]["acme/shop-api"] = [pull]
    set_gh(story, data)


def test_a_conversation_comment_reworks_the_unit(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    _pull_with(story, comments=[{"id": 1, "user": "po-user", "body": "Rename the flag to --dry"}])
    nxt = story.run("next", "412")
    assert states(nxt)["implement-T1"] == ("ready", {"rework": ["stale"]})
    brief = handouts(nxt)["implement-T1"]["brief"]
    assert "These inputs changed since then, so read them again: reviews." in brief
    assert "**po-user** (conversation comment):" in brief and "Rename the flag to --dry" in brief


def test_reviews_and_comments_are_interleaved_oldest_first(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    _pull_with(
        story,
        reviews=[
            {"id": 1, "state": "COMMENTED", "user": "a", "body": "first", "at": "2026-01-01T10:00:00Z"},
            {"id": 2, "state": "CHANGES_REQUESTED", "user": "a", "body": "third", "at": "2026-01-01T10:10:00Z"},
        ],
        comments=[{"id": 1, "user": "b", "body": "second", "at": "2026-01-01T10:05:00Z"}],
    )
    brief = handouts(story.run("next", "412"))["implement-T1"]["brief"]
    assert brief.index("first") < brief.index("second") < brief.index("third")


def test_a_fab0_comment_does_not_count(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    _pull_with(story, comments=[{"id": 1, "user": "me", "body": "<!-- fab0:questions -->\nWhich hotel?"}])
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"gate": "gate"})


def test_the_brief_says_no_reviews_when_the_only_comment_is_fab0s(story):
    story.edit("steps/spec.yaml", lambda d: d.update(inputs=["intent", "reviews"]))
    started(story)
    data = gh(story)
    data["pulls"]["acme/shop"] += [
        {
            "number": 5,
            "branch": "fab0/412/spec",
            "base": "fab0/412/base",
            "comments": [{"id": 1, "user": "me", "body": "<!-- fab0:questions -->\nWhich hotel?"}],
        }
    ]
    set_gh(story, data)
    brief = handouts(story.run("next", "412"))["spec"]["brief"]
    assert "No reviews ask for changes." in brief and "Which hotel?" not in brief


def test_a_comment_on_a_merged_pull_request_does_not_rework_the_unit(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    _pull_with(story, state="merged", comments=[{"id": 1, "user": "po-user", "body": "Too late."}])
    assert states(story.run("status", "412"))["implement-T1"] == ("done", None)


def test_comments_of_an_earlier_merged_pull_request_stay_in_the_brief(story):
    story.edit("steps/spec.yaml", lambda d: d.update(inputs=["intent", "reviews"]))
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): ONE_TASK}
    finish(story, "412", h, start_work(story, h), files, {"summary": "Wrote the spec."})
    data = gh(story)
    later = {"id": 2, "state": "COMMENTED", "user": "po-user", "body": "And keep it short."}
    data["pulls"]["acme/shop"] += [
        {
            "number": 5,
            "branch": "fab0/412/spec",
            "base": "fab0/412/base",
            "comments": [{"id": 1, "user": "po-user", "body": "A demo hotel."}],
            "state": "merged",
        },
        {"number": 6, "branch": "fab0/412/spec", "base": "fab0/412/base", "reviews": [later]},
    ]
    set_gh(story, data)
    brief = handouts(story.run("next", "412"))["spec"]["brief"]
    assert brief.index("A demo hotel.") < brief.index("And keep it short.")


def test_reviews_without_comments_hash_as_before(story):
    do_spec(story, tasks=ONE_TASK)
    old = [
        {"state": "COMMENTED", "user": "a", "body": "Hm."},
        {"state": "CHANGES_REQUESTED", "user": "b", "body": "No."},
    ]
    data = gh(story)
    data["pulls"]["acme/shop-api"] = [
        {
            "number": 9,
            "branch": "fab0/412/implement-T1",
            "base": "fab0/412/base",
            "reviews": [dict(r, id=n) for n, r in enumerate(old, 1)],
        }
    ]
    set_gh(story, data)
    rec = handouts(story.run("next", "412"))["implement-T1"]
    res = finish(story, "412", rec, start_work(story, rec), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "D"})
    assert res["recorded"]["inputs"]["reviews"] == sha_json(old)


def test_reviews_and_comments_of_all_pull_requests_are_ordered_by_time(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story)
    data = gh(story)
    base = {"branch": "fab0/412/implement-T1", "base": "fab0/412/base"}
    data["pulls"]["acme/shop-api"] = [
        dict(
            base,
            number=8,
            state="merged",
            comments=[{"id": 1, "user": "a", "body": "old pr late", "at": "2026-01-03T00:00:00Z"}],
        ),
        dict(
            base,
            number=9,
            reviews=[
                {"id": 1, "state": "COMMENTED", "user": "a", "body": "new pr early", "at": "2026-01-02T00:00:00Z"},
                {"id": 2, "state": "COMMENTED", "user": "a", "body": "untimed", "at": ""},
            ],
        ),
    ]
    set_gh(story, data)
    brief = handouts(story.run("next", "412"))["implement-T1"]["brief"]
    assert brief.index("new pr early") < brief.index("untimed") < brief.index("old pr late")


def test_a_removed_output_is_written_again(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    trees = start_work(story, h)
    finish(
        story,
        "412",
        h,
        trees,
        {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): TASKS},
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
        {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): TASKS},
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
    files = {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): ONE_TASK}
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
    files = {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): ONE_TASK}
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
    files = {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): ONE_TASK}
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


# ------------------------------------------------------------ store artifacts (MIGRATION M3a)


def test_a_store_artifact_is_an_object_in_the_run_store_and_never_in_a_repository(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    assert h["outbox"].endswith("/fab0/out/412/spec-1") and Path(h["outbox"]).is_dir()
    assert f"- tasks: write `tasks.yaml` in `{h['outbox']}`, outside every repository" in h["brief"]
    files = {("home", "specs/412.md"): SPEC, ("out", "tasks.yaml"): TASKS}
    res = finish(story, "412", h, start_work(story, h), files, {"summary": "Wrote the spec."})
    assert res["verified"] and not Path(h["outbox"]).exists(), "the outbox is emptied once recorded"
    digest = res["recorded"]["outputs"]["tasks"]
    objects = story.store_path() / "runs" / "shop" / "412" / "objects"
    assert (objects / f"sha256-{digest[7:]}.yaml").read_text() == TASKS
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    sync_children(story, TASKS)  # the foreach's onStart: a sub-issue per task in the object
    t1 = handouts(story.run("next", "412"))["implement-T1"]
    assert t1["element"] == {"id": "T1", "title": "Booking API", "repo": "backend", "files": ["src/a.py"]}
    assert set(states(story.run("status", "412"))) >= {"implement-T1", "implement-T2", "implement-T3"}
    files = git(story.repo("home"), "log", "--all", "--name-only", "--format=").split()
    work = [f for f in files if not f.startswith((".fab0/", ".claude/"))]  # past the wiring, the run's own files
    assert "specs/412.md" in work and not [f for f in work if "tasks" in f], "only the spec reached the repository"


def test_the_outbox_holds_only_the_steps_outputs(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    files = {("home", "specs/412.md"): SPEC, ("out", "notes.txt"): "scratch"}
    res = finish(story, "412", h, start_work(story, h), files, {"summary": "Forgot the tasks."})
    assert res["recorded"]["verify"]["tasks"] == ["tasks.yaml is missing"]
    assert res["recorded"]["verify"]["outbox"] == ["notes.txt in the outbox is no output of this step"]
    assert "tasks" not in res["recorded"].get("outputs", {})
    assert states(story.run("status", "412"))["spec"] == ("ready", {"rework": ["verify"]})


def test_a_version_that_failed_its_checks_is_never_the_latest(story):
    do_spec(story, tasks="- {id: T1, title: A, repo: backend, files: [a]}\n")
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    first = story.fact("412", "spec-1")["outputs"]["tasks"]
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(body="Guests extend a booking twice."))
    h = handouts(story.run("next", "412"))["spec"]
    assert h["attempt"] == 2
    trees = {"home": Path(h["branches"][-1]["worktree"])}
    finish(story, "412", h, trees, {("out", "tasks.yaml"): "- {id: X}\n"}, {"summary": "Broke the tasks."})
    assert story.fact("412", "spec-2")["verify"]["tasks"] != "pass"
    run = Run(Wiring(story.home), "story", "412", "", None)
    assert run.latest_object("tasks", Ctx(parent=None)) == first, "the last version that passed"
