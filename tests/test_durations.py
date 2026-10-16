"""`status` gives each unit's total duration from the ledger's times (and `next`, which shares the summaries)."""

from pathlib import Path

import yaml
from conftest import git, start_docs
from fab0_engine.ledger import parse
from fab0_engine.schedule import Unit
from test_gates import ONE_TASK, implement, record_gate, verdict
from test_run import BRIEF, DOCS_GH, do_spec, finish, handouts, start_work


def unit_of(result: dict, name: str) -> dict:
    return next(u for u in result["units"] if u["unit"] == name)


def retime(tree: Path, name: str, started: str | None, ended: str | None = None, run: str = "412") -> None:
    """Set the times of a ledger entry in a checkout, as if the unit had taken that long. None drops them."""
    path = tree / "work" / run / "ledger" / name
    entry = yaml.safe_load(path.read_text())
    entry.pop("started", None)
    entry.pop("ended", None)
    if started:
        entry["started"], entry["ended"] = started, ended
    path.write_text(yaml.safe_dump(entry, sort_keys=False))


def three_records(story) -> Path:
    """implement-T1: attempt 1 (4m 12s), its gate rejects (1m 5s), attempt 2 (3m 0s)."""
    do_spec(story, tasks=ONE_TASK)
    h = implement(story)
    tree = Path(h["branches"][-1]["worktree"])
    retime(tree, "implement-T1-1.yaml", "2026-10-15T10:00:00Z", "2026-10-15T10:04:12Z")
    record_gate(story, "412", "implement-T1", verdict("reject"))
    retime(tree, "implement-T1-1-gate.yaml", "2026-10-15T10:05:00Z", "2026-10-15T10:06:05Z")
    implement(story, attempt=2, text="x = 2\n")
    retime(tree, "implement-T1-2.yaml", "2026-10-15T10:10:00Z", "2026-10-15T10:13:00Z")
    return tree


def test_a_unit_has_the_sum_of_its_attempts_and_gate_decisions(story):
    three_records(story)
    for command in ("status", "next"):
        unit = unit_of(story.run(command, "412"), "implement-T1")
        assert unit["duration"] == 497
        assert unit["took"] == "8m 17s"


def test_a_unit_never_recorded_shows_no_duration(story):
    do_spec(story, tasks=ONE_TASK)
    unit = unit_of(story.run("status", "412"), "implement-T1")
    assert "duration" not in unit and "took" not in unit
    spec = unit_of(story.run("status", "412"), "spec")
    assert "duration" in spec and spec["took"]


def test_an_attempt_alone_counts(story):
    do_spec(story, tasks=ONE_TASK)
    tree = Path(implement(story)["branches"][-1]["worktree"])
    retime(tree, "implement-T1-1.yaml", "2026-10-15T10:00:00Z", "2026-10-15T10:00:42Z")
    unit = unit_of(story.run("status", "412"), "implement-T1")
    assert (unit["duration"], unit["took"]) == (42, "42s")


def test_an_entry_without_times_leaves_the_duration_out(story):
    tree = three_records(story)
    retime(tree, "implement-T1-1.yaml", None)  # written before times existed
    res = story.run("status", "412")
    unit = unit_of(res, "implement-T1")
    assert "duration" not in unit and "took" not in unit
    assert unit["state"], "nothing fails"
    old = tree / "work" / "412" / "ledger" / "implement-T1-1.yaml"
    assert "started" not in parse(old.read_bytes(), "old entry"), "an old entry is still valid"


def test_a_gate_decision_without_times_leaves_the_duration_out(story):
    tree = three_records(story)
    retime(tree, "implement-T1-1-gate.yaml", None)
    unit = unit_of(story.run("status", "412"), "implement-T1")
    assert "duration" not in unit and "took" not in unit


def test_a_clock_that_moved_back_counts_zero():
    unit = Unit(id="u", step=None, label="u", ctx=None)
    unit.history = [{"started": "2026-10-15T10:04:12Z", "ended": "2026-10-15T10:00:00Z"}]
    unit.gates = {1: {"started": "2026-10-15T10:00:00Z", "ended": "2026-10-15T10:01:05Z"}}
    assert unit.duration() == 65
    assert Unit(id="u", step=None, label="u", ctx=None).duration() is None


def gated_block_with_a_decision(factory):
    """The docs fixture's gated block `parallel-3`, whose command gate has approved; the decision took 1m 5s."""
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
    h = handouts(f.run("next", "docs-1", "--unit", "parallel-3"))["parallel-3"]
    tree = Path(h["worktree"])
    git(f.repo("home"), "worktree", "add", "-q", str(tree), h["branch"])
    record_gate(f, "docs-1", "parallel-3", exit=0)
    retime(tree, "parallel-3-1-gate.yaml", "2026-10-15T10:00:00Z", "2026-10-15T10:01:05Z", run="docs-1")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "--amend", "--no-edit")
    f.block_tree = tree
    return f


def test_a_gated_block_has_its_gate_decisions_duration_while_it_waits(factory):
    f = gated_block_with_a_decision(factory)
    block = unit_of(f.run("status", "docs-1"), "parallel-3")
    assert block["state"] == "waiting"
    assert (block["duration"], block["took"]) == (65, "1m 5s")


def test_a_gated_block_keeps_its_duration_once_it_is_done(factory):
    f = gated_block_with_a_decision(factory)
    f.merge("home", "docs/docs-1--parallel-3", "docs/docs-1")
    block = unit_of(f.run("status", "docs-1"), "parallel-3")
    assert block["state"] == "done"
    assert (block["duration"], block["took"]) == (65, "1m 5s")


def test_a_gated_block_keeps_its_duration_when_a_unit_moves_on(factory):
    f = gated_block_with_a_decision(factory)
    block_tree = f.block_tree
    (block_tree / "work" / "docs-1" / "pages.json").write_text('["intro"]')  # an input of a unit moves on
    git(block_tree, "commit", "-qam", "a new page")
    res = f.run("status", "docs-1")
    assert unit_of(res, "parallel-3--index")["state"] in ("ready", "running"), "the unit asks for a new attempt"
    block = unit_of(res, "parallel-3")
    assert block["state"] == "open"
    assert (block["duration"], block["took"]) == (65, "1m 5s")
