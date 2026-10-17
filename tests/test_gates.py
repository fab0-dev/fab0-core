"""Auto gates: hand-outs, verdicts and command exits, retry, escalation, merge, and gated blocks."""

import json
from pathlib import Path

from conftest import git, start_docs
from test_run import BRIEF, DOCS_GH, actions, do_spec, finish, handouts, start_work, states

ONE_TASK = "- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n"


def verdict(decision: str, **extra) -> dict:
    return {"decision": decision, "reasons": [f"the gate says {decision}"], **extra}


def record_gate(f, run: str, unit: str, verdict_: dict | None = None, exit=None, output=None, expect=0) -> dict:
    """What a session does with a gate hand-out: record the verdict (or exit code), which goes to the run store."""
    h = handouts(f.run("next", run, "--unit", unit))[unit]
    assert h.get("gate"), h
    args = ["record", run, unit, "--gate"]
    tmp = f.home.parent
    if verdict_ is not None:
        (tmp / "verdict.json").write_text(json.dumps(verdict_))
        args += ["--return", str(tmp / "verdict.json")]
    if exit is not None:
        args += ["--exit", str(exit)]
    if output is not None:
        (tmp / "output.txt").write_text(output)
        args += ["--output", str(tmp / "output.txt")]
    return f.run(*args, expect=expect)


def implement(f, attempt: int = 1, text: str = "x = 1\n") -> dict:
    h = handouts(f.run("next", "412", "--unit", "implement-T1"))["implement-T1"]
    assert h["attempt"] == attempt
    trees = start_work(f, h) if attempt == 1 else {"backend": Path(h["branches"][-1]["worktree"])}
    finish(f, "412", h, trees, {("backend", "src/a.py"): text}, {"summary": f"Attempt {attempt}."})
    return h


def test_a_recorded_attempt_hands_out_its_gate(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    res = story.run("next", "412")
    assert states(res)["implement-T1"] == ("waiting", {"gate": "gate"})
    g = handouts(res)["implement-T1"]
    assert g["gate"] and g["executor"] == "agent" and g["agent"] == "reviewer" and g["model"] == "opus"
    assert g["branch"] == "fab0/412/implement-T1" and g["merge"] == "auto"
    assert g["return_schema"]["properties"]["decision"]["enum"] == ["approve", "reject", "escalate"]
    assert "# Review: implement for T1: run 412" in g["brief"] and "Attempt 1." in g["brief"]
    assert "diff fab0/412/base...fab0/412/implement-T1" in g["brief"] and "up to 2 times" in g["brief"]
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"gate": "running"})


def test_approve_then_the_merge_finishes_the_unit(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    res = record_gate(story, "412", "implement-T1", verdict("approve"))
    assert res["decision"] == "approve"
    entry = res["recorded"]
    assert entry["gate"] == {
        "type": "auto",
        "merge": "auto",
        "into": "fab0/412/base",
        "agent": "reviewer",
        "decision": "approve",
    }
    assert entry["unit"] == "T1" and entry["attempt"] == 1 and entry["verdict"]["decision"] == "approve"
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"gate": "merge"})
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    assert states(story.run("status", "412"))["implement-T1"] == ("done", None)


def test_the_ledger_stays_out_of_the_product_repositories(story):
    """MIGRATION M2: every attempt and gate decision is in the run store, and the product branches carry only the
    work, whatever merged them."""
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    story.run("apply", "412")  # merges the approved unit into the run branch
    assert states(story.run("status", "412"))["implement-T1"] == ("done", None)
    assert {"spec-1", "implement-T1-1", "implement-T1-1-gate"} <= set(story.facts("412"))
    for repo in ("home", "backend"):
        files = git(story.repo(repo), "log", "--all", "--name-only", "--format=").split()
        assert not [f for f in files if "ledger" in f], f"{repo} has no ledger file on any branch"
    subjects = git(story.repo("backend"), "log", "--format=%s", "fab0/412/base").splitlines()
    assert not [s for s in subjects if " gate " in s], "a gate decision is no commit"


