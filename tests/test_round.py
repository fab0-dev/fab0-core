"""A round of `/fab0:run`: `next --apply` performs what is due and hands out from one pass over the tracker, `record`
reads no tracker, and the session records and commits a round's hand-outs in one turn."""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from conftest import ROOT, SCRIPT, Factory, git, reads, start_story
from test_gates import record_gate, verdict
from test_run import SPEC, STORY_GH, do_spec, handouts, start_work, states
from test_tracker import no_repeats, two_task_run

THREE = """- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}
- {id: T2, title: Pricing, repo: backend, files: [src/b.py]}
- {id: T3, title: Help page, repo: home, files: [help/booking.md], depends_on: [T1]}
"""
INDEPENDENT = """- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}
- {id: T2, title: Pricing, repo: backend, files: [src/b.py]}
- {id: T3, title: Help page, repo: home, files: [help/booking.md]}
"""
SKILL = ROOT / "plugins" / "fab0" / "skills" / "run" / "SKILL.md"


def scrub(value, *roots: Path):
    """A result as text, with the temporary directories each factory lives in taken out."""
    text = json.dumps(value, indent=1, sort_keys=True)
    for root in roots:
        text = text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")
    return text


def second_factory(name: str, tmp_path: Path, gh: dict) -> Factory:
    folder = tmp_path / "second"
    folder.mkdir()
    return Factory(name, folder).init_git(gh)


def implemented(f, unit: str, rel: str, repo: str = "backend") -> dict:
    """What a session does with a unit's hand-out: its worktree, its files, its record and its commit."""
    h = handouts(f.run("next", "412", "--unit", unit))[unit]
    trees = start_work(f, h)
    (trees[repo] / rel).parent.mkdir(parents=True, exist_ok=True)
    (trees[repo] / rel).write_text("x = 1\n")
    returned = trees[repo] / ".." / f"{unit}-return.json"
    returned.write_text(json.dumps({"summary": "Done."}))
    f.run("record", "412", unit, "--return", str(returned))
    git(trees[repo], "add", "-A")
    git(trees[repo], "commit", "-q", "-m", f"{unit} attempt 1")
    return h


# ------------------------------------------------------------ S12, S13: next --apply


def test_next_apply_gives_what_apply_gives_and_what_next_gives_after_it(story, tmp_path):
    other = second_factory("story", tmp_path, STORY_GH)
    combined = story.run("next", "412", "--apply")
    applied = other.run("apply", "412")
    after = other.run("next", "412")
    assert [a["action"] for a in combined["applied"]] == ["git.run-branch", "pr.open-run", "tracker.field"]
    assert scrub(combined["applied"], story.home.parent) == scrub(applied["applied"], other.home.parent)
    assert [h["unit"] for h in combined["handouts"]] == ["spec"], "something was due, and a unit is ready"
    assert scrub(combined["handouts"], story.home.parent) == scrub(after["handouts"], other.home.parent)
    assert scrub(combined["units"], story.home.parent) == scrub(after["units"], other.home.parent)
    assert combined["kind"] == after["kind"] == "units"
    assert story.run("next", "412")["handouts"][0]["unit"] == "spec", "claimed, and handed out again while it runs"


def test_next_without_apply_performs_nothing(story):
    res = story.run("next", "412")
    assert "applied" not in res and story.run("status", "412")["units"][0]["state"] == "ready"


def quiet_round(f) -> tuple[list[dict], list[dict], dict, dict]:
    """The tracker reads of `status` and of `next --apply` on a run where nothing is due."""
    for _ in range(3):  # until nothing is due and every signal is up to date
        if not f.run("apply", "412")["applied"]:
            break
    assert f.run("apply", "412")["applied"] == []
    f.reset_calls()
    status = f.run("status", "412")
    asked = reads(f.calls())
    f.reset_calls()
    res = f.run("next", "412", "--apply")
    assert res["applied"] == []
    no_repeats(f.calls())
    return asked, reads(f.calls()), status, res


