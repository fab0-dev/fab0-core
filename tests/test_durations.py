"""`status` gives each unit's total duration from the ledger's times (and `next`, which shares the summaries)."""

from pathlib import Path

import yaml
from fab0_engine.schedule import Unit
from test_gates import ONE_TASK, implement, record_gate, verdict
from test_run import do_spec


def unit_of(result: dict, name: str) -> dict:
    return next(u for u in result["units"] if u["unit"] == name)


def retime(tree: Path, name: str, started: str | None, ended: str | None = None) -> None:
    """Set the times of a ledger entry in a checkout, as if the unit had taken that long. None drops them."""
    path = tree / "work" / "412" / "ledger" / name
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
