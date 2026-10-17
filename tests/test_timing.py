"""Progress: the start line of a hand-out, the end line and times of a record, and the times in the ledger."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import git, start_docs
from fab0_engine.ledger import now, parse, seconds_between, took
from test_gates import ONE_TASK, TRIAGE_GH, classify, implement, record_gate, verdict
from test_rework import implement_t1
from test_run import BRIEF, DOCS_GH, PAGES, do_spec, finish, handouts, start_work

TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def pending_file(f, run: str, unit: str, gate: bool = False) -> Path:
    return f.repo("home") / ".git" / "fab0" / "pending" / run / f"{unit}{'-gate' if gate else ''}.json"


def edit_pending(path: Path, **changes) -> None:
    data = json.loads(path.read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data))


def record_gate_now(f, unit: str, verdict_: dict, started: str | None, **edit) -> dict:
    """Record a handed-out gate with its saved start changed, as a session would after a while (or an old engine)."""
    g = handouts(f.run("next", "412", "--unit", unit))[unit]
    edit_pending(pending_file(f, "412", unit, gate=True), started=started)
    tree = Path(g["worktree"])
    if not tree.exists():
        git(f.repo(g["repo"]), "worktree", "add", "-q", str(tree), g["branch"])
    (tree.parent / "verdict.json").write_text(json.dumps(verdict_))
    return f.run("record", "412", unit, "--gate", "--return", str(tree.parent / "verdict.json"))


def ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- formatting


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (42, "42s"),
        (59, "59s"),
        (60, "1m 0s"),
        (65, "1m 5s"),
        (252, "4m 12s"),
        (497, "8m 17s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (3780, "1h 3m"),
        (90000, "25h 0m"),
        (-5, "0s"),
    ],
)
def test_took_formats_a_duration(seconds, text):
    assert took(seconds) == text


def test_a_duration_is_never_negative():
    assert seconds_between("2026-10-15T10:00:00Z", "2026-10-15T10:04:12Z") == 252
    assert seconds_between("2026-10-15T10:04:12Z", "2026-10-15T10:00:00Z") == 0
    assert TIME.match(now())


# ---------------------------------------------------------------- start lines


def test_an_agent_hand_out_has_a_start_line(story):
    do_spec(story, tasks=ONE_TASK)
    h = handouts(story.run("next", "412"))["implement-T1"]
    assert h["line"] == "implement-T1, attempt 1: implementer on sonnet, branch fab0/412/implement-T1"
    assert TIME.match(json.loads(pending_file(story, "412", "implement-T1").read_text())["started"])
    assert "started" not in h and "brief" in h, "additions only: the hand-out keeps its fields"


def test_a_rework_hand_out_names_its_attempt(story):
    do_spec(story, tasks=ONE_TASK)
    implement_t1(story, {("backend", "src/a.py"): "x = 1\n", ("backend", "setup.cfg"): "[x]\n"})
    h = handouts(story.run("next", "412"))["implement-T1"]
    assert h["line"].startswith("implement-T1, attempt 2: implementer on sonnet, branch ")


def test_a_gate_hand_out_has_a_start_line(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    g = handouts(story.run("next", "412"))["implement-T1"]
    assert g["gate"]
    assert g["line"] == "gate of implement-T1, attempt 1: reviewer on opus, branch fab0/412/implement-T1"
    assert TIME.match(json.loads(pending_file(story, "412", "implement-T1", gate=True).read_text())["started"])


def test_commands_and_tools_have_start_lines_and_actions_none(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    classify(f, {"severity": "high", "area": "billing"})
    gate = handouts(f.run("next", "7"))["classify"]
    assert gate["executor"] == "command"
    assert gate["line"] == f"gate of classify, attempt 1: command `{gate['command']}`, branch fab0/7/classify"
    record_gate(f, "7", "classify", exit=0)
    res = f.run("next", "7")
    assert "line" not in handouts(res)["label"], "an action hand-out has no start line"
    f.gh_edit(lambda d: d["issues"]["acme/support#7"]["labels"].extend(["severity-high", "area-billing"]))
    page = handouts(f.run("next", "7"))["page"]
    assert page["executor"] == "tool"
    assert page["line"] == f"page, attempt 1: tool {page['tool']}, branch fab0/7/page"


# ---------------------------------------------------------------- end lines and times


def test_a_verified_unit_gets_times_and_an_end_line(story):
    do_spec(story, tasks=ONE_TASK)
    h = handouts(story.run("next", "412"))["implement-T1"]
    edit_pending(pending_file(story, "412", "implement-T1"), started=ago(252))
    res = finish(story, "412", h, start_work(story, h), {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})
    assert TIME.match(res["started"]) and TIME.match(res["ended"])
    assert res["duration"] == seconds_between(res["started"], res["ended"]) >= 252
    assert res["took"] == took(res["duration"])
    assert res["line"] == f"implement-T1, attempt 1: verified, {res['took']}"
    assert res["recorded"]["started"] == res["started"] and res["recorded"]["ended"] == res["ended"]
    assert "duration" not in res["recorded"], "the ledger keeps the two times only"


def test_questions_are_counted_in_the_end_line(story):
    do_spec(story, tasks=ONE_TASK)
    h = handouts(story.run("next", "412"))["implement-T1"]
    returned = {"summary": "Stuck.", "questions": ["Which one?", "Why?"]}
    res = finish(story, "412", h, start_work(story, h), {("backend", "src/a.py"): "1"}, returned)
    assert res["line"].startswith("implement-T1, attempt 1: verified, 2 questions, ")


def test_a_unit_that_fails_its_checks_says_so(story):
    do_spec(story, tasks=ONE_TASK)
    res = implement_t1_result(story, {("backend", "src/a.py"): "x = 1\n", ("backend", "setup.cfg"): "[x]\n"})
    assert res["verified"] is False and res["failed"] == ["claims:backend"]
    assert res["line"] == f"implement-T1, attempt 1: not verified (claims:backend), {res['took']}"


def implement_t1_result(f, files) -> dict:
    h = handouts(f.run("next", "412"))["implement-T1"]
    return finish(f, "412", h, start_work(f, h), files, {"summary": "Done."})


def test_a_command_end_line_gives_the_exit_code(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    brief = handouts(f.run("next", "docs-1"))["brief"]
    files = {("home", "briefs/docs-1.md"): BRIEF, ("out", "pages.json"): PAGES}
    finish(f, "docs-1", brief, start_work(f, brief), files, {"summary": "Planned."})
    f.merge("home", "docs/docs-1--brief", "docs/docs-1")
    write = handouts(f.run("next", "docs-1"))["write-intro"]
    finish(f, "docs-1", write, start_work(f, write), {("home", "docs/intro.md"): "# Intro\n"}, {"summary": "W"})
    f.merge("home", "docs/docs-1--write-intro", "docs/docs-1")
    spelling = handouts(f.run("next", "docs-1"))["parallel-3--spelling"]
    assert spelling["line"].startswith("parallel-3--spelling, attempt 1: command `codespell docs`, branch ")
    res = finish(f, "docs-1", spelling, start_work(f, spelling), exit=0)
    assert res["line"] == f"parallel-3--spelling, attempt 1: verified, exit 0, {res['took']}"


def test_a_gate_gets_times_and_an_end_line(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    res = record_gate_now(story, "implement-T1", verdict("reject"), ago(65))
    assert res["duration"] == seconds_between(res["started"], res["ended"]) >= 65
    assert res["line"] == f"gate of implement-T1, attempt 1: rejected, {res['took']}"
    assert res["recorded"]["started"] == res["started"] and res["recorded"]["ended"] == res["ended"]


@pytest.mark.parametrize(("decision", "word"), [("approve", "approved"), ("escalate", "escalated")])
def test_a_gate_end_line_says_its_decision(story, decision, word):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    res = record_gate(story, "412", "implement-T1", verdict(decision))
    assert res["line"] == f"gate of implement-T1, attempt 1: {word}, {res['took']}"


def test_a_command_gate_gives_the_decision_and_the_exit_code(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    classify(f, {"severity": "high", "area": "billing"})
    res = record_gate(f, "7", "classify", exit=1, output="too vague\n")
    assert res["line"] == f"gate of classify, attempt 1: rejected, exit 1, {res['took']}"


# ---------------------------------------------------------------- the ledger


def test_the_ledger_entry_holds_the_times_and_is_valid(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    for name in ("implement-T1-1", "implement-T1-1-gate"):
        entry = parse(json.dumps(story.fact("412", name)), name)
        assert TIME.match(entry["started"]) and TIME.match(entry["ended"])
        assert seconds_between(entry["started"], entry["ended"]) >= 0


def test_the_schema_refuses_a_time_that_isnt_utc(story):
    do_spec(story, tasks=ONE_TASK)
    entry = implement_t1_result(story, {("backend", "src/a.py"): "x\n"})["recorded"]
    parse(json.dumps({**entry, "started": "2026-10-15T10:00:00Z"}), "ok")
    for bad in ("2026-10-15 10:00:00", "2026-10-15T10:00:00+02:00", "yesterday"):
        with pytest.raises(Exception, match="invalid ledger entry"):
            parse(json.dumps({**entry, "started": bad}), "bad")


# ---------------------------------------------------------------- refused and repeated hand-outs


def test_a_refused_record_keeps_the_start(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    h = handouts(f.run("next", "7"))["classify"]
    trees = start_work(f, h)
    path = pending_file(f, "7", "classify")
    edit_pending(path, started="2026-10-15T10:00:00Z")
    bad = finish(f, "7", h, trees, returned={"summary": "x", "data": {"severity": "dire"}}, expect=4)
    assert "doesn't match its schema" in bad["error"]
    assert "classify-1" not in f.facts("7"), "a refused record wrote no entry"
    assert json.loads(path.read_text())["started"] == "2026-10-15T10:00:00Z"
    res = finish(f, "7", h, trees, returned={"summary": "B.", "data": {"severity": "high", "area": "billing"}})
    assert res["recorded"]["started"] == "2026-10-15T10:00:00Z" and TIME.match(res["recorded"]["ended"])
    assert res["duration"] == seconds_between("2026-10-15T10:00:00Z", res["ended"])


def test_a_unit_handed_out_again_starts_again(story):
    do_spec(story, tasks=ONE_TASK)
    handouts(story.run("next", "412"))
    path = pending_file(story, "412", "implement-T1")
    edit_pending(path, started="2026-10-15T10:00:00Z")
    h = handouts(story.run("next", "412"))["implement-T1"]
    started = json.loads(path.read_text())["started"]
    assert started != "2026-10-15T10:00:00Z" and TIME.match(started)
    res = finish(story, "412", h, start_work(story, h), {("backend", "src/a.py"): "x\n"}, {"summary": "D."})
    assert res["started"] == started


# ---------------------------------------------------------------- entries without times


def test_a_hand_out_saved_before_times_is_recorded_without_them(story):
    do_spec(story, tasks=ONE_TASK)
    h = handouts(story.run("next", "412"))["implement-T1"]
    edit_pending(pending_file(story, "412", "implement-T1"), started=None)
    res = finish(story, "412", h, start_work(story, h), {("backend", "src/a.py"): "x\n"}, {"summary": "D."})
    assert TIME.match(res["ended"])
    assert not {"started", "duration", "took"} & res.keys()
    assert "started" not in res["recorded"] and "ended" not in res["recorded"]
    assert res["line"] == "implement-T1, attempt 1: verified, time unknown"
    parse(json.dumps(res["recorded"]), "old")
    assert story.run("status", "412")["kind"]


def test_a_gate_saved_before_times_is_recorded_without_them(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    res = record_gate_now(story, "implement-T1", verdict("approve"), None)
    assert not {"started", "duration", "took"} & res.keys()
    assert res["line"] == "gate of implement-T1, attempt 1: approved, time unknown"
    assert "started" not in res["recorded"]
