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
            "pull": "acme/shop#1",
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


def test_apply_pushes_the_run_store_and_the_inbox_finds_runs_there(factory):
    gh = {"user": "u", "issues": {"acme/support#7": {"title": "Fails", "body": "x", "labels": ["needs-triage"]}}}
    f = factory("triage").init_git(gh)
    remotes(f)
    f.run("apply", "7")  # its onStart takes the trigger's label off: only the store says the run is open
    handouts(f.run("next", "7"))
    f.put_fact("7", "classify-1", FACT)  # recorded here, not pushed: a record whose push failed
    res = f.run("apply", "7")
    assert {"store": "pushed 1 write"} in res["applied"]
    assert f.run("apply", "7")["applied"] == []
    assert [r["run"] for r in f.run("inbox", "--as", "u")["runs"]] == ["7"], "found in the run store"


FACT = {
    "step": "classify",
    "attempt": 1,
    "executor": "agent",
    "actor": "u",
    "branch": "fab0/7/classify",
    "base": {"branch": "main", "sha": "0" * 40},
    "gate": {"type": "auto"},
    "summary": "Billing.",
    "data": {"severity": "high", "area": "billing"},
}


def log_of(f, run: str):
    return f.repo("home") / ".git" / "fab0" / "events" / f"{run}.jsonl"


def test_the_inbox_shows_the_latest_event_of_each_run_that_has_one(story):
    story.gh_edit(lambda d: d["issues"].update({"acme/shop#413": {"title": "Cancel a booking", "type": "Story"}}))
    spec_done(story)
    story.run("apply", "412")
    events = [json.loads(line) for line in log_of(story, "412").read_text().splitlines()]
    assert not log_of(story, "413").exists()
    res = story.run("inbox", "--as", "po-user")
    runs = {r["run"]: r for r in res["runs"]}
    assert runs["412"]["latest"] == {"time": events[-1]["time"], "line": events[-1]["line"]}
    assert "latest" not in runs["413"]
    assert not log_of(story, "413").exists(), "the inbox writes no log"


def test_the_inbox_changes_no_log(story):
    spec_done(story)
    story.run("apply", "412")
    before = log_of(story, "412").read_bytes()
    story.run("inbox", "--as", "po-user")
    assert log_of(story, "412").read_bytes() == before


def test_an_unreadable_last_line_is_not_the_latest_event(story):
    spec_done(story)
    story.run("apply", "412")
    complete = [json.loads(line) for line in log_of(story, "412").read_text().splitlines()]
    with log_of(story, "412").open("a") as fh:
        fh.write('{"time": "2026-10-16T')  # an event still being written
    res = story.run("inbox", "--as", "po-user")
    assert res["runs"][0]["latest"]["line"] == complete[-1]["line"]
