"""Lifecycle actions, action checks, and triggers that pick a run's pipeline."""

import pytest
import yaml
from conftest import STORY_GH
from fab0_engine.actions import check
from fab0_engine.schedule import Ctx, Run
from fab0_engine.tracker import make_tracker
from fab0_engine.wiring import Wiring
from test_gates import classify
from test_run import SPEC, actions, finish, handouts, start_work, started, states


def messages(f) -> str:
    res = f.run("validate", expect=3)
    return "\n".join(e["message"] for e in res["errors"])


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda d: d["onStart"].remove("git.run-branch"), "run branching needs git.run-branch"),
        (lambda d: d["onStart"].append({"tracker.child.sync": {}}), "belongs in a foreach's onStart"),
        (lambda d: d["steps"][1].update(onEnd=["git.sync-base"]), "acts on the whole run"),
        (lambda d: d["steps"][1].update(onEnd=[{"tracker.child.sync": {}}]), "belongs in a foreach's onStart"),
        (lambda d: d["steps"].append({"step": "ready", "action": "pr.ready"}), "pr.ready can't be a step"),
        (lambda d: d["steps"][1]["onStart"].append("tracker.child.close"), "belongs in a foreach's onEnd"),
    ],
)
def test_actions_belong_where_they_act(factory, change, expected):
    f = factory("story")
    f.edit("pipelines/story.yaml", change)
    assert expected in messages(f)


def test_a_foreach_waits_for_its_own_onstart(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    tasks = (
        "- {id: T1, title: Booking API, repo: backend, files: [a]}\n"
        "- {id: T2, title: Pricing, repo: backend, files: [b], depends_on: [T1]}\n"
    )
    finish(
        story,
        "412",
        h,
        start_work(story, h),
        {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): tasks},
        {"summary": "Spec."},
    )
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    res = story.run("next", "412")
    assert actions(res)["tasks--onStart-1"] == (
        "tracker.child.sync",
        "ready",
        {"missing": "no sub-issue for T1, T2"},
    )
    assert states(res)["implement-T1"] == ("blocked", {"order": "onStart"})
    sync = handouts(res)["tasks--onStart-1"]
    assert sync["with"] == {
        "type": "Task",
        "items": [
            {"id": "T1", "title": "Booking API", "after": []},
            {"id": "T2", "title": "Pricing", "after": ["T1"]},
        ],
    }

    def children(data):
        data["issues"]["acme/shop#500"] = {"title": "Booking API", "type": "Task"}
        data["issues"]["acme/shop#412"]["sub_issues"] = ["acme/shop#500"]

    story.gh_edit(children)
    assert actions(story.run("status", "412"))["tasks--onStart-1"][2] == {"missing": "no sub-issue for T2"}
    story.gh_edit(
        lambda d: (
            d["issues"].update({"acme/shop#501": {"title": "Pricing", "type": "Bug"}})
            or d["issues"]["acme/shop#412"]["sub_issues"].append("acme/shop#501")
        )
    )
    assert actions(story.run("status", "412"))["tasks--onStart-1"][2] == {"missing": "no sub-issue for T2"}, (
        "the type must match too"
    )
    story.gh_edit(lambda d: d["issues"]["acme/shop#501"].update(type="Task"))
    res = story.run("status", "412")
    assert actions(res)["tasks--onStart-1"][1] == "done" and states(res)["implement-T1"] == ("ready", None)


def test_onstart_labels_come_first(factory):
    gh = {"user": "u", "issues": {"acme/support#7": {"title": "Fails", "body": "x", "labels": ["needs-triage"]}}}
    f = factory("triage").init_git(gh)
    res = f.run("next", "7")
    assert actions(res)["onStart-1"] == ("tracker.label", "ready", {"missing": "add triaging; remove needs-triage"})
    assert states(res)["classify"] == ("blocked", {"order": "onStart"})
    assert handouts(res)["onStart-1"]["with"] == {"add": ["triaging"], "remove": ["needs-triage"]}


def run_of(f, pipeline: str, run: str, monkeypatch) -> Run:
    monkeypatch.setenv("FAB0_GH_FAKE", str(f.gh_path))
    return Run(Wiring(f.home), pipeline, run, "tester", make_tracker())


