"""The run's event log: hand-outs, records, what `apply` did, and units that wait or are blocked, with their
times, in `<home git dir>/fab0/events/<run>.jsonl`; read-only commands never write it, and a log that can't be
written is a warning."""

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git, start_docs
from fab0_engine import apply as apply_module
from fab0_engine.errors import Fail
from fab0_engine.events import EventLog, append_event, read_events
from fab0_engine.schemas import core_schema, schema_errors
from test_apply import ONE_TASK, gh, implement, spec_done
from test_dependencies import TASKS, blocked_by, issue
from test_gates import record_gate, verdict
from test_lifecycle import run_of
from test_run import BRIEF, DOCS_GH, SPEC, do_spec, finish, handouts, start_work, started

TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def log_path(f, run: str = "412"):
    return f.repo("home") / ".git" / "fab0" / "events" / f"{run}.jsonl"


def events(f, run: str = "412") -> list[dict]:
    path = log_path(f, run)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def lines(f, *kinds: str) -> list[str]:
    return [e["line"] for e in events(f) if not kinds or e["kind"] in kinds]


def test_an_event_is_checked_against_its_schema(tmp_path):
    path = tmp_path / "events" / "1.jsonl"
    good = {"time": "2026-10-16T10:12:40Z", "kind": "record", "line": "spec, attempt 1: verified, 8m 28s"}
    append_event(path, {**good, "unit": "spec", "attempt": 1})
    for bad in ({**good, "kind": "dance"}, {**good, "time": "yesterday"}, {**good, "tokens": 5}, {"kind": "apply"}):
        with pytest.raises(ValueError, match="invalid event"):
            append_event(path, bad)
    assert len(path.read_text().splitlines()) == 1
    assert schema_errors({**good, "reason": "gate human"}, core_schema("event")) == []


def test_an_event_is_one_line_in_one_write(tmp_path, monkeypatch):
    writes = []
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or real(fd, data))
    event = {"time": "2026-10-16T10:12:40Z", "kind": "apply", "line": "apply: x: two\nlines"}
    append_event(tmp_path / "1.jsonl", event)
    assert len(writes) == 1 and writes[0].endswith(b"\n") and writes[0].count(b"\n") == 1


def test_reading_skips_what_is_not_a_complete_event(tmp_path):
    path = tmp_path / "1.jsonl"
    good = {"time": "2026-10-16T10:12:40Z", "kind": "apply", "line": "apply: ledger: pushed x"}
    path.write_text(json.dumps(good) + "\nnot json\n" + json.dumps({"kind": "apply"}) + "\n" + json.dumps(good)[:20])
    assert read_events(path) == [good]
    assert read_events(tmp_path / "absent.jsonl") == []


def test_each_hand_out_is_logged_with_the_line_next_returned(story):
    res = story.run("next", "412")
    assert [h["executor"] for h in res["handouts"]] == ["action"]
    assert not log_path(story).exists(), "an action's hand-out adds no event"
    story.run("apply", "412")
    before = len(events(story))
    h = handouts(story.run("next", "412"))["spec"]
    added = events(story)[before:]
    assert [(e["kind"], e["unit"], e["attempt"], e["line"]) for e in added] == [("handout", "spec", 1, h["line"])]
    assert TIME.match(added[0]["time"])
    assert h["line"].startswith("spec, attempt 1: spec-author on opus, branch fab0/412/spec")
    assert set(added[0]) == {"time", "kind", "unit", "attempt", "line"}, "no brief, no return data"


def test_a_gate_hand_out_is_logged_too(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    g = handouts(story.run("next", "412"))["implement-T1"]
    assert g["gate"]
    assert g["line"] == "gate of implement-T1, attempt 1: reviewer on opus, branch fab0/412/implement-T1"
    last = events(story)[-1]
    assert (last["kind"], last["unit"], last["attempt"], last["line"]) == ("handout", "implement-T1", 1, g["line"])


def test_a_record_and_a_gate_decision_are_logged_with_the_line_record_returned(story):
    do_spec(story, tasks=ONE_TASK)
    h = handouts(story.run("next", "412", "--unit", "implement-T1"))["implement-T1"]
    rec = finish(story, "412", h, start_work(story, h), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})
    assert rec["line"].startswith("implement-T1, attempt 1: verified, ")
    last = events(story)[-1]
    assert (last["kind"], last["unit"], last["attempt"], last["line"]) == ("record", "implement-T1", 1, rec["line"])
    assert TIME.match(last["time"])
    gate = record_gate(story, "412", "implement-T1", verdict("reject"))
    last = events(story)[-1]
    assert (last["kind"], last["unit"], last["line"]) == ("gate", "implement-T1", gate["line"])
    assert gate["line"].startswith("gate of implement-T1, attempt 1: rejected, ")


