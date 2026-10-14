"""`inbox`: open runs found from their traces, and what each needs from whom."""

import json

from conftest import STORY_GH
from test_apply import spec_done
from test_conflicts import remotes
from test_run import handouts


def test_an_item_that_matches_a_trigger_is_a_run_to_start(story):
    res = story.run("inbox", "--as", "po-user")
    assert res["roles"] == ["product-owner"]
    assert res["runs"] == [{"run": "412", "pipeline": "story", "kind": "units", "title": "Extend a booking"}]
    assert res["ready"] == [{"run": "412", "pipeline": "story", "units": ["onStart-1"], "command": "/fab0:run 412"}]


def test_a_human_gate_is_in_its_owners_inbox_and_waits_for_the_others(story):
    spec_done(story)
    story.run("apply", "412")
    mine = story.run("inbox", "--as", "po-user")
    assert mine["yours"] == [
        {
            "run": "412",
            "unit": "spec",
            "reason": {"gate": "human"},
            "owner": "product-owner",
            "pull": "acme/shop#2",
        }
    ]
    assert mine["ready"] == [] and mine["waiting"] == []
    theirs = story.run("inbox", "--as", "be-user")
    assert theirs["yours"] == [] and theirs["waiting"][0]["unit"] == "spec"


def test_signals_that_need_a_person_are_listed_and_the_queue_isnt(story):
    data = {"user": "po-user", "issues": {**STORY_GH["issues"], "acme/shop#300": {"title": "Old work"}}}
    data["issues"]["acme/shop#412"] = {**data["issues"]["acme/shop#412"], "blocked_by": ["acme/shop#300"]}
    story.gh_path.write_text(json.dumps(data))
    res = story.run("inbox", "--as", "po-user")
    assert res["blocked"] == [{"run": "412", "unit": "onStart-1", "reason": {"tracker": ["acme/shop#300"]}}]
    assert res["ready"] == []


def test_a_finished_run_leaves_the_inbox(factory):
    gh = {"user": "u", "issues": {"acme/support#7": {"title": "Fails", "body": "x", "labels": ["needs-triage"]}}}
    f = factory("triage").init_git(gh)
    assert [r["run"] for r in f.run("inbox", "--as", "u")["runs"]] == ["7"]
    f.gh_edit(lambda d: d["issues"]["acme/support#7"].update(labels=["triaging"]))
    assert [r["run"] for r in f.run("inbox", "--as", "u")["runs"]] == [], "no trace left: not triggered, no ledger"


def test_apply_pushes_the_ledger_branch(factory):
    gh = {"user": "u", "issues": {"acme/support#7": {"title": "Fails", "body": "x", "labels": ["needs-triage"]}}}
    f = factory("triage").init_git(gh)
    remotes(f)
    f.run("apply", "7")
    h = handouts(f.run("next", "7"))["classify"]
    assert h["branches"][0]["branch"] == "fab0/ledger"
    f.checkout("home", "fab0/ledger", "main")
    f.write("home", "triage/7/notes.txt", "x\n")  # anything committed in the run's folder
    f.commit("home", "classify attempt 1")
    f.checkout("home", "main")
    res = f.run("apply", "7")
    assert {"ledger": "pushed fab0/ledger"} in res["applied"]
    assert f.run("apply", "7")["applied"] == []
    assert [r["run"] for r in f.run("inbox", "--as", "u")["runs"]] == ["7"], "found on the ledger branch"
