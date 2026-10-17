"""Runs: units, their states and blocking reasons, hand-outs and records, on the three fixture factories."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import STORY_GH, git, start_docs, start_story

SPEC = "# Extend a booking\n\n## Intent\n\nOne more night.\n\n## Scope\n\nBookings.\n\n## Scenarios\n\n- S1\n"
TASKS = """- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}
- {id: T2, title: Pricing, repo: backend, files: [src/b.py], depends_on: [T1]}
- {id: T3, title: Help page, repo: home, files: [help/booking.md]}
- {id: T4, title: Booking API errors, repo: backend, files: [src/a.py]}
"""


def states(result: dict) -> dict:
    """The steps' units and their states; the pipeline's and blocks' own actions are in `actions`."""
    return {u["unit"]: (u["state"], u.get("reason")) for u in result["units"] if "lifecycle" not in u}


def actions(result: dict) -> dict:
    return {u["unit"]: (u["action"], u["state"], u.get("reason")) for u in result["units"] if "lifecycle" in u}


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
    """Write the files and record the unit, which commits them on its branch."""
    for (repo, rel), text in (files or {}).items():
        path = trees[repo] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    args = ["record", run, handout["unit"], *kw.get("extra", [])]
    if returned is not None:
        path = f.home.parent / f"{handout['unit']}-return.json"
        path.write_text(json.dumps(returned))
        args += ["--return", str(path)]
    if exit is not None:
        args += ["--exit", str(exit)]
    return f.run(*args, expect=kw.get("expect", 0))


def started(f):
    start_story(f)


def do_spec(f, tasks=TASKS, spec=SPEC):
    started(f)
    h = handouts(f.run("next", "412"))["spec"]
    trees = start_work(f, h)
    files = {("home", "specs/412.md"): spec, ("home", "work/412/tasks.yaml"): tasks}
    finish(f, "412", h, trees, files, {"summary": "Wrote the spec."})
    f.merge("home", "fab0/412/spec", "fab0/412/base")
    sync_children(f, tasks)


def sync_children(f, tasks: str) -> None:
    """What `apply` does for the foreach's onStart: a Task sub-issue per task, titled like it, blocked by the
    sub-issues of the tasks it depends on."""

    def change(data):
        children, refs = [], {}
        listed = [t for t in yaml.safe_load(tasks) or [] if isinstance(t, dict) and "title" in t]
        for i, task in enumerate(listed):
            refs[task.get("id")] = ref = f"acme/shop#{500 + i}"
            data["issues"][ref] = {"title": task["title"], "type": "Task"}
            children.append(ref)
        for task in listed:
            after = [refs[d] for d in task.get("depends_on", []) if d in refs]
            if after:
                data["issues"][refs[task["id"]]]["blocked_by"] = after
        data["issues"]["acme/shop#412"]["sub_issues"] = children

    f.gh_edit(change)


def test_a_run_starts_with_its_onstart_actions(story):
    res = story.run("next", "412")
    assert res["kind"] == "units" and res["branch"] == "fab0/412/base"
    assert actions(res)["onStart-1"] == ("git.run-branch", "ready", {"missing": "no fab0/412/base in home, backend"})
    assert actions(res)["onStart-2"] == ("pr.open-run", "blocked", {"order": "onStart-1"})
    assert states(res)["spec"] == ("blocked", {"order": "onStart"})
    h = handouts(res)["onStart-1"]
    assert h["executor"] == "action" and h["lifecycle"] == "onStart" and h["with"] == {}
    story.branch("home", "fab0/412/base", "main")
    story.branch("backend", "fab0/412/base", "main")
    res = story.run("next", "412")
    assert actions(res)["onStart-2"] == ("pr.open-run", "ready", {"missing": "home: no pull request"})
    start_story(story)  # the pull request and the field too; the branches are already there
    res = story.run("status", "412")
    assert actions(res) == {
        "onStart-1": ("git.run-branch", "done", None),
        "onStart-2": ("pr.open-run", "done", None),
        "onStart-3": ("tracker.field", "done", None),
        "onEnd-1": ("git.sync-base", "blocked", {"order": "steps"}),
        "onEnd-2": ("git.remove-run-artifacts", "blocked", {"order": "steps"}),
        "onEnd-3": ("pr.ready", "blocked", {"order": "steps"}),
        "onEnd-4": ("tracker.close", "blocked", {"order": "steps"}),
    }
    assert states(res)["spec"] == ("ready", None)


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
            "exists": True,  # `next` claimed it by creating the branch (D46)
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


