"""Runs: units, their states and blocking reasons, hand-outs and records, on the three fixture factories."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import git

STORY_GH = {
    "user": "po-user",
    "issues": {"acme/shop#412": {"title": "Extend a booking", "body": "Guests extend a booking.", "type": "Story"}},
}
SPEC = "# Extend a booking\n\n## Intent\n\nOne more night.\n\n## Scope\n\nBookings.\n\n## Scenarios\n\n- S1\n"
TASKS = """- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}
- {id: T2, title: Pricing, repo: backend, files: [src/b.py], depends_on: [T1]}
- {id: T3, title: Help page, repo: home, files: [help/booking.md]}
- {id: T4, title: Booking API errors, repo: backend, files: [src/a.py]}
"""


def states(result: dict) -> dict:
    return {u["unit"]: (u["state"], u.get("reason")) for u in result["units"]}


def handouts(result: dict) -> dict:
    return {h["unit"]: h for h in result["handouts"]}


def start_work(f, handout: dict) -> dict[str, Path]:
    """What a session does with a hand-out: create the missing branches, and a worktree for the unit's own."""
    trees = {}
    for b in handout["branches"]:
        path = f.repo(b["repo"])
        if "worktree" in b:
            if b["exists"]:
                git(path, "worktree", "add", "-q", b["worktree"], b["branch"])
            else:
                git(path, "worktree", "add", "-q", "-b", b["branch"], b["worktree"], b["from"])
            trees[b["repo"]] = Path(b["worktree"])
        elif (
            not b["exists"]
            and subprocess.run(["git", "-C", path, "rev-parse", "-q", "--verify", b["branch"]]).returncode
        ):
            git(path, "branch", b["branch"], b["from"])  # a sibling unit may have created it already
    return trees


def finish(f, run: str, handout: dict, trees: dict, files: dict | None = None, returned=None, exit=None, **kw):
    """Write the files, record the unit, and commit everything on its branch."""
    for (repo, rel), text in (files or {}).items():
        path = trees[repo] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    args = ["record", run, handout["unit"], *kw.get("extra", [])]
    if returned is not None:
        path = trees[handout["repo"]] / ".." / f"{handout['unit']}-return.json"
        path.write_text(json.dumps(returned))
        args += ["--return", str(path)]
    if exit is not None:
        args += ["--exit", str(exit)]
    res = f.run(*args, expect=kw.get("expect", 0))
    if kw.get("expect", 0) == 0:
        for tree in trees.values():
            git(tree, "add", "-A")
            git(tree, "commit", "-q", "-m", f"{handout['unit']} attempt {handout['attempt']}")
    return res


@pytest.fixture
def story(factory):
    f = factory("story").init_git(STORY_GH)
    return f


def started(f):
    f.branch("home", "fab0/412/base", "main")
    f.branch("backend", "fab0/412/base", "main")


def do_spec(f, tasks=TASKS, spec=SPEC):
    started(f)
    h = handouts(f.run("next", "412"))["spec"]
    trees = start_work(f, h)
    files = {("home", "specs/412.md"): spec, ("home", "work/412/tasks.yaml"): tasks}
    finish(f, "412", h, trees, files, {"summary": "Wrote the spec."})
    f.merge("home", "fab0/412/spec", "fab0/412/base")


def test_a_run_starts_with_its_run_branch(story):
    res = story.run("status", "412")
    assert res["kind"] == "start" and res["branch"] == "fab0/412/base"
    assert res["actions"][0] == "git.run-branch" and res["units"] == []


def test_the_first_step_is_ready_and_the_rest_wait_on_it(story):
    started(story)
    res = story.run("next", "412")
    assert states(res) == {
        "spec": ("ready", None),
        "foreach-tasks": ("blocked", {"order": "spec"}),
        "when": ("blocked", {"order": "spec"}),
    }
    h = handouts(res)["spec"]
    assert h["executor"] == "agent" and h["agent"] == "spec-author" and h["model"] == "opus"
    assert h["branches"] == [
        {
            "repo": "home",
            "branch": "fab0/412/spec",
            "from": "fab0/412/base",
            "exists": False,
            "worktree": h["branches"][0]["worktree"],
        }
    ]
    assert h["branches"][0]["worktree"].endswith(".git/fab0/worktrees/412/spec")
    assert "Guests extend a booking." in h["brief"] and "Handed out by fab0" in h["brief"]
    assert h["claims"] == {"home": ["specs/**", "work/**"]}
    assert states(story.run("status", "412"))["spec"] == ("running", None)