def test_reject_reruns_with_the_findings_then_escalates(story):
    do_spec(story, tasks=ONE_TASK)
    finding = {"check": "scenarios", "message": "A full hotel isn't handled.", "location": "src/a.py:1"}
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("reject", findings=[finding]))
    nxt = story.run("next", "412")
    assert states(nxt)["implement-T1"] == ("ready", {"rework": ["rejected"]})
    brief = handouts(nxt)["implement-T1"]["brief"]
    assert "- The gate rejected it:" in brief
    assert "  - scenarios: A full hotel isn't handled. (src/a.py:1)" in brief and "Attempt 1." in brief
    for attempt in (2, 3):
        implement(story, attempt, f"x = {attempt}\n")
        record_gate(story, "412", "implement-T1", verdict("reject", findings=[finding]))
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"escalated": "rejected 3 times"})
    story.merge("backend", "fab0/412/implement-T1", "fab0/412/base")
    assert states(story.run("status", "412"))["implement-T1"] == ("done", None), "a person merged it anyway"


def test_escalate_waits_for_a_person(story):
    do_spec(story, tasks=ONE_TASK)
    implement(story)
    record_gate(story, "412", "implement-T1", verdict("escalate"))
    assert states(story.run("status", "412"))["implement-T1"] == ("waiting", {"escalated": "the gate escalated"})


def test_a_verdict_is_checked_and_must_be_handed_out(story):
    do_spec(story, tasks=ONE_TASK)
    h = handouts(story.run("next", "412"))["implement-T1"]
    finish(story, "412", h, start_work(story, h), {("backend", "src/a.py"): "1"}, {"summary": "Done."})
    bad = story.run("record", "412", "implement-T1", "--gate", "--exit", "0", expect=4)
    assert bad["error"] == "no gate of implement-T1 was handed out to this checkout; run next first"
    res = record_gate(story, "412", "implement-T1", {"decision": "maybe", "reasons": []}, expect=4)
    assert res["error"] == "implement-T1: the verdict doesn't match its schema"


