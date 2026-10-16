"""The tracker adapter: each thing is read once per evaluation, a board is read once, and a write makes only write
calls. The fake tracker logs every call it gets, one per request GitHub would see; `gh` is stubbed for the real
adapter."""

import json
import os
import stat
import sys
import threading

import pytest
import yaml
from conftest import in_flight, reads, writes
from fab0_engine.errors import Fail
from fab0_engine.tracker import GhCli, GhFake, Memo
from test_apply import gh, implement, spec_done
from test_gates import record_gate, verdict
from test_run import do_spec, states

TWO_TASKS = """- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}
- {id: T2, title: Help page, repo: home, files: [help/booking.md], depends_on: [T1]}
"""
REVIEW = {"id": 1, "state": "COMMENTED", "body": "Why this?", "user": "po-user", "at": "2026-01-02T10:00:00Z"}


def two_task_run(f, *, review: bool = True, pipelines: int = 1) -> None:
    """A Story run whose spec is merged into the run branch, with two tasks in two repositories: each task has its
    sub-issue of the run's item, the second is blocked by the first, and each has an open pull request. The
    foreach closes its items at its end; with `pipelines=2`, the project has a second issue-triggered pipeline,
    so the run's item picks the run's."""
    close = [{"tracker.child.close": {"type": "Task"}}]
    f.edit("pipelines/story.yaml", lambda d: d["steps"][1].update(onEnd=close))
    if pipelines == 2:
        fix = {**f.read("pipelines/story.yaml"), "pipeline": "fix", "trigger": {"issue": {"type": "Bug"}}}
        (f.home / ".fab0" / "pipelines" / "fix.yaml").write_text(yaml.safe_dump(fix, sort_keys=False))
        f.edit("fab0.yaml", lambda d: d.update(pipelines=["story", "fix"]))
    do_spec(f, tasks=TWO_TASKS)
    implement(f)

    def pulls(data):
        for repo, number, unit in (("acme/shop-api", 2, "T1"), ("acme/shop", 3, "T2")):
            data["pulls"].setdefault(repo, []).append(
                {
                    "number": number,
                    "branch": f"fab0/412/implement-{unit}",
                    "base": "fab0/412/base",
                    "reviews": [REVIEW] if review else [],
                }
            )

    f.gh_edit(pulls)
    f.reset_calls()


def asks(calls: list[dict]) -> list[tuple]:
    return [(c["method"], *c["args"]) for c in calls]


def no_repeats(calls: list[dict]) -> None:
    seen = asks(reads(calls))
    assert len(seen) == len(set(seen)), f"a read was made twice: {seen}"


# ------------------------------------------------------------ S1, S2, S3: once per command


@pytest.mark.parametrize("command", ["status", "next", "apply"])
def test_no_read_is_repeated_in_a_command(story, command):
    two_task_run(story, pipelines=2)
    story.run(command, "412")
    calls = story.calls()
    assert reads(calls), "the command read the tracker"
    no_repeats(calls)


def test_the_inbox_reads_each_thing_once(story):
    two_task_run(story)
    story.run("inbox", "--as", "po-user")
    no_repeats(story.calls())


@pytest.mark.parametrize("command", ["status", "next", "apply"])
def test_the_run_item_and_its_sub_issues_are_fetched_in_one_call(story, command):
    """The foreach checks its sub-issues for its onStart and its onEnd, and the run's item picks the pipeline."""
    two_task_run(story, pipelines=2)
    story.run(command, "412")
    asked = [c for c in story.calls() if c["method"] == "item"]
    assert [c["args"] for c in asked] == [["acme/shop", 412]]


def test_a_run_shaped_like_run_44_needs_few_calls(story):
    two_task_run(story)
    res = story.run("status", "412")
    assert states(res)["implement-T2"] == ("blocked", {"order": ["T1"]})
    assert len(story.calls()) < 8


# ------------------------------------------------------------ S6, S7: the current state, every time


def test_each_command_reads_the_current_state(story):
    two_task_run(story, review=False)
    assert states(story.run("status", "412"))["implement-T1"][0] == "waiting"
    story.reset_calls()

    def review(data):
        data["pulls"]["acme/shop-api"][0]["reviews"] = [REVIEW]

    story.gh_edit(review)
    res = story.run("status", "412")
    assert states(res)["implement-T1"] == ("ready", {"rework": ["stale"]}), "the review changed the unit"
    assert {c["method"] for c in story.calls()} >= {"item", "pulls"}, "and was read from the tracker"


