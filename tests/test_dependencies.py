"""The tracker's relations as dependencies (P11, D49): blocked-by and sub-issues, read on every call, and
`after` projected as blocked-by when the items are made."""

from conftest import STORY_GH
from fab0_engine.schedule import Ctx
from test_apply import gh, implement, spec_done
from test_gates import record_gate, verdict
from test_lifecycle import run_of
from test_run import actions, do_spec, handouts, states

TASKS = """- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}
- {id: T2, title: Pricing, repo: backend, files: [src/b.py], depends_on: [T1]}
- {id: T3, title: Help page, repo: home, files: [help/booking.md]}
"""
T1, T2, T3 = "acme/shop#500", "acme/shop#501", "acme/shop#502"


def blocked_by(f, item: str, *refs: str) -> None:
    f.gh_edit(lambda d: d["issues"][item].update(blocked_by=list(refs)))


def issue(f, ref: str, **fields) -> None:
    f.gh_edit(lambda d: d["issues"].setdefault(ref, {"title": ref}).update(fields))


def test_a_blocked_by_on_the_board_changes_what_next_hands_out(story):
    do_spec(story, tasks=TASKS)
    s = states(story.run("status", "412"))
    assert s["implement-T2"] == ("blocked", {"order": ["T1"]}), "after, projected as blocked-by"
    assert s["implement-T3"] == ("ready", None)
    blocked_by(story, T2)
    assert states(story.run("status", "412"))["implement-T2"] == ("ready", None), "removed on the board"
    blocked_by(story, T3, T1)
    assert states(story.run("status", "412"))["implement-T3"] == ("blocked", {"order": ["T1"]}), "added"
    assert set(handouts(story.run("next", "412"))) == {"implement-T1", "implement-T2"}


def test_an_item_outside_the_run_holds_a_unit_back_until_it_closes(story):
    do_spec(story, tasks=TASKS)
    issue(story, "acme/billing#9", state="open")
    blocked_by(story, T3, "acme/billing#9")
    res = story.run("next", "412")
    assert states(res)["implement-T3"] == ("blocked", {"tracker": ["acme/billing#9"]})
    assert "implement-T3" not in handouts(res)
    issue(story, "acme/billing#9", state="closed", state_reason="completed")
    assert states(story.run("status", "412"))["implement-T3"] == ("ready", None)
    issue(story, "acme/billing#9", state_reason="not_planned")
    assert states(story.run("status", "412"))["implement-T3"] == (
        "blocked",
        {"invalid-dependency": "blocked by acme/billing#9, closed as not planned"},
    )


def test_invalid_relations_are_reported_not_guessed(story):
    do_spec(story, tasks=TASKS)
    issue(story, T1, state="closed")
    s = states(story.run("status", "412"))
    assert s["implement-T2"] == ("blocked", {"invalid-dependency": "acme/shop#500 closed, but its work isn't merged"})
    blocked_by(story, T3, "acme/shop#412")
    s = states(story.run("status", "412"))
    assert s["implement-T3"] == ("blocked", {"invalid-dependency": "blocked by its own run's item acme/shop#412"})
    blocked_by(story, T2)
    blocked_by(story, T1, T3)
    blocked_by(story, T3, T1)
    s = states(story.run("status", "412"))
    assert s["implement-T1"] == ("blocked", {"invalid-dependency": "after makes a cycle: T1 -> T3 -> T1"})
    assert s["implement-T2"] == ("ready", None)


def test_the_run_waits_for_what_its_item_is_blocked_by(factory):
    data = {"user": "po-user", "issues": {**STORY_GH["issues"], "acme/shop#300": {"title": "Old work"}}}
    data["issues"]["acme/shop#412"] = {**data["issues"]["acme/shop#412"], "blocked_by": ["acme/shop#300"]}
    f = factory("story").init_git(data)
    res = f.run("apply", "412")
    assert res["applied"] == [] and actions(res)["onStart-1"] == (
        "git.run-branch",
        "blocked",
        {"tracker": ["acme/shop#300"]},
    )
    issue(f, "acme/shop#300", state="closed")
    assert f.run("apply", "412")["applied"][0]["action"] == "git.run-branch"


def test_the_run_ends_after_its_items_other_sub_issues(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    issue(story, "acme/shop#600", title="Tell support", state="open")
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"]["sub_issues"].append("acme/shop#600"))
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("apply", "412")
    assert states(res)["implement-T1"] == ("done", None)
    assert actions(res)["onEnd-2"] == ("git.remove-run-artifacts", "blocked", {"tracker": ["acme/shop#600"]})
    assert not any(a["unit"].startswith("onEnd") for a in res["applied"])
    issue(story, "acme/shop#600", state="closed")
    assert "onEnd-2" in {a["unit"] for a in story.run("apply", "412")["applied"]}


def test_apply_projects_after_as_blocked_by_once(story):
    spec_done(story, tasks=TASKS)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    res = story.run("apply", "412")
    assert res["applied"][0]["did"] == "created #413, #414, #415; #414 after #413"
    assert gh(story)["issues"]["acme/shop#414"]["blocked_by"] == ["acme/shop#413"]
    assert states(res)["implement-T2"] == ("blocked", {"order": ["T1"]})
    story.gh_edit(lambda d: d["issues"]["acme/shop#414"].update(blocked_by=[]))
    res = story.run("apply", "412")
    assert res["applied"] == [], "a relation a person removed isn't put back"
    assert states(res)["implement-T2"] == ("ready", None)


def test_apply_shows_where_each_item_is_on_the_board(story):
    story.edit("fab0.yaml", lambda d: d["tracker"]["fields"].update(signal="Signal"))
    spec_done(story, tasks=TASKS)
    res = story.run("apply", "412")
    assert {"item": "acme/shop#412", "signal": "waiting: spec (gate human)"} in res["applied"]
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    res = story.run("apply", "412")
    signals = {a["item"]: a["signal"] for a in res["applied"] if "signal" in a}
    assert signals == {
        "acme/shop#412": "ready: implement-T1",
        "acme/shop#413": "ready: implement-T1",
        "acme/shop#414": "blocked: implement-T2 (order T1)",
        "acme/shop#415": "ready: implement-T3",
    }
    assert gh(story)["issues"]["acme/shop#414"]["fields"] == {"Signal": "blocked: implement-T2 (order T1)"}
    assert story.run("apply", "412")["applied"] == []


def test_a_condition_on_a_board_field(story, monkeypatch):
    cond = {"tracker": {"field": {"name": "state", "equals": "Started"}}}
    assert run_of(story, "story", "412", monkeypatch).condition(cond, Ctx(parent=None)) == (False, None)
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(fields={"State": "Started"}))
    assert run_of(story, "story", "412", monkeypatch).condition(cond, Ctx(parent=None)) == (True, None)