def test_a_quiet_round_costs_one_tracker_call_more_than_status(story):
    two_task_run(story)
    asked, round_, status, res = quiet_round(story)
    extra = [m for m, a in tuples(round_) if (m, a) not in tuples(asked)]
    assert extra == ["user"] and len(round_) == len(asked) + 1, "who is running"
    assert states(res) == states(status)


def tuples(calls: list[dict]) -> list[tuple]:
    return [(c["method"], c["args"]) for c in calls]


def test_a_quiet_round_with_a_signal_field_reads_the_signals_once_too(story):
    story.edit("fab0.yaml", lambda d: d["tracker"]["fields"].update(signal="Signal"))
    two_task_run(story)
    story.gh_edit(lambda d: [d["issues"][f"acme/shop#{n}"].update(fields={}) for n in (500, 501)])
    asked, round_, status, res = quiet_round(story)
    extra = [c for c in tuples(round_) if c not in tuples(asked)]
    assert sorted(m for m, _ in extra) == ["item", "user"], "who is running, and the item again, with its board entries"
    assert states(res) == states(status)


# ------------------------------------------------------------ S14, S15, S16


def test_apply_failing_inside_next_apply_hands_out_and_claims_nothing(story):
    story.gh_edit(lambda d: d.update(boards={"acme/7": {"fields": {"Other": {}}}}))  # onStart sets State
    failed = story.run("apply", "412", expect=1)
    res = story.run("next", "412", "--apply", expect=1)
    assert res == failed and "State" in res["error"]
    assert "handouts" not in res
    pending = Path(git(story.repo("home"), "rev-parse", "--path-format=absolute", "--git-common-dir")) / "fab0"
    assert not (pending / "pending").exists(), "nothing was claimed or saved"
    assert git(story.repo("home"), "branch", "--list", "fab0/412/spec") == ""


def test_unit_limits_only_the_hand_outs(story):
    do_spec(story, tasks=THREE)
    implemented(story, "implement-T2", "src/b.py")
    record_gate(story, "412", "implement-T2", verdict("approve"))
    res = story.run("next", "412", "--apply", "--unit", "implement-T1")
    assert {"unit": "implement-T2", "merge": "merged into backend:fab0/412/base"} in res["applied"], "T2's, too"
    assert [h["unit"] for h in res["handouts"]] == ["implement-T1"], "T3 is blocked, T2 is done: only T1 was asked"
    assert states(res)["implement-T2"] == ("done", None)


def test_a_unit_that_is_not_ready_is_refused_and_the_result_lists_what_apply_did(story):
    do_spec(story, tasks=THREE)
    implemented(story, "implement-T2", "src/b.py")
    record_gate(story, "412", "implement-T2", verdict("approve"))
    res = story.run("next", "412", "--apply", "--unit", "implement-T3", expect=4)
    assert "implement-T3 is not ready" in res["error"]
    assert res["unit"]["reason"] == {"order": ["T1"]}
    assert {"unit": "implement-T2", "merge": "merged into backend:fab0/412/base"} in res["applied"]
    assert git(story.repo("backend"), "show", "fab0/412/base:src/b.py") == "x = 1", "and it did them"


def test_a_run_waiting_for_its_pull_request_or_done_hands_out_nothing(story):
    do_spec(story, tasks="- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n")
    implemented(story, "implement-T1", "src/a.py")
    record_gate(story, "412", "implement-T1", verdict("approve"))
    res = story.run("next", "412", "--apply")
    assert res["kind"] == "waiting" and res["handouts"] == []
    assert any(a.get("action") == "pr.ready" for a in res["applied"]), "the end of the run was due"
    again = story.run("next", "412", "--apply")
    assert again["kind"] == "waiting" and again["handouts"] == [] and again["applied"] == []
    story.merge("home", "fab0/412/base", "main")
    story.merge("backend", "fab0/412/base", "main")
    done = story.run("next", "412", "--apply")
    assert done["kind"] == "done" and done["handouts"] == []