def test_tracker_and_pull_request_checks(factory, monkeypatch):
    gh = {
        "user": "u",
        "issues": {
            "acme/support#7": {
                "title": "Fails",
                "labels": [],
                "assignees": ["support-user"],
                "comments": [{"id": 1, "body": "Triaged as high.", "user": "u"}],
            }
        },
    }
    f = factory("triage").init_git(gh)
    run = run_of(f, "triage", "7", monkeypatch)
    ctx = Ctx(parent=None)
    assert check(run, "tracker.assign", {"role": "support"}, ctx) is None
    assert check(run, "tracker.comment", {"body": "Triaged as high."}, ctx) is None
    assert check(run, "tracker.comment", {"body": "Not said."}, ctx) == "the comment isn't there"
    assert check(run, "tracker.close", {}, ctx) == "the item is open"
    assert check(run, "pr.ready", {}, ctx) is None, "flat branching has no run pull request"


def test_board_fields_read_through_the_fields_mapping(story, monkeypatch):
    run = run_of(story, "story", "412", monkeypatch)
    assert check(run, "tracker.field", {"state": "Started"}, Ctx(parent=None)) == "State is None, not 'Started'"
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(fields={"State": "Started"}))
    run = run_of(story, "story", "412", monkeypatch)
    assert check(run, "tracker.field", {"state": "Started"}, Ctx(parent=None)) is None


def two_pipelines(f) -> None:
    story = f.read("pipelines/story.yaml")
    hotfix = {**story, "pipeline": "hotfix", "trigger": {"issue": {"type": "Bug"}}}
    manual = {**story, "pipeline": "spike", "trigger": {"manual": {"id": "^spike-"}}}
    for p in (hotfix, manual):
        (f.home / ".fab0" / "pipelines" / f"{p['pipeline']}.yaml").write_text(yaml.safe_dump(p, sort_keys=False))
    f.edit("fab0.yaml", lambda d: d.update(pipelines=["story", "hotfix", "spike"]))


def test_a_run_finds_its_pipeline_by_its_trigger(factory):
    gh = {"user": "u", "issues": {**STORY_GH["issues"], "acme/shop#413": {"title": "Crash", "type": "Bug"}}}
    f = factory("story").init_git(gh)
    two_pipelines(f)
    assert f.run("status", "412")["pipeline"] == "story"
    assert f.run("status", "413")["pipeline"] == "hotfix"
    assert f.run("status", "spike-1")["pipeline"] == "spike"
    assert "matches none of their triggers" in f.run("status", "release-2", expect=2)["error"]
    assert f.run("status", "413", "--pipeline", "story")["pipeline"] == "story", "asked for wins"


def test_two_matching_triggers_need_a_choice(factory):
    f = factory("story").init_git(STORY_GH)
    two_pipelines(f)
    f.edit("pipelines/spike.yaml", lambda d: d["trigger"]["manual"].update(id="^[0-9]+$"))
    res = f.run("status", "412", expect=2)
    assert res["error"] == "run 412: pass --pipeline; it matches story, spike"


def test_onstart_isnt_redone_once_the_run_has_records(factory):
    gh = {"user": "u", "issues": {"acme/support#7": {"title": "Fails", "body": "x", "labels": ["needs-triage"]}}}
    f = factory("triage").init_git(gh)
    f.run("apply", "7")
    classify(f, {"severity": "low", "area": "billing"})
    f.gh_edit(lambda d: d["issues"]["acme/support#7"].update(labels=[]))  # what onEnd, or a person, may do
    res = f.run("apply", "7")
    assert actions(res)["onStart-1"] == ("tracker.label", "done", None)
    assert not any(a["unit"] == "onStart-1" for a in res["applied"]), "from the e2e check: no tug of war with onEnd"


def test_pr_merge_needs_the_base_allowed(factory):
    """From R7: merging the run into the base branch is a person's, unless the policy says otherwise."""
    f = factory("story")
    f.edit("pipelines/story.yaml", lambda d: d["onEnd"].append("pr.merge"))
    assert "pr.merge merges into the base branch, which git.auto_merge doesn't allow" in messages(f)
    f.edit("fab0.yaml", lambda d: d["git"].update(auto_merge={"into": ["run", "base"], "allow_base": True}))
    assert f.run("validate")["ok"]
