"""Auto gates: the hand-out of a gate (an agent's verdict, or a command's exit code) and its record.

A gate judges one attempt of a step, or a gated block's units as they are. Its ledger entry is the fact
`<unit>-<attempt>-gate` in the run store, beside the attempt it judged, so no entry is ever edited. A command
gate reads the attempt it judges at `{attempt}`, the path of that attempt's entry.
"""

from __future__ import annotations

import copy
import json

from .artifacts import tail, view
from .errors import EXIT_REFUSED, EXIT_USAGE, Fail
from .handout import end_line, push, start_line, worktree
from .ledger import check as check_entry
from .ledger import entry_name, now, timing
from .schedule import Run, Unit
from .schemas import core_schema, schema_errors
from .templates import Unresolved, render
from .version import read_version
from .wiring import HARNESS


def gate_model(run: Run, unit: Unit) -> str:
    """The gate's model class, else the step's, else the project's first."""
    classes = run.w.project["models"][HARNESS]
    cls = unit.auto.get("model") or (unit.step or {}).get("model") or next(iter(classes))
    return classes[cls]


def changes(run: Run, unit: Unit) -> list[str]:
    """How the reviewer sees what the unit changed: a diff per repository."""
    lines = []
    if unit.step is None:
        repo = run.home
        parent = run.parent_in(repo, unit.ctx.parent)
        lines.append(f"- `git -C {repo.path} diff {repo.fresh(parent) or parent}...{unit.branch}`")
        return lines
    for name in unit.write:
        repo = run.repos[name]
        parent = run.parent_in(repo, unit.ctx.parent)
        lines.append(f"- {name}: `git -C {repo.path} diff {repo.fresh(parent) or parent}...{unit.branch}`")
    return lines or ["- It changed no files."]


def gate_brief(run: Run, unit: Unit, schema: dict) -> str:
    auto = unit.auto
    what = unit.step["step"] + (f" for {unit.element_id}" if unit.element_id else "") if unit.step else unit.label
    lines = [
        f"# Review: {what}: run {run.run}",
        "",
        f"Pipeline {run.name}, attempt {unit.attempt}.",
        f"Handed out by fab0 {read_version()}.",
        "",
        "You are the gate after this work. Another agent produced it; decide whether it does what its inputs "
        "ask. Look for what is missing or wrong, not for reasons to agree. Change nothing: don't edit, create or "
        "delete files, and don't commit. Use the shell only to read and to run the project's checks.",
        "",
    ]
    if auto.get("instructions"):
        lines += ["## Instructions", "", auto["instructions"], ""]
    if unit.step:
        step = unit.step
        if step.get("instructions"):
            lines += ["## What the step was asked", "", step["instructions"], ""]
        if unit.ctx.element:
            lines += ["## Unit", "", "````json", json.dumps(unit.ctx.element["raw"], indent=2), "````", ""]
        inputs = [n for n in step.get("inputs", []) if n != "unit" and not n.startswith("unit.")]
        if inputs:
            lines += ["## Its inputs", ""]
            for name in inputs:
                lines += [f"### {name}", "", view(run, name, unit.ctx, unit).text, ""]
        entry = unit.latest
        lines += ["## What it says it did", "", entry.get("summary", ""), ""]
        if "data" in entry:
            lines += ["````json", json.dumps(entry["data"], indent=2), "````", ""]
        checks = entry.get("verify", {})
        if checks:
            lines += ["Its outputs passed their checks: " + ", ".join(sorted(checks)) + ".", ""]
    else:
        lines += ["## The block's units", ""]
        for u in run.units:
            if u.step and u.id.startswith(unit.id + "--") and u.latest:
                lines.append(f"- {u.id}, attempt {u.attempt}: {u.latest.get('summary', '')}")
        lines.append("")
    lines += ["## What changed", ""] + changes(run, unit) + [""]
    if unit.step and unit.branch:  # from R8: a reviewer switched branches in a person's checkout to run tests
        trees = [
            f"`{worktree(run, name, unit.branch, unit.id)}` ({name})"
            for name in dict.fromkeys(unit.write or [unit.pr_repo])
        ]
        lines += [
            "Its branch is checked out in " + ", ".join(trees) + ": run the project's checks there. Don't check out "
            "or switch branches in any other checkout.",
            "",
        ]
    if auto.get("skills"):
        lines += ["## Skills", "", "Load these with the Skill tool first: " + ", ".join(auto["skills"]) + ".", ""]
    retries = unit.gate.get("retry", 2)
    lines += [
        "## Decide",
        "",
        "- `approve`: it does what its inputs ask. Minor notes may go in `findings`; they don't block.",
        f"- `reject`: something fails that the producer can fix. It runs again with your findings, up to {retries} "
        "times. Make each finding specific: `check`, a one-sentence `message`, and a `location` (`path:line`).",
        "- `escalate`: a person has to decide, for example when the inputs are ambiguous or contradict each other. "
        "Say what has to be decided in `reasons`.",
        "",
        "## Reply",
        "",
        "Reply with one JSON object and nothing else, valid against this schema:",
        "",
        "````json",
        json.dumps(schema, indent=2),
        "````",
    ]
    return "\n".join(lines) + "\n"