# ------------------------------------------------------------ S17, S18: record reads no tracker


def spec_files(f) -> tuple[dict, dict]:
    start_story(f)
    h = handouts(f.run("next", "412"))["spec"]
    trees = start_work(f, h)
    (trees["home"] / "specs").mkdir()
    (trees["home"] / "specs" / "412.md").write_text(SPEC)
    (trees["home"] / "work" / "412").mkdir(parents=True)
    (trees["home"] / "work" / "412" / "tasks.yaml").write_text(INDEPENDENT)
    returned = trees["home"] / ".." / "spec-return.json"
    returned.write_text(json.dumps({"summary": "Wrote the spec."}))
    return h, {"tree": trees["home"], "return": returned}


def test_record_makes_no_tracker_call(story):
    h, made = spec_files(story)
    story.reset_calls()
    res = story.run("record", "412", "spec", "--return", str(made["return"]))
    assert story.calls() == []
    assert res["verified"] and res["recorded"]["summary"] == "Wrote the spec." and res["line"].startswith("spec")
    assert Path(res["entry"]).is_file()


def test_record_of_a_failed_check_reads_nothing_either(story):
    h, made = spec_files(story)
    (made["tree"] / "specs" / "412.md").unlink()
    story.reset_calls()
    res = story.run("record", "412", "spec", "--return", str(made["return"]))
    assert story.calls() == [] and res["verified"] is False and "spec" in res["failed"]


def test_record_of_a_foreach_unit_and_of_a_gate_reads_nothing(story):
    do_spec(story, tasks=THREE)
    h = handouts(story.run("next", "412", "--unit", "implement-T1"))["implement-T1"]
    trees = start_work(story, h)
    (trees["backend"] / "src").mkdir()
    (trees["backend"] / "src" / "a.py").write_text("x = 1\n")
    returned = trees["backend"] / ".." / "T1-return.json"
    returned.write_text(json.dumps({"summary": "Done."}))
    story.reset_calls()
    res = story.run("record", "412", "implement-T1", "--return", str(returned))
    assert story.calls() == [] and res["unit"] == "implement-T1" and res["attempt"] == 1
    git(trees["backend"], "add", "-A")
    git(trees["backend"], "commit", "-q", "-m", "T1")
    gate = handouts(story.run("next", "412", "--unit", "implement-T1"))["implement-T1"]
    verdict_file = Path(gate["worktree"]).parent / "verdict.json"
    verdict_file.write_text(json.dumps(verdict("approve")))
    story.reset_calls()
    res = story.run("record", "412", "implement-T1", "--gate", "--return", str(verdict_file))
    assert story.calls() == [] and res["decision"] == "approve"


def test_record_with_several_pipelines_reads_only_the_runs_item(story):
    fix = {**story.read("pipelines/story.yaml"), "pipeline": "fix", "trigger": {"issue": {"type": "Bug"}}}
    (story.home / ".fab0" / "pipelines" / "fix.yaml").write_text(yaml.safe_dump(fix, sort_keys=False))
    story.edit("fab0.yaml", lambda d: d.update(pipelines=["story", "fix"]))
    h, made = spec_files(story)
    story.reset_calls()
    res = story.run("record", "412", "spec", "--return", str(made["return"]))
    assert [(c["method"], c["args"]) for c in story.calls()] == [("item", ["acme/shop", 412, False])]
    assert res["verified"]


# ------------------------------------------------------------ S19, S20: the session's round