def test_a_record_that_is_not_verified_is_logged_as_returned(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    rec = finish(story, "412", h, start_work(story, h), {("home", "specs/other.md"): "x"}, {"summary": "Wrote it."})
    assert not rec["verified"] and rec["line"].startswith("spec, attempt 1: not verified (")
    assert events(story)[-1]["line"] == rec["line"]


def test_a_refused_record_adds_nothing(story):
    started(story)
    assert story.run("record", "412", "spec", "--exit", "0", expect=4)["ok"] is False
    assert not log_path(story).exists()
    h = handouts(story.run("next", "412"))["spec"]
    before = log_path(story).read_text()
    finish(story, "412", h, start_work(story, h), returned={"summary": "x", "mood": "fine"}, expect=4)
    assert log_path(story).read_text() == before
    story.run("record", "412", "spec", "--gate", "--exit", "0", expect=4)
    assert log_path(story).read_text() == before


def test_apply_logs_one_event_per_entry_in_order(story):
    story.run("apply", "412")
    assert lines(story) == [
        "apply: onStart-1: git.run-branch: created fab0/412/base in home, backend",
        "apply: onStart-2: pr.open-run: opened acme/shop#1",
        "apply: onStart-3: tracker.field: set State to Started",
    ]
    assert [e["unit"] for e in events(story) if e["kind"] == "apply"] == ["onStart-1", "onStart-2", "onStart-3"]
    assert all(e["kind"] == "apply" and TIME.match(e["time"]) for e in events(story))
    before = events(story)
    assert story.run("apply", "412")["applied"] == []
    assert events(story) == before, "an apply that did nothing adds no apply event"


def test_apply_logs_a_pull_request_and_a_merge(story):
    spec_done(story)
    story.run("apply", "412")
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    story.run("apply", "412")
    record_gate(story, "412", "implement-T1", verdict("approve"))
    story.run("apply", "412")
    said = lines(story, "apply")
    assert "apply: spec: opened acme/shop#2" in said
    opened = next(x for x in said if x.startswith("apply: implement-T1: opened acme/shop-api#"))
    merged = "apply: implement-T1: merged into backend:fab0/412/base"
    assert said.index(opened) < said.index(merged)


def test_a_merge_that_waits_for_checks_is_logged_once(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    story.run("apply", "412")
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(checks="pending"))
    record_gate(story, "412", "implement-T1", verdict("approve"))
    for _ in range(3):
        res = story.run("apply", "412")
        assert res["applied"] == [{"unit": "implement-T1", "merge": "waiting for acme/shop-api#2's required checks"}]
    waits = [x for x in lines(story, "apply") if "required checks" in x]
    assert waits == ["apply: implement-T1: waiting for acme/shop-api#2's required checks"]
    story.gh_edit(lambda d: d["pulls"]["acme/shop-api"][0].update(checks="pass"))
    story.run("apply", "412")
    assert "apply: implement-T1: merged into backend:fab0/412/base" in lines(story, "apply")


def test_a_unit_that_starts_to_wait_on_a_person_is_logged_once(story):
    spec_done(story)
    story.run("apply", "412")
    states_of = lambda: [e for e in events(story) if e["kind"] == "waiting"]  # noqa: E731
    assert [e["line"] for e in states_of()] == ["waiting: spec (gate human)"]
    waiting = states_of()[0]
    assert (waiting["unit"], waiting["attempt"], waiting["reason"]) == ("spec", 1, "gate human")
    for command in (("apply", "412"), ("next", "412"), ("apply", "412"), ("status", "412"), ("next", "412")):
        story.run(*command)
    assert len(states_of()) == 1, "further commands while it still waits add none"


def test_a_later_attempt_that_waits_again_adds_a_new_event(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    finish(story, "412", h, start_work(story, h), returned={"summary": "Unclear.", "questions": ["Which?"]})
    story.run("status", "412")
    story.run("next", "412")
    assert lines(story, "waiting") == ["waiting: spec (questions 1)"]
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(body="Guests extend a booking by one night."))
    again = handouts(story.run("next", "412"))["spec"]
    assert again["attempt"] == 2
    trees = {"home": Path(again["branches"][0]["worktree"])}
    finish(story, "412", again, trees, returned={"summary": "Still unclear.", "questions": ["Which?"]})
    story.run("next", "412")
    story.run("next", "412")
    waits = [e for e in events(story) if e["kind"] == "waiting"]
    assert {e["line"] for e in waits} == {"waiting: spec (questions 1)"}
    assert [e["attempt"] for e in waits] == [1, 2]


def test_an_escalation_is_logged(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("escalate"))
    story.run("apply", "412")
    assert "waiting: implement-T1 (escalated the gate escalated)" in lines(story, "waiting")
    count = len(events(story))
    story.run("next", "412")
    assert len(events(story)) == count


def test_the_run_waiting_for_the_merge_of_its_pull_requests_is_logged(story):
    spec_done(story)
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    story.run("apply", "412")
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("apply", "412")
    assert res["kind"] == "waiting"
    assert lines(story, "waiting")[-1] == "waiting: fab0/412/base (gate human; merge fab0/412/base into the base)"
    count = len(events(story))
    story.run("apply", "412")
    assert len(events(story)) == count


def test_a_unit_blocked_by_a_signal_is_logged_and_one_that_waits_its_turn_is_not(story):
    do_spec(story, tasks=TASKS)
    issue(story, "acme/billing#9", state="open")
    blocked_by(story, "acme/shop#502", "acme/billing#9")
    res = story.run("next", "412")
    assert {u["unit"]: u.get("reason") for u in res["units"]}["implement-T2"] == {"order": ["T1"]}
    blocked = [e for e in events(story) if e["kind"] == "blocked"]
    assert [(e["unit"], e["reason"], e["line"]) for e in blocked] == [
        ("implement-T3", "tracker acme/billing#9", "blocked: implement-T3 (tracker acme/billing#9)")
    ]
    story.run("apply", "412")
    story.run("next", "412")
    assert len([e for e in events(story) if e["kind"] == "blocked"]) == 1
    assert not any(e.get("unit") == "implement-T2" for e in events(story) if e["kind"] in ("blocked", "waiting"))


def test_a_gated_block_that_waits_is_logged_with_its_attempt(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    brief = handouts(f.run("next", "docs-1"))["brief"]
    files = {("home", "briefs/docs-1.md"): BRIEF, ("home", "work/docs-1/pages.json"): "[]"}
    finish(f, "docs-1", brief, start_work(f, brief), files, {"summary": "Nothing to write."})
    f.merge("home", "docs/docs-1--brief", "docs/docs-1")
    for unit in ("parallel-3--spelling", "parallel-3--links", "parallel-3--index"):
        h = handouts(f.run("next", "docs-1", "--unit", unit))[unit]
        files = {("home", "docs/index.md"): "# Docs\n"} if unit.endswith("index") else None
        extra = [{"summary": "Index."}] if files else []
        finish(f, "docs-1", h, start_work(f, h), files, *extra, **({} if files else {"exit": 0}))
        f.merge("home", h["branch"], "docs/docs-1--parallel-3")
    record_gate(f, "docs-1", "parallel-3", exit=1, output="docs/index.md:1 MD041 first line\n")
    f.run("next", "docs-1")
    f.run("next", "docs-1")
    waits = [e for e in events(f, "docs-1") if e["kind"] == "waiting"]
    assert [(e["unit"], e["attempt"], e["reason"]) for e in waits] == [("parallel-3", 1, "escalated reject")]


def block(attempt: int, reason: dict, state: str = "waiting"):
    """A gated block as the schedule gives it: no step, and the attempt of its latest gate decision."""
    return SimpleNamespace(
        id="parallel-3", step=None, gate={"auto": {}}, auto={}, state=state, reason=reason, attempt=attempt
    )


def test_a_block_that_escalates_on_two_successive_attempts_adds_two_events(tmp_path):
    run = SimpleNamespace(units=[block(1, {"escalated": "reject"})], run_branch=None)
    log = EventLog(run)
    log._path = tmp_path / "docs-1.jsonl"
    log.states({"kind": "units"})
    log.states({"kind": "units"})
    run.units = [block(2, {"escalated": "reject"})]
    log.states({"kind": "units"})
    log.states({"kind": "units"})
    waits = read_events(log._path)
    assert [(e["kind"], e["unit"], e["attempt"], e["reason"]) for e in waits] == [
        ("waiting", "parallel-3", 1, "escalated reject"),
        ("waiting", "parallel-3", 2, "escalated reject"),
    ]
    assert log.warnings == []


def test_units_that_only_wait_their_turn_are_never_logged(tmp_path):
    def unit(name: str, state: str, reason: dict):
        return SimpleNamespace(
            id=name, step={"step": name}, gate="none", auto=None, state=state, reason=reason, attempt=1
        )

    run = SimpleNamespace(
        units=[
            unit("a", "blocked", {"order": ["T1"]}),
            unit("b", "blocked", {"data": "tasks is missing: home:work/412/tasks.yaml"}),
            unit("c", "blocked", {"claim": "implement-T1"}),
            unit("d", "blocked", {"limit": "parallel.max 2"}),
            unit("e", "waiting", {"gate": "merge"}),
            unit("f", "waiting", {"gate": "running"}),
            unit("g", "blocked", {"tracker": "acme/billing#9"}),
        ],
        run_branch=None,
    )
    log = EventLog(run)
    log._path = tmp_path / "412.jsonl"
    log.states({"kind": "units"})
    assert [(e["kind"], e["unit"]) for e in read_events(log._path)] == [("blocked", "g")]


def test_read_only_commands_write_no_events(story, factory):
    story.run("status", "412")
    story.run("inbox", "--as", "po-user")
    story.run("validate")
    assert not log_path(story).exists(), "no log file is created"
    spec_done(story)
    before = log_path(story).read_bytes()
    for command in (
        ("status", "412"),
        ("inbox", "--as", "po-user"),
        ("validate",),
        ("status", "413", "--pipeline", "story"),
    ):
        story.run(*command, expect=None)
    assert log_path(story).read_bytes() == before
    assert not log_path(story, "413").exists()


@pytest.fixture
def unwritable(story):
    """A log whose folder can't be written to: a file sits where the folder would be."""
    folder = story.repo("home") / ".git" / "fab0"
    folder.mkdir(exist_ok=True)
    (folder / "events").write_text("not a folder")
    return story


def warned(res: dict) -> list[str]:
    return [w for w in res.get("warnings", []) if "event couldn't be logged" in w]


def test_a_log_that_cant_be_written_is_a_warning_and_nothing_else(unwritable):
    f = unwritable
    res = f.run("apply", "412")
    assert len(warned(res)) == 1 and "events" in warned(res)[0]
    assert [a["unit"] for a in res["applied"]] == ["onStart-1", "onStart-2", "onStart-3"]
    spec = f.run("next", "412")
    assert len(warned(spec)) == 1 and [h["unit"] for h in spec["handouts"]] == ["spec"]
    h = spec["handouts"][0]
    files = {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): ONE_TASK}
    rec = finish(f, "412", h, start_work(f, h), files, {"summary": "Wrote it."})
    assert rec["verified"] and len(warned(rec)) == 1 and rec["line"].startswith("spec, attempt 1: ")
    assert gh(f)["pulls"]["acme/shop"][0]["draft"], "the work itself was done"
    res = f.run("apply", "412")
    assert len(warned(res)) == 1 and res["applied"] == [{"unit": "spec", "pull": "opened acme/shop#2"}]
    assert git(f.repo("home"), "status", "--porcelain") == "", "the log moves nothing in git"


def test_apply_that_stops_with_a_failure_still_logs_and_warns(unwritable, monkeypatch):
    f = unwritable
    run = run_of(f, "story", "412", monkeypatch)
    real = apply_module.perform
    calls = []

    def perform(run_, unit):
        calls.append(unit.id)
        if len(calls) == 2:
            raise Fail(1, "the second action fails")
        return real(run_, unit)

    monkeypatch.setattr(apply_module, "perform", perform)
    with pytest.raises(Fail) as stopped:
        apply_module.apply(run)
    assert stopped.value.code == 1 and stopped.value.payload["error"] == "the second action fails"
    assert len(warned(stopped.value.payload)) == 1 and "events" in warned(stopped.value.payload)[0]
    assert git(f.repo("home"), "status", "--porcelain") == ""


def test_apply_that_stops_with_a_failure_logs_what_it_did_first(story, monkeypatch):
    run = run_of(story, "story", "412", monkeypatch)
    real = apply_module.perform
    calls = []

    def perform(run_, unit):
        calls.append(unit.id)
        if len(calls) == 2:
            raise Fail(1, "the second action fails")
        return real(run_, unit)

    monkeypatch.setattr(apply_module, "perform", perform)
    with pytest.raises(Fail) as stopped:
        apply_module.apply(run)
    assert "warnings" not in stopped.value.payload
    logged = events(story)
    assert [(e["kind"], e["unit"]) for e in logged] == [("apply", "onStart-1")]
    assert logged[0]["line"].startswith("apply: onStart-1: ")