def gate_handout(run: Run, unit: Unit) -> dict:
    auto = unit.auto
    repo_name = unit.pr_repo
    branch = unit.branch
    tree = worktree(run, repo_name, branch, unit.id if unit.step else unit.id + "-gate")
    out = {
        "unit": unit.id,
        "gate": True,
        "attempt": unit.attempt,
        "executor": "agent" if "agent" in auto else "command",
        "repo": repo_name,
        "branch": branch,
        "worktree": tree,
        "merge": unit.gate.get("merge", "human"),
        "fab0_version": read_version(),
    }
    if "agent" in auto:
        schema = copy.deepcopy(core_schema("verdict"))
        out |= {"agent": auto["agent"], "model": gate_model(run, unit), "skills": auto.get("skills", [])}
        out["return_schema"] = schema
        out["brief"] = gate_brief(run, unit, schema)
    else:
        lookup = run.lookup(unit.ctx)
        judged = str(run.store.fact_path(run.run, entry_name(unit.id, unit.attempt)))

        def resolve(name: str):
            return judged if name == "attempt" and unit.step else lookup(name)

        try:
            out["command"] = render(auto["command"], resolve)
            out["attempt_path"] = judged
        except Unresolved as exc:
            raise Fail(EXIT_REFUSED, f"{unit.id}: {{{exc}}} has no value yet") from None
    out["line"] = start_line(
        f"gate of {unit.id}",
        unit.attempt,
        branch,
        agent=out.get("agent"),
        model=out.get("model"),
        command=out.get("command"),
    )
    return out


def save_gate_pending(run: Run, unit: Unit, out: dict) -> None:
    repo = run.repos[unit.pr_repo]
    if unit.step:
        base = unit.latest["base"]
    else:
        parent = run.parent_in(repo, unit.ctx.parent)
        ref = repo.fresh(parent)
        base = {"branch": parent, "sha": repo.sha(ref)} if ref else {}
    pending = {
        **out,
        "step": unit.step["step"] if unit.step else unit.id,
        "pipeline": run.name,
        "actor": run.actor,
        "base": base,
        "into": run.parent_in(repo, unit.ctx.parent),
        "started": now(),  # a gate handed out again by a later `next` starts again
    }
    if unit.element_id:
        pending["element_id"] = unit.element_id
    if unit.gate_inputs:
        pending["inputs"] = {"units": unit.gate_inputs}
    pending.pop("brief", None)
    folder = run.pending_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{unit.id}-gate.json").write_text(json.dumps(pending, indent=2))


def record_gate(run: Run, unit_id: str, verdict: dict | None, exit_code: int | None, output: str | None) -> dict:
    path = run.pending_dir() / f"{unit_id}-gate.json"
    if not path.is_file():
        raise Fail(EXIT_REFUSED, f"no gate of {unit_id} was handed out to this checkout; run next first")
    pending = json.loads(path.read_text())
    if not pending["base"]:
        raise Fail(EXIT_REFUSED, f"{unit_id}: the branch it merges into doesn't exist")
    gate: dict = {"type": "auto", "merge": pending["merge"], "into": pending["into"]}
    entry: dict = {
        "step": pending["step"],
        "attempt": pending["attempt"],
        "executor": pending["executor"],
        "actor": pending["actor"],
        "branch": pending["branch"],
        "base": pending["base"],
    }
    if "element_id" in pending:
        entry["unit"] = pending["element_id"]
    if "model" in pending:
        entry["model"] = pending["model"]
    if "inputs" in pending:
        entry["inputs"] = pending["inputs"]
    if pending["executor"] == "command":
        if exit_code is None:
            raise Fail(EXIT_USAGE, "a command gate is recorded with --exit")
        # the attempt's path is this machine's: the ledger, which everyone shares, keeps `{attempt}`
        command = pending["command"].replace(pending.get("attempt_path") or "\0", "{attempt}")
        gate["command"] = command
        decision = "approve" if exit_code == 0 else "reject"
        verdict = {"decision": decision, "reasons": [f"`{command}` exited {exit_code}"]}
        if exit_code and output:
            verdict["findings"] = [{"check": command, "message": tail(output)}]
    else:
        if verdict is None:
            raise Fail(EXIT_USAGE, "an agent gate is recorded with --return, its verdict")
        errors = schema_errors(verdict, core_schema("verdict"))
        if errors:
            raise Fail(EXIT_REFUSED, f"{unit_id}: the verdict doesn't match its schema", errors=errors)
        gate["agent"] = pending["agent"]
    gate["decision"] = verdict["decision"]
    entry["gate"] = gate
    entry["summary"] = "; ".join(verdict["reasons"]) or verdict["decision"]
    entry["verdict"] = verdict
    name = entry_name(unit_id, pending["attempt"], gate=True)
    times = timing(pending.get("started"), now())
    if "started" in times:
        entry |= {"started": times["started"], "ended": times["ended"]}
    check_entry(entry, name)
    if run.store.append(run.run, name, entry) == "exists":
        raise Fail(EXIT_REFUSED, f"{run.run}/{name} is in the ledger already; the ledger is append-only")
    path.unlink()
    pushed = push(run)
    return {
        "ok": True,
        "unit": unit_id,
        "gate": True,
        "attempt": pending["attempt"],
        "decision": gate["decision"],
        "entry": str(run.store.fact_path(run.run, name)),
        "recorded": entry,
        **times,
        "line": end_line(
            f"gate of {unit_id}",
            pending["attempt"],
            {"approve": "approved", "reject": "rejected", "escalate": "escalated"}[gate["decision"]],
            times,
            exit_code=exit_code if pending["executor"] == "command" else None,
        ),
        **pushed,
    }