def test_a_human_merge_after_approval(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    brief = handouts(f.run("next", "docs-1"))["brief"]
    pages = json.dumps([{"slug": "intro", "file": "docs/intro.md"}])
    files = {("home", "briefs/docs-1.md"): BRIEF, ("home", "work/docs-1/pages.json"): pages}
    finish(f, "docs-1", brief, start_work(f, brief), files, {"summary": "Planned."})
    f.merge("home", "docs/docs-1--brief", "docs/docs-1")
    write = handouts(f.run("next", "docs-1"))["write-intro"]
    finish(f, "docs-1", write, start_work(f, write), {("home", "docs/intro.md"): "# Intro\n"}, {"summary": "W"})
    g = handouts(f.run("next", "docs-1", "--unit", "write-intro"))["write-intro"]
    assert g["agent"] == "docs-reviewer" and g["skills"] == ["docs-style"] and g["merge"] == "human"
    record_gate(f, "docs-1", "write-intro", verdict("approve"))
    assert states(f.run("status", "docs-1"))["write-intro"] == ("waiting", {"gate": "human"})


def test_a_gated_block_is_judged_as_a_whole(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    brief = handouts(f.run("next", "docs-1"))["brief"]
    files = {("home", "briefs/docs-1.md"): BRIEF, ("home", "work/docs-1/pages.json"): "[]"}
    finish(f, "docs-1", brief, start_work(f, brief), files, {"summary": "Nothing to write."})
    f.merge("home", "docs/docs-1--brief", "docs/docs-1")
    res = f.run("next", "docs-1")
    for unit in ("parallel-3--spelling", "parallel-3--links"):
        h = handouts(res)[unit]
        finish(f, "docs-1", h, start_work(f, h), exit=0)
        f.merge("home", h["branch"], "docs/docs-1--parallel-3")
    index = handouts(f.run("next", "docs-1"))["parallel-3--index"]
    finish(f, "docs-1", index, start_work(f, index), {("home", "docs/index.md"): "# Docs\n"}, {"summary": "Index."})
    f.merge("home", index["branch"], "docs/docs-1--parallel-3")
    res = f.run("next", "docs-1")
    assert states(res)["parallel-3"] == ("waiting", {"gate": "gate"})
    g = handouts(res)["parallel-3"]
    assert g["executor"] == "command" and g["command"] == "markdownlint docs"
    assert g["branch"] == "docs/docs-1--parallel-3" and g["worktree"].endswith("parallel-3-gate")
    rejected = record_gate(f, "docs-1", "parallel-3", exit=1, output="docs/index.md:1 MD041 first line\n")
    assert rejected["recorded"]["verdict"]["findings"][0]["message"] == "docs/index.md:1 MD041 first line"
    assert states(f.run("status", "docs-1"))["parallel-3"] == ("waiting", {"escalated": "reject"})


def test_an_approved_block_merges_and_the_run_ends(factory):
    f = factory("docs").init_git(DOCS_GH)
    start_docs(f)
    brief = handouts(f.run("next", "docs-1"))["brief"]
    files = {("home", "briefs/docs-1.md"): BRIEF, ("home", "work/docs-1/pages.json"): "[]"}
    finish(f, "docs-1", brief, start_work(f, brief), files, {"summary": "Nothing to write."})
    f.merge("home", "docs/docs-1--brief", "docs/docs-1")
    for unit in ("parallel-3--spelling", "parallel-3--links", "parallel-3--index"):
        h = handouts(f.run("next", "docs-1", "--unit", unit))[unit]
        files = {("home", "docs/index.md"): "# Docs\n"} if unit.endswith("index") else None
        finish(
            f,
            "docs-1",
            h,
            start_work(f, h),
            files,
            *([{"summary": "Index."}] if files else []),
            **({} if files else {"exit": 0}),
        )
        f.merge("home", h["branch"], "docs/docs-1--parallel-3")
    record_gate(f, "docs-1", "parallel-3", exit=0)
    assert states(f.run("status", "docs-1"))["parallel-3"] == ("waiting", {"gate": "merge"})
    f.merge("home", "docs/docs-1--parallel-3", "docs/docs-1")
    end = f.run("status", "docs-1")
    assert states(end)["parallel-3"] == ("done", None) and actions(end)["onEnd-1"] == ("pr.ready", "done", None)
    assert end["kind"] == "waiting" and end["reason"] == {"gate": "human", "merge": "docs/docs-1 into the base"}
    f.merge("home", "docs/docs-1", "main")
    assert f.run("status", "docs-1")["kind"] == "done"


TRIAGE_GH = {
    "user": "support-user",
    "issues": {"acme/support#7": {"title": "Checkout fails", "body": "500 on pay.", "labels": ["triaging"]}},
}


def classify(f, data: dict) -> None:
    h = handouts(f.run("next", "7", "--unit", "classify"))["classify"]
    trees = {"home": Path(h["branches"][0]["worktree"])}
    if not trees["home"].exists():
        trees = start_work(f, h)
    finish(f, "7", h, trees, returned={"summary": "Classified.", "data": data})


def test_a_command_gate_decides_alone_when_nothing_is_to_merge(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    classify(f, {"severity": "high", "area": "billing"})
    record_gate(f, "7", "classify", exit=0)
    res = f.run("next", "7")
    assert states(res)["classify"] == ("done", None)
    label = handouts(res)["label"]
    assert label["executor"] == "action" and label["action"] == "tracker.label"
    assert label["with"] == {"add": ["severity-high", "area-billing"]}
    assert label["missing"] == "add severity-high, area-billing"
    refused = f.run("record", "7", "label", "--exit", "0", expect=4)
    assert "is an action" in refused["error"]
    f.gh_edit(lambda d: d["issues"]["acme/support#7"]["labels"].extend(["severity-high", "area-billing"]))
    res = f.run("next", "7")
    assert states(res)["label"] == ("done", None)
    page = handouts(res)["page"]
    assert page["executor"] == "tool" and page["with"]["text"] == "High-severity report 7: Checkout fails"
    finish(f, "7", page, start_work(f, page), returned={"summary": "Posted."})
    assert states(f.run("status", "7"))["page"] == ("waiting", {"gate": "human"})
    f.gh_edit(lambda d: d["issues"]["acme/support#7"]["labels"].append("acknowledged"))
    assert states(f.run("status", "7"))["page"] == ("done", None), "the label is the person's decision"


def test_then_fail_stops_the_run_after_the_retries(factory):
    f = factory("triage").init_git(TRIAGE_GH)
    for attempt in (1, 2):
        classify(f, {"severity": "low", "area": "billing"})
        record_gate(f, "7", "classify", exit=1, output=f"attempt {attempt}: not enough detail\n")
    assert states(f.run("status", "7"))["classify"] == ("blocked", {"failed": "rejected 2 times"})


def test_a_gate_brief_names_where_the_work_is_checked_out(story):
    """From R8: so a reviewer runs checks in the unit's worktree, not by switching a person's checkout."""
    do_spec(story, tasks="- {id: T1, title: Booking API, repo: backend, files: [src/a.py]}\n")
    h = handouts(story.run("next", "412"))["implement-T1"]
    trees = start_work(story, h)
    finish(story, "412", h, trees, {("backend", "src/a.py"): "x = 1\n"}, {"summary": "Done."})
    brief = handouts(story.run("next", "412", "--unit", "implement-T1"))["implement-T1"]["brief"]
    assert f"Its branch is checked out in `{trees['backend']}` (backend)" in brief