def test_a_recorded_step_waits_on_its_gate_until_merged(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    trees = start_work(story, h)
    files = {("home", "specs/412.md"): SPEC, ("home", "work/412/tasks.yaml"): TASKS}
    rec = finish(story, "412", h, trees, files, {"summary": "Wrote the spec."})
    entry = rec["recorded"]
    assert entry["step"] == "spec" and entry["attempt"] == 1 and entry["executor"] == "agent"
    assert entry["actor"] == "po-user" and entry["model"] == "opus" and entry["gate"] == {"type": "human"}
    assert entry["base"]["branch"] == "fab0/412/base" and len(entry["base"]["sha"]) == 40
    assert states(story.run("status", "412"))["spec"] == ("waiting", {"gate": "human"})
    story.merge("home", "fab0/412/spec", "fab0/412/base")
    assert states(story.run("status", "412"))["spec"] == ("done", None)


def test_questions_wait_for_a_person(story):
    started(story)
    h = handouts(story.run("next", "412"))["spec"]
    trees = start_work(story, h)
    finish(story, "412", h, trees, returned={"summary": "Unclear.", "questions": ["Which bookings?"]})
    assert states(story.run("status", "412"))["spec"] == ("waiting", {"questions": 1})


def test_record_checks_the_return_and_the_hand_out(story):
    started(story)
    assert "wasn't handed out" in story.run("record", "412", "spec", "--exit", "0", expect=4)["error"]
    h = handouts(story.run("next", "412"))["spec"]
    trees = start_work(story, h)
    res = finish(story, "412", h, trees, returned={"summary": "x", "mood": "fine"}, expect=4)
    assert "doesn't match its schema" in res["error"]
    git(trees["home"], "checkout", "-q", "--detach")
    res = finish(story, "412", h, trees, returned={"summary": "x"}, expect=4)
    assert "check out fab0/412/spec" in res["error"]


def test_foreach_units_follow_after_and_claims(story):
    do_spec(story)
    res = story.run("next", "412")
    assert list(states(res).items()) == [
        ("spec", ("done", None)),
        ("implement-T1", ("ready", None)),
        ("implement-T2", ("blocked", {"order": ["T1"]})),
        ("implement-T3", ("ready", None)),
        ("implement-T4", ("blocked", {"claim": "implement-T1"})),
        ("when", ("blocked", {"order": "foreach tasks"})),
    ]
    t1 = handouts(res)["implement-T1"]
    assert t1["repo"] == "backend" and t1["claims"] == {"backend": ["src/a.py"]}
    assert t1["branches"][0]["branch"] == "fab0/412/implement-T1" and t1["branches"][0]["from"] == "fab0/412/base"
    assert t1["element"]["title"] == "Booking API" and '"id": "T1"' in t1["brief"]
    trees = start_work(story, t1)
    finish(story, "412", t1, trees, {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"gate": "gate"})
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    s = states(story.run("status", "412"))
    assert s["implement-T1"] == ("done", None)
    assert s["implement-T2"] == ("ready", None)
    assert s["implement-T4"] == ("blocked", {"limit": "foreach max 2"}), "T3 is running and T2 is ready"


def test_a_missing_or_broken_list_blocks_the_foreach(story):
    do_spec(story, tasks="- {id: T1, title: A, repo: backend, files: [a]}\n- {title: no id}\n")
    assert states(story.run("status", "412"))["foreach-tasks"][1] == {
        "data": "tasks[1] has no usable 'id' (letters, digits, . _ -)"
    }


def test_invalid_dependencies_are_reported_not_guessed(factory):
    f = factory("story").init_git(STORY_GH)
    tasks = """- {id: T1, title: A, repo: backend, files: [a], depends_on: [T2]}
- {id: T2, title: B, repo: backend, files: [b], depends_on: [T1]}
- {id: T3, title: C, repo: backend, files: [c], depends_on: [T9]}
- {id: T4, title: D, repo: backend, files: [d]}
"""
    do_spec(f, tasks=tasks)
    s = states(f.run("status", "412"))
    assert s["implement-T1"] == ("blocked", {"invalid-dependency": "after makes a cycle: T1 -> T2 -> T1"})
    assert s["implement-T3"] == ("blocked", {"invalid-dependency": "after: no element T9"})
    assert s["implement-T4"] == ("ready", None)


def test_when_runs_its_steps_only_if_the_condition_holds(story):
    one = "- {id: T1, title: A, repo: backend, files: [a]}\n"
    do_spec(story, tasks=one, spec=SPEC + "\n## Docs\n\nA note.\n")
    t1 = handouts(story.run("next", "412"))["implement-T1"]
    finish(story, "412", t1, start_work(story, t1), {("backend", "a"): "1"}, {"summary": "Done."})
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    res = story.run("next", "412")
    assert states(res)["docs-note"] == ("ready", None)
    note = handouts(res)["docs-note"]
    assert note["claims"] == {"home": ["docs/**"]} and note["model"] == "haiku"
    finish(story, "412", note, start_work(story, note), {("home", "docs/notes/412.md"): "x"}, {"summary": "Noted."})
    assert story.run("status", "412")["kind"] == "units", "gate none: merged by apply, not yet"
    story.merge("home", "fab0/412/docs-note", "fab0/412/base")
    end = story.run("status", "412")
    assert end["kind"] == "end" and end["actions"][-1] == "tracker.close"


def test_without_the_section_the_run_ends_after_the_tasks(story):
    do_spec(story, tasks="- {id: T1, title: A, repo: backend, files: [a]}\n")
    t1 = handouts(story.run("next", "412"))["implement-T1"]
    finish(story, "412", t1, start_work(story, t1), {("backend", "a"): "1"}, {"summary": "Done."})
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    res = story.run("status", "412")
    assert res["kind"] == "end" and "docs-note" not in states(res)


def test_parallel_project_limit(story):
    story.edit("fab0.yaml", lambda d: d["parallel"].update(max=1))
    do_spec(story)
    s = states(story.run("status", "412"))
    assert s["implement-T1"] == ("ready", None)
    assert s["implement-T3"] == ("blocked", {"limit": "parallel.max 1"})


# ---------------------------------------------------------------- docs: manual runs, gated blocks, commands

DOCS_GH = {"user": "docs-user", "issues": {}}
PAGES = json.dumps([{"slug": "intro", "heading": "Intro", "file": "docs/intro.md"}])


@pytest.fixture
def docs(factory):
    return factory("docs").init_git(DOCS_GH)


def test_a_manual_run_id_must_match_its_pattern(docs):
    assert "doesn't match" in docs.run("status", "release-3", expect=2)["error"]


def test_a_gated_block_has_its_own_branch(docs):
    docs.branch("home", "docs/docs-1", "main")
    brief = handouts(docs.run("next", "docs-1"))["brief"]
    assert brief["branch"] == "docs/docs-1--brief"
    files = {
        ("home", "briefs/docs-1.md"): "# B\n## Intent\n## Findings\n## Outline\n",
        ("home", "work/docs-1/pages.json"): PAGES,
    }
    finish(docs, "docs-1", brief, start_work(docs, brief), files, {"summary": "Planned."})
    docs.merge("home", "docs/docs-1--brief", "docs/docs-1")
    write = handouts(docs.run("next", "docs-1"))["write-intro"]
    assert write["claims"] == {"home": ["docs/intro.md"]}
    finish(docs, "docs-1", write, start_work(docs, write), {("home", "docs/intro.md"): "# Intro\n"}, {"summary": "W"})
    docs.merge("home", "docs/docs-1--write-intro", "docs/docs-1")
    res = docs.run("next", "docs-1")
    s = states(res)
    assert s["parallel-3"] == ("open", None)
    assert s["parallel-3--spelling"] == ("ready", None)
    assert s["parallel-3--links"] == ("ready", None)
    assert s["parallel-3--index"] == ("blocked", {"order": ["spelling"]})
    spelling = handouts(res)["parallel-3--spelling"]
    assert spelling["executor"] == "command" and spelling["command"] == "codespell docs"
    assert [(b["branch"], b["from"], b["exists"]) for b in spelling["branches"]] == [
        ("docs/docs-1--parallel-3", "docs/docs-1", False),
        ("docs/docs-1--parallel-3--spelling", "docs/docs-1--parallel-3", False),
    ]
    finish(docs, "docs-1", spelling, start_work(docs, spelling), exit=0)
    links = handouts(res)["parallel-3--links"]
    finish(docs, "docs-1", links, start_work(docs, links), exit=1)
    s = states(docs.run("status", "docs-1"))
    assert s["parallel-3--spelling"] == ("waiting", {"gate": "merge"})
    assert s["parallel-3--links"] == ("blocked", {"failed": "exit 1"})
    docs.merge("home", "docs/docs-1--parallel-3--spelling", "docs/docs-1--parallel-3")
    s = states(docs.run("status", "docs-1"))
    assert s["parallel-3--spelling"] == ("done", None) and s["parallel-3--index"] == ("ready", None)


def test_a_command_is_recorded_with_its_exit_code(docs):
    docs.branch("home", "docs/docs-1", "main")
    brief = handouts(docs.run("next", "docs-1"))["brief"]
    res = finish(docs, "docs-1", brief, start_work(docs, brief), exit=0, expect=2)
    assert "recorded with --return" in res["error"]


# ---------------------------------------------------------------- triage: no pull requests, a ledger branch

TRIAGE_GH = {
    "user": "support-user",
    "issues": {"acme/support#7": {"title": "Checkout fails", "body": "500 on pay.", "labels": ["needs-triage"]}},
}


def test_a_run_without_files_works_on_the_ledger_branch(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    res = f.run("next", "7")
    assert res["kind"] == "units", "flat branching: no run branch to wait for"
    assert states(res)["classify"] == ("ready", None)
    h = handouts(res)["classify"]
    assert h["branch"] == "fab0/ledger" and h["claims"] == {}
    assert h["branches"][0]["from"] == "main" and not h["branches"][0]["exists"]
    assert "500 on pay." in h["brief"]
    assert h["return_schema"]["properties"]["data"]["required"] == ["severity", "area"]
    bad = finish(f, "7", h, start_work(f, h), returned={"summary": "x", "data": {"severity": "dire"}}, expect=4)
    assert "doesn't match its schema" in bad["error"]
    trees = {"home": Path(h["branches"][0]["worktree"])}
    finish(f, "7", h, trees, returned={"summary": "Billing.", "data": {"severity": "high", "area": "billing"}})
    s = states(f.run("status", "7"))
    assert s["classify"] == ("waiting", {"gate": "gate"}), "its auto gate decides from step R2d"
    assert s["label"] == ("blocked", {"order": "classify"})