def test_a_list_without_usable_ids_blocks_the_foreach(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    brief = handouts(f.run("next", "docs-1"))["brief"]
    pages = json.dumps([{"slug": "intro", "file": "docs/intro.md"}, {"file": "docs/other.md"}])
    files = {("home", "briefs/docs-1.md"): BRIEF, ("home", "work/docs-1/pages.json"): pages}
    finish(f, "docs-1", brief, start_work(f, brief), files, {"summary": "Planned."})
    f.merge("home", "docs/docs-1--brief", "docs/docs-1")
    assert states(f.run("status", "docs-1"))["foreach-pages"] == (
        "blocked",
        {"data": "pages[1] has no usable 'slug' (letters, digits, . _ -)"},
    )


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
    assert states(end)["docs-note"] == ("done", None)
    assert actions(end)["onEnd-2"] == (
        "git.remove-run-artifacts",
        "ready",
        {"missing": "still there: home:work/412/tasks.yaml"},
    )


def test_the_run_ends_with_its_onend_actions_and_a_merge(story):
    do_spec(story, tasks="- {id: T1, title: A, repo: backend, files: [a]}\n")
    t1 = handouts(story.run("next", "412"))["implement-T1"]
    finish(story, "412", t1, start_work(story, t1), {("backend", "a"): "1"}, {"summary": "Done."})
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    res = story.run("next", "412")
    assert "docs-note" not in states(res) and res["kind"] == "units"
    assert actions(res)["onEnd-1"] == ("git.sync-base", "done", None)
    assert actions(res)["onEnd-2"][1:] == ("ready", {"missing": "still there: home:work/412/tasks.yaml"})
    assert [h["unit"] for h in res["handouts"]] == ["onEnd-2"]
    home = story.repo("home")  # what apply does: remove the run's artifacts on the run branch
    git(home, "checkout", "-q", "fab0/412/base")
    git(home, "rm", "-q", "work/412/tasks.yaml")
    git(home, "commit", "-q", "-m", "remove the run's artifacts")
    git(home, "checkout", "-q", "main")
    res = story.run("status", "412")
    assert states(res)["spec"] == ("done", None) and states(res)["implement-T1"] == ("done", None), "D38"
    assert actions(res)["onEnd-3"] == ("pr.ready", "ready", {"missing": "home: a draft; backend: no pull request"})

    def ready(data):
        data["pulls"]["acme/shop"][0]["draft"] = False
        data["pulls"]["acme/shop-api"] = [{"number": 2, "branch": "fab0/412/base", "base": "main"}]
        data["issues"]["acme/shop#412"]["state"] = "closed"

    story.gh_edit(ready)
    res = story.run("status", "412")
    assert res["kind"] == "waiting" and res["reason"] == {"gate": "human", "merge": "fab0/412/base into the base"}
    story.merge("home", "fab0/412/base", "main")
    assert story.run("status", "412")["kind"] == "waiting", "backend's run pull request isn't merged yet"
    story.merge("backend", "fab0/412/base", "main")
    assert story.run("status", "412")["kind"] == "done"


def test_parallel_project_limit(story):
    story.edit("fab0.yaml", lambda d: d["parallel"].update(max=1))
    do_spec(story)
    s = states(story.run("status", "412"))
    assert s["implement-T1"] == ("ready", None)
    assert s["implement-T3"] == ("blocked", {"limit": "parallel.max 1"})


# ---------------------------------------------------------------- docs: manual runs, gated blocks, commands

DOCS_GH = {"user": "docs-user", "issues": {}}
BRIEF = "# B\n\n## Intent\n\n## Findings\n\n## Outline\n"
PAGES = json.dumps([{"slug": "intro", "heading": "Intro", "file": "docs/intro.md"}])


@pytest.fixture
def docs(factory):
    return factory("docs").init_git(DOCS_GH)


def test_a_manual_run_id_must_match_its_pattern(docs):
    assert "doesn't match" in docs.run("status", "release-3", expect=2)["error"]


def test_a_gated_block_has_its_own_branch(docs):
    start_docs(docs)
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
    assert s["parallel-3--spelling"] == ("done", None), "a check that changed nothing has nothing to merge"
    assert s["parallel-3--links"] == ("blocked", {"failed": "exit 1"})
    assert s["parallel-3--index"] == ("ready", None)


def test_a_command_is_recorded_with_its_exit_code(docs):
    start_docs(docs)
    brief = handouts(docs.run("next", "docs-1"))["brief"]
    res = finish(docs, "docs-1", brief, start_work(docs, brief), exit=0, expect=2)
    assert "recorded with --return" in res["error"]


# ---------------------------------------------------------------- triage: no pull requests, no files

TRIAGE_GH = {
    "user": "support-user",
    "issues": {"acme/support#7": {"title": "Checkout fails", "body": "500 on pay.", "labels": ["triaging"]}},
}


def test_a_run_without_files_records_into_the_run_store(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    res = f.run("next", "7")
    assert res["kind"] == "units", "flat branching: no run branch to wait for"
    assert states(res)["classify"] == ("ready", None)
    h = handouts(res)["classify"]
    assert h["branch"] == "fab0/7/classify" and h["claims"] == {}
    assert h["branches"][0]["from"] == "main" and h["branches"][0]["exists"], "next claimed it by creating it"
    assert "500 on pay." in h["brief"]
    assert h["return_schema"]["properties"]["data"]["required"] == ["severity", "area"]
    bad = finish(f, "7", h, start_work(f, h), returned={"summary": "x", "data": {"severity": "dire"}}, expect=4)
    assert "doesn't match its schema" in bad["error"]
    trees = {"home": Path(h["branches"][0]["worktree"])}
    res = finish(f, "7", h, trees, returned={"summary": "Billing.", "data": {"severity": "high", "area": "billing"}})
    assert "heads" not in res["recorded"], "it changed nothing, so there is nothing to merge"
    assert f.fact("7", "classify-1")["data"] == {"severity": "high", "area": "billing"}
    assert (
        git(f.repo("home"), "log", "--format=%s", "main..fab0/7/classify") == "fab0: support-user takes fab0/7/classify"
    )
    s = states(f.run("status", "7"))
    assert s["classify"] == ("waiting", {"gate": "gate"}), "its auto gate decides from step R2d"
    assert s["label"] == ("blocked", {"order": "classify"})


def test_a_home_checkout_off_the_base_branch_is_warned_about(story):
    """From R6: a person checked out the run's pull request in the home checkout, and its older wiring ran."""
    assert "warnings" not in story.run("status", "412")
    story.checkout("home", "elsewhere", "main")
    assert story.run("status", "412")["warnings"] == [
        "the home checkout is on elsewhere, not main; its .fab0/ wiring is the one used"
    ]
