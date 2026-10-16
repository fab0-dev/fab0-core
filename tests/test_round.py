"""A round of `/fab0:run`: `next --apply` performs what is due and hands out from one pass over the tracker, `record`
reads no tracker, and the session records and commits a round's hand-outs in one turn."""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from conftest import ROOT, SCRIPT, Factory, git, reads, start_story, writes
from test_gates import TRIAGE_GH, classify, record_gate, verdict
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


def signal_round(story):
    story.edit("fab0.yaml", lambda d: d["tracker"]["fields"].update(signal="Signal"))
    two_task_run(story)
    story.gh_edit(lambda d: [d["issues"][f"acme/shop#{n}"].update(fields={}) for n in (500, 501)])
    return quiet_round(story)


def test_a_quiet_round_with_a_signal_field_gives_what_status_gives(story):
    asked, round_, status, res = signal_round(story)
    extra = [m for m, _ in tuples(round_) if (m, _) not in tuples(asked)]
    assert "user" in extra, "who is running"
    assert states(res) == states(status)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP, a follow-up: `cli.py` calls `make_tracker()` without `boards=True`, so the first read of "
    "a board field makes a second read of the run's item. Passing it needs T1's tests in tests/test_tracker.py "
    "changed.",
)
def test_a_quiet_round_with_a_signal_field_costs_one_tracker_call_more_than_status(story):
    asked, round_, _, _ = signal_round(story)
    assert len(round_) == len(asked) + 1, "S13: who is running, and nothing else"


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


def test_record_of_a_unit_behind_a_tracker_condition_reads_the_tracker_as_status_does(factory):
    """The fallback: a `when` on a tracker condition leaves the unit out of the offline run, so `record` reads the
    tracker once, as one evaluation (a follow-up gives `record` the unit from its saved hand-out instead)."""
    f = factory("triage").init_git(TRIAGE_GH)
    classify(f, {"severity": "high", "area": "billing"})
    record_gate(f, "7", "classify", exit=0)
    f.gh_edit(lambda d: d["issues"]["acme/support#7"]["labels"].extend(["severity-high", "area-billing"]))
    page = handouts(f.run("next", "7"))["page"]
    tree = Path(page["branches"][0]["worktree"])
    if not tree.exists():
        start_work(f, page)
    returned = tree / ".." / "page-return.json"
    returned.write_text(json.dumps({"summary": "Posted."}))
    f.reset_calls()
    f.run("status", "7")
    status = f.calls()
    f.reset_calls()
    res = f.run("record", "7", "page", "--return", str(returned))
    assert res["unit"] == "page" and res["verified"]
    calls = f.calls()
    assert calls and not writes(calls), "it reads, and writes nothing"
    no_repeats(calls)
    assert "user" not in {c["method"] for c in calls}, "and doesn't ask who is running"
    assert sorted(tuples(calls)) == sorted(tuples(status)), "the reads of one evaluation, as `status` makes them"


# ------------------------------------------------------------ S19, S20: the session's round


def test_the_skill_runs_a_round_in_four_turns():
    text = SKILL.read_text()
    loop = text[text.index("## The loop") : text.index("## At the end")]
    assert "fab0-resolve next <run> --apply" in loop
    assert not re.search(r"`fab0-resolve apply <run>`", loop), "no separate apply in the loop"
    assert not re.search(r"`fab0-resolve next <run>`", loop), "and no separate next"
    steps = re.findall(r"^\d+\. \*\*(.+?)\.?\*\*", loop, re.M)
    assert steps == ["Apply and hand out", "Checkouts", "Execute", "Record and commit"]
    checkouts = loop[loop.index("2. **Checkouts") : loop.index("3. **Execute")]
    assert "every checkout command of the round, for every hand-out, in one Bash call" in checkouts
    execute = loop[loop.index("3. **Execute") : loop.index("4. **Record and commit")]
    assert "<returns>/<unit>.json" not in execute, "no return file is written before the record turn"
    record = loop[loop.index("**Record and commit") :]
    assert "&&" in record and "one Bash call" in record, "chained, so a refused record commits nothing"
    assert "one message" in record, "a round's calls go in one message"
    assert "`line`" in loop and "applied" in loop, "what apply did, and the start and end lines, are still shown"
    chain = record[record.index("```bash") :]
    chain = chain[: chain.index("```", 3)]
    assert "<<'FAB0_RETURN'" in chain, "the return file is written inside the call, by a quoted heredoc"
    order = [
        chain.index(x)
        for x in ("cat > <returns>/<unit>.json", "fab0-resolve record", "git -C <worktree> add", "|| echo")
    ]
    assert order == sorted(order), "written, then recorded, then committed"
    assert "--return <returns>/<unit>.json" in chain, "record reads the file the call wrote"
    assert "<file>" not in record, "no unnamed return file: concurrent calls could share a path"
    assert "<returns>/<unit>.json" in record and "<returns>/<unit>-gate.json" in record, (
        "a file per unit and per gate, so a round's concurrent calls keep their files apart"
    )
    assert "<returns>/<unit>-gate.txt" in record, "a command gate's output has its own file"


def chained(f, unit: str, tree: Path, returned: Path, returns: dict) -> str:
    """The Bash call the skill makes for a hand-out: write the return, record it, and commit only when it was
    recorded. The form is the skill's own: a heredoc on the first line, the chain continued after it."""
    message = f"{unit} attempt 1 (fab0 test): Done."
    return (
        f"mkdir -p {returned.parent} && cat > {returned} <<'FAB0_RETURN' \\\n"
        f"  && {sys.executable} {SCRIPT} record 412 {unit} --return {returned} \\\n"
        f"  && git -C {tree} add -A && git -C {tree} commit -q -m '{message}' \\\n"
        f'  || echo "failed: exit $?"\n'
        f"{json.dumps(returns)}\n"
        f"FAB0_RETURN\n"
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
    ok, bad_unit = {"summary": "Done."}, "implement-T2"
    for unit, (repo, rel) in {
        "implement-T1": ("backend", "src/a.py"),
        "implement-T2": ("backend", "src/b.py"),
        "implement-T3": ("home", "help/booking.md"),
    }.items():
        tree = start_work(story, ready[unit])[repo]
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text("x = 1\n")
        returned = tree / ".." / f"{unit}-return.json"  # not written: the record call writes it
        work[unit] = (repo, tree, returned)

    def turn(unit: str, returns: dict | None = None) -> subprocess.CompletedProcess:
        _, tree, returned = work[unit]
        bad = {"nothing": "to say"}
        return call(story, chained(story, unit, tree, returned, returns or ({bad_unit: bad}.get(unit, ok))))

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
    again = turn("implement-T2", ok)  # the agent called again with the errors
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