def test_a_round_of_apply_reads_again_after_it_changed_something(story):
    spec_done(story, tasks=TWO_TASKS)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    story.reset_calls()
    res = story.run("apply", "412")
    assert {"unit": "implement-T1", "merge": "merged into backend:fab0/412/base"} in res["applied"]
    assert states(res)["implement-T1"] == ("done", None)
    assert states(res)["implement-T2"] == ("ready", None), "the second round saw the merge"
    rounds = [c for c in story.calls() if c["method"] == "item"]
    assert len(rounds) == 2, "one read of the run's item per round"
    assert len(asks(reads(story.calls()))) > len(set(asks(reads(story.calls())))), "the round before is read again"


def test_the_memo_answers_from_what_it_read_until_it_is_told_to_forget(story):
    two_task_run(story)
    tracker = Memo(GhFake(str(story.gh_path)))
    first = tracker.pull("acme/shop-api", "fab0/412/implement-T1", "fab0/412/base")
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(draft=False))
    assert tracker.pull("acme/shop-api", "fab0/412/implement-T1", "fab0/412/base") == first
    assert len(story.calls()) == 1
    tracker.forget()
    assert tracker.pull("acme/shop-api", "fab0/412/implement-T1", "fab0/412/base")["draft"] is False
    assert len(story.calls()) == 2


def test_a_pull_request_comes_with_its_reviews_and_comments(story):
    two_task_run(story)
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(comments=[{"id": 5, "body": "Hi", "user": "x"}]))
    story.reset_calls()
    tracker = Memo(GhFake(str(story.gh_path)))
    [pull] = tracker.pulls("acme/shop-api", "fab0/412/implement-T1", "fab0/412/base")
    assert set(pull) == {"number", "state", "draft", "body"}
    assert tracker.reviews("acme/shop-api", pull["number"]) == [REVIEW]
    assert [c["body"] for c in tracker.comments("acme/shop-api", pull["number"])] == ["Hi"]
    assert [c["method"] for c in story.calls()] == ["pulls"]


def test_what_one_caller_changes_in_an_answer_isnt_seen_by_another(story):
    two_task_run(story)
    tracker = Memo(GhFake(str(story.gh_path)))
    tracker.issue("acme/shop", 412)["labels"].append("x")
    tracker.sub_issues("acme/shop", 412).clear()
    assert "x" not in tracker.issue("acme/shop", 412)["labels"]
    assert len(tracker.sub_issues("acme/shop", 412)) == 2


def test_an_item_comes_with_its_sub_issues_relations_and_board_entry(story):
    two_task_run(story)
    story.gh_edit(lambda d: d["issues"]["acme/shop#501"].update(fields={"Signal": "x"}))
    story.reset_calls()
    tracker = Memo(GhFake(str(story.gh_path)))
    assert [c["title"] for c in tracker.sub_issues("acme/shop", 412)] == ["Booking API", "Help page"]
    assert [b["number"] for b in tracker.blocked_by("acme/shop", 501)] == [500]
    assert tracker.issue("acme/shop", 501)["title"] == "Help page"
    assert tracker.fields("acme/shop", 501, "acme", 7) == {"Signal": "x"}
    assert tracker.fields("acme/shop", 500, "acme", 7) == {}
    assert [c["method"] for c in story.calls()] == ["item"]