def test_the_skill_runs_a_round_in_four_turns():
    text = SKILL.read_text()
    loop = text[text.index("## The loop") : text.index("## At the end")]
    assert "fab0-resolve next <run> --apply" in loop
    assert not re.search(r"`fab0-resolve apply <run>`", loop), "no separate apply in the loop"
    assert not re.search(r"`fab0-resolve next <run>`", loop), "and no separate next"
    steps = re.findall(r"^\d+\. \*\*(.+?)\.?\*\*", loop, re.M)
    assert steps == ["Apply and hand out", "Checkouts", "Execute", "Record and commit"]
    record = loop[loop.index("**Record and commit") :]
    assert "&&" in record and "one Bash call" in record, "chained, so a refused record commits nothing"
    assert "one message" in record, "a round's calls go in one message"
    assert "`line`" in loop and "applied" in loop, "what apply did, and the start and end lines, are still shown"


def chained(f, unit: str, tree: Path, returned: Path) -> str:
    """The Bash call the skill makes for a hand-out: record, and commit only when it was recorded."""
    message = f"{unit} attempt 1 (fab0 test): Done."
    return (
        f"{sys.executable} {SCRIPT} record 412 {unit} --return {returned}"
        f" && git -C {tree} add -A && git -C {tree} commit -q -m '{message}'"
        f' || echo "failed: exit $?"'
    )


def call(f, command: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "FAB0_GH_FAKE": str(f.gh_path)}
    return subprocess.run(["bash", "-c", command], cwd=f.home, capture_output=True, text=True, env=env)


def test_a_refused_record_commits_nothing_and_the_others_do(story):
    story.edit("pipelines/story.yaml", lambda d: d["steps"][1].update(max=3))
    do_spec(story, tasks=INDEPENDENT)
    round_ = story.run("next", "412", "--apply")
    ready = {h["unit"]: h for h in round_["handouts"]}
    assert sorted(ready) == ["implement-T1", "implement-T2", "implement-T3"]
    work = {}
    for unit, (repo, rel) in {
        "implement-T1": ("backend", "src/a.py"),
        "implement-T2": ("backend", "src/b.py"),
        "implement-T3": ("home", "help/booking.md"),
    }.items():
        tree = start_work(story, ready[unit])[repo]
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text("x = 1\n")
        returned = tree / ".." / f"{unit}-return.json"
        returned.write_text(json.dumps({"nothing": "to say"} if unit == "implement-T2" else {"summary": "Done."}))
        work[unit] = (repo, tree, returned)

    def turn(unit: str) -> subprocess.CompletedProcess:
        _, tree, returned = work[unit]
        return call(story, chained(story, unit, tree, returned))

    with ThreadPoolExecutor(3) as pool:  # a round's calls go in one message
        out = dict(zip(work, pool.map(turn, work), strict=True))
    for unit in ("implement-T1", "implement-T3"):
        repo, tree, _ = work[unit]
        assert '"verified": true' in out[unit].stdout and "failed: exit" not in out[unit].stdout, out[unit].stdout
        assert git(tree, "log", "-1", "--format=%s") == f"{unit} attempt 1 (fab0 test): Done."
        assert git(tree, "status", "--short") == ""
    repo, tree, returned = work["implement-T2"]
    assert "doesn't match its schema" in out["implement-T2"].stdout and "failed: exit 4" in out["implement-T2"].stdout
    assert git(tree, "log", "-1", "--format=%s") != "implement-T2 attempt 1 (fab0 test): Done.", "not committed"
    assert not list((tree / "work" / "412" / "ledger").glob("implement-T2-*")), "and no entry was written"
    returned.write_text(json.dumps({"summary": "Done."}))  # the agent called again with the errors
    again = turn("implement-T2")
    assert '"verified": true' in again.stdout and "failed: exit" not in again.stdout
    assert git(tree, "log", "-1", "--format=%s") == "implement-T2 attempt 1 (fab0 test): Done."


def test_a_round_of_next_apply_needs_no_other_tracker_pass(story):
    """The round's one tracker pass: `next --apply` reads no more than `apply` then `next` did."""
    two_task_run(story)
    story.reset_calls()
    story.run("next", "412", "--apply")
    combined = len(reads(story.calls()))
    story.reset_calls()
    story.run("apply", "412")
    story.run("next", "412")
    separate = len(reads(story.calls()))
    assert combined < separate