def test_concurrent_callers_share_one_read(story):
    two_task_run(story)
    story.delay(0.2)
    story.reset_calls()
    tracker = Memo(GhFake(str(story.gh_path)))
    threads = [threading.Thread(target=tracker.issue, args=("acme/shop", 412)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [c["method"] for c in story.calls()] == ["item"]


def test_at_most_four_requests_are_in_flight(story):
    two_task_run(story)
    story.delay(0.1)
    story.reset_calls()
    tracker = Memo(GhFake(str(story.gh_path)))
    threads = [threading.Thread(target=tracker.pulls, args=("acme/shop", f"b{n}", "main")) for n in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(story.calls()) == 12
    assert 2 <= in_flight(story.calls()) <= 4


# ------------------------------------------------------------ S8, S9, S10: the board


def signal_run(f, *, off_board: str | None = None) -> None:
    """A run whose item and two task items are on the board, with a signal field and nothing set."""
    f.edit("fab0.yaml", lambda d: d["tracker"]["fields"].update(signal="Signal"))
    do_spec(f, tasks=TWO_TASKS)

    def board(data):
        for ref in ("acme/shop#500", "acme/shop#501"):
            if ref != off_board:
                data["issues"][ref]["fields"] = {}

    f.gh_edit(board)
    f.reset_calls()


def test_board_field_writes_read_the_board_once(story):
    signal_run(story)
    res = story.run("apply", "412")
    assert [a["item"] for a in res["applied"] if "signal" in a] == ["acme/shop#412", "acme/shop#500", "acme/shop#501"]
    calls = story.calls()
    assert [c["method"] for c in calls if c["method"] == "board"] == ["board"], "the board, once"
    assert [c["method"] for c in writes(calls)] == ["set_field"] * 3, "one call for each item's field"
    assert gh(story)["issues"]["acme/shop#501"]["fields"] == {"Signal": "blocked: implement-T2 (order T1)"}
    no_repeats(calls)


def test_an_item_that_is_not_on_the_board_yet_is_added_first(story):
    signal_run(story, off_board="acme/shop#501")
    story.run("apply", "412")
    calls = story.calls()
    assert [c["method"] for c in calls if c["method"] == "board"] == ["board"]
    assert [(c["method"], c["args"][1]) for c in writes(calls)] == [
        ("set_field", "acme/shop#412"),
        ("set_field", "acme/shop#500"),
        ("add_to_board", 501),
        ("set_field", "acme/shop#501"),
    ]
    assert [c["method"] for c in reads(calls)] == ["user", "item", "board"], "nothing more read for the extra write"
    assert gh(story)["issues"]["acme/shop#501"]["fields"] == {"Signal": "blocked: implement-T2 (order T1)"}


def test_a_second_field_of_an_item_off_the_board_adds_it_once(story):
    signal_run(story, off_board="acme/shop#501")
    tracker = Memo(GhFake(str(story.gh_path)))
    tracker.set_field("acme/shop", 501, "acme", 7, "Signal", "a")
    tracker.set_field("acme/shop", 501, "acme", 7, "Other", "b")
    assert [c["method"] for c in writes(story.calls())] == ["add_to_board", "set_field", "set_field"]


@pytest.mark.parametrize(
    "layout, message",
    [
        ({"Other": {}}, "no field Signal on project acme/7"),
        ({"Signal": {"options": ["done"]}}, "field Signal on project acme/7 has no option"),
    ],
)
def test_a_missing_field_or_option_fails_and_writes_nothing_more(story, layout, message):
    signal_run(story, off_board="acme/shop#501")
    story.gh_edit(lambda d: d.update(boards={"acme/7": {"fields": layout}}))
    res = story.run("apply", "412", expect=1)
    assert res["ok"] is False and message in res["error"]
    assert writes(story.calls()) == [], "nothing written, even to add an item to the board"


# ------------------------------------------------------------ S11: a write makes no reads

STUB = """#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["GH_STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
text = " ".join(args)
for key, out in json.load(open(os.environ["GH_STUB_ANSWERS"])).items():
    if key in text:
        print(out if isinstance(out, str) else json.dumps(out))
        break
"""


@pytest.fixture
def gh_stub(tmp_path, monkeypatch):
    """A `gh` that logs every call, and answers what the test says: the first answer whose key is in the call."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(STUB.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log, answers = tmp_path / "gh.log", tmp_path / "answers.json"
    answers.write_text("{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_STUB_LOG", str(log))
    monkeypatch.setenv("GH_STUB_ANSWERS", str(answers))

    class Stub:
        def answer(self, by_key: dict):
            answers.write_text(json.dumps(by_key))

        def calls(self) -> list[list[str]]:
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return Stub()


def test_every_write_of_the_adapter_makes_only_its_write_calls(gh_stub):
    gh_stub.answer(
        {
            "api repos/o/r/issues -f title": {"number": 9, "id": 99},
            "item-add": {"id": "PVTI_1"},
            "pr create": "https://github.com/o/r/pull/12",
        }
    )
    adapter = GhCli()
    created = adapter.create_issue("o/r", "Title", "Body", "Task")
    assert created == {"number": 9, "id": 99}
    adapter.add_sub_issue("o/r", 1, created)
    adapter.add_blocked_by("o/r", 9, "o/r", 8, 88)
    adapter.edit_labels("o/r", 9, ["a"], ["b"])
    adapter.assign("o/r", 9, ["me", "you"])
    adapter.comment("o/r", 9, "Hi")
    adapter.close("o/r", 9)
    board = {"id": "PVT_1", "fields": {"Signal": {"id": "F1", "options": None}}}
    plan = adapter.plan_field(board, "o", 7, "Signal", "done")
    assert adapter.add_to_board("o/r", 9, "o", 7) == "PVTI_1"
    adapter.set_field(board, "PVTI_1", plan, "done")
    assert adapter.create_pr("o/r", "head", "main", "T", "B", True) == 12
    adapter.ready_pr("o/r", 12)
    assert gh_stub.calls() == [
        ["api", "repos/o/r/issues", "-f", "title=Title", "-f", "body=Body", "-f", "type=Task"],
        ["api", "repos/o/r/issues/1/sub_issues", "-F", "sub_issue_id=99"],
        ["api", "repos/o/r/issues/9/dependencies/blocked_by", "-F", "issue_id=88"],
        ["issue", "edit", "9", "--repo", "o/r", "--add-label", "a", "--remove-label", "b"],
        ["issue", "edit", "9", "--repo", "o/r", "--add-assignee", "me,you"],
        ["issue", "comment", "9", "--repo", "o/r", "--body", "Hi"],
        ["issue", "close", "9", "--repo", "o/r"],
        ["project", "item-add", "7", "--owner", "o", "--url", "https://github.com/o/r/issues/9", "--format", "json"],
        ["project", "item-edit", "--id", "PVTI_1", "--project-id", "PVT_1", "--field-id", "F1", "--text", "done"],
        ["pr", "create", "--repo", "o/r", "--head", "head", "--base", "main", "--title", "T", "--body", "B", "--draft"],
        ["pr", "ready", "12", "--repo", "o/r"],
    ]


def graphql(call: list[str]) -> str:
    return next(a for a in call if a.startswith("query="))


ITEM = {
    "data": {
        "repository": {
            "issue": {
                "databaseId": 412,
                "number": 412,
                "title": "Extend a booking",
                "body": "Guests extend.",
                "state": "OPEN",
                "stateReason": None,
                "issueType": {"name": "Story"},
                "repository": {"nameWithOwner": "o/r"},
                "labels": {"nodes": [{"name": "x"}]},
                "assignees": {"nodes": [{"login": "me"}]},
                "blockedBy": {
                    "nodes": [
                        {
                            "databaseId": 300,
                            "number": 300,
                            "title": "Old",
                            "state": "CLOSED",
                            "stateReason": "NOT_PLANNED",
                            "repository": {"nameWithOwner": "o/other"},
                        }
                    ]
                },
                "projectItems": {
                    "nodes": [
                        {
                            "id": "PVTI_412",
                            "project": {"number": 7, "owner": {"login": "o"}},
                            "fieldValues": {
                                "nodes": [
                                    {},
                                    {"name": "Started", "field": {"name": "State"}},
                                    {"text": "t", "field": {"name": "N"}},
                                ]
                            },
                        },
                        {
                            "id": "PVTI_x",
                            "project": {"number": 8, "owner": {"login": "o"}},
                            "fieldValues": {"nodes": []},
                        },
                    ]
                },
                "subIssues": {
                    "nodes": [
                        {
                            "databaseId": 500,
                            "number": 500,
                            "title": "Booking API",
                            "body": None,
                            "state": "OPEN",
                            "stateReason": None,
                            "issueType": {"name": "Task"},
                            "repository": {"nameWithOwner": "o/r"},
                            "labels": {"nodes": []},
                            "assignees": {"nodes": []},
                            "blockedBy": {"nodes": []},
                            "projectItems": {"nodes": []},
                        }
                    ]
                },
            }
        }
    }
}


def test_the_adapter_reads_an_item_with_everything_that_comes_with_it_in_one_request(gh_stub):
    gh_stub.answer({"issue(number": ITEM})
    tracker = Memo(GhCli())
    assert tracker.issue("o/r", 412) == {
        "number": 412,
        "id": 412,
        "title": "Extend a booking",
        "body": "Guests extend.",
        "labels": ["x"],
        "type": "Story",
        "state": "open",
        "state_reason": None,
        "assignees": ["me"],
    }
    [child] = tracker.sub_issues("o/r", 412)
    assert (child["repo"], child["number"], child["type"], child["body"], child["id"]) == ("o/r", 500, "Task", "", 500)
    [blocker] = tracker.blocked_by("o/r", 412)
    assert (blocker["repo"], blocker["number"], blocker["state"], blocker["state_reason"]) == (
        "o/other",
        300,
        "closed",
        "not_planned",
    )
    assert tracker.fields("o/r", 412, "o", 7) == {"State": "Started", "N": "t"}
    assert tracker.fields("o/r", 412, "o", 9) == {}
    assert tracker.blocked_by("o/r", 500) == [] and tracker.issue("o/r", 500)["title"] == "Booking API"
    calls = gh_stub.calls()
    assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"]
    assert "-F" in calls[0] and "number=412" in calls[0]


def test_the_adapter_reads_a_pull_request_with_its_reviews_and_comments_in_one_request(gh_stub):
    def pull(number, state, draft, repo="o/r"):
        return {
            "number": number,
            "state": state,
            "isDraft": draft,
            "body": "B",
            "headRepository": {"nameWithOwner": repo},
            "reviews": {
                "nodes": [
                    {"databaseId": 4, "state": "APPROVED", "body": None, "submittedAt": "t1", "author": {"login": "a"}}
                ]
            },
            "comments": {"nodes": [{"databaseId": 5, "body": "c", "createdAt": "t2", "author": None}]},
        }

    nodes = [pull(7, "OPEN", True), pull(3, "MERGED", False), pull(5, "OPEN", False, repo="fork/r")]
    gh_stub.answer({"pullRequests": {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}})
    tracker = Memo(GhCli())
    found = tracker.pulls("o/r", "head", "main")
    assert [(p["number"], p["state"], p["draft"], p["body"]) for p in found] == [
        (3, "merged", False, "B"),
        (7, "open", True, "B"),
    ], "oldest first, from a fork left out"
    assert tracker.reviews("o/r", 7) == [{"id": 4, "state": "APPROVED", "body": "", "user": "a", "at": "t1"}]
    assert tracker.comments("o/r", 7) == [{"id": 5, "body": "c", "user": None, "at": "t2"}]
    assert len(gh_stub.calls()) == 1


def test_a_board_field_is_set_with_one_read_of_the_board_and_one_write(gh_stub):
    board = {
        "data": {
            "repositoryOwner": {
                "projectV2": {
                    "id": "PVT_1",
                    "fields": {
                        "nodes": [
                            {"id": "F1", "name": "Title"},
                            {"id": "F2", "name": "Signal", "options": [{"id": "O1", "name": "done"}]},
                        ]
                    },
                }
            }
        }
    }
    gh_stub.answer({"issue(number": ITEM, "projectV2": board, "item-add": {"id": "PVTI_500"}})
    tracker = Memo(GhCli())
    tracker.set_field("o/r", 412, "o", 7, "Signal", "done")
    tracker.set_field("o/r", 500, "o", 7, "Signal", "done")
    kinds = [c[1] if c[0] == "api" else " ".join(c[:2]) for c in gh_stub.calls()]
    assert kinds == ["graphql", "graphql", "project item-edit", "project item-add", "project item-edit"]
    edit = gh_stub.calls()[2]
    assert edit[-6:] == ["--project-id", "PVT_1", "--field-id", "F2", "--single-select-option-id", "O1"]
    with pytest.raises(Fail, match="field Signal on project o/7 has no option 'nope'"):
        tracker.set_field("o/r", 412, "o", 7, "Signal", "nope")
    with pytest.raises(Fail, match="no field Missing on project o/7"):
        tracker.set_field("o/r", 412, "o", 7, "Missing", "x")
    assert len(gh_stub.calls()) == 5


def test_a_relation_takes_the_id_of_the_item_that_was_read_or_made(story):
    """`apply` makes the sub-issues and their relations in a row: its reads come before and after, not between."""
    spec_done(story, tasks=TWO_TASKS)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.reset_calls()
    res = story.run("apply", "412")
    assert res["applied"][0]["did"] == "created #413, #414; #414 after #413"
    methods = [c["method"] for c in story.calls()]
    first = methods.index("create_issue")
    assert methods[first : first + 5] == ["create_issue", "add_sub_issue"] * 2 + ["add_blocked_by"]
    relation = next(c for c in story.calls() if c["method"] == "add_blocked_by")
    assert relation["args"] == ["acme/shop", 414, "acme/shop", 413, 413]
    assert methods.count("item") == 2, "a read before the writes, and one for the next round"
