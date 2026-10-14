"""Hand-outs (`next`) and results (`record`): what a session needs to execute a unit, and the ledger entry after.

A hand-out is saved under the home repo's git directory (`fab0/pending/<run>/<unit>.json`) until `record`
consumes it, so a unit is recorded only as it was handed out.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .errors import EXIT_REFUSED, EXIT_USAGE, Fail
from .ledger import dump as dump_entry
from .ledger import entry_name, parse
from .schedule import Run, Unit
from .schemas import core_schema, load_schema, resolve_schema_ref, schema_errors
from .templates import Unresolved, render
from .version import read_version
from .wiring import HARNESS

INLINE_LIMIT = 64 * 1024  # text inputs up to this size go in the brief, so agents need no git to read them


def return_schema(run: Run, step: dict) -> dict:
    schema = copy.deepcopy(core_schema("return"))
    extensions = schema.pop("$defs")["extensions"]
    returns = step.get("return", {})
    if "questions" in returns:
        schema["properties"]["questions"] = extensions["questions"]
        if returns["questions"] == "required":
            schema["required"].append("questions")
    if "schema" in returns:
        schema["properties"]["data"] = load_schema(resolve_schema_ref(returns["schema"], run.w.root))
        schema["required"].append("data")
    return schema


def worktree(run: Run, repo_name: str, branch: str, unit: str) -> str:
    """Where the unit works: the checkout that has its branch, or else a worktree the session adds at this path."""
    repo = run.repos[repo_name]
    existing = repo.worktrees().get(branch)
    return str(existing) if existing else str(repo.common_dir() / "fab0" / "worktrees" / run.run / unit)


def branches(run: Run, unit: Unit) -> list[dict]:
    """The branches the session needs, in each repository the unit works in, oldest first.

    The last one in each repository is the unit's own, with the `worktree` to check it out in.
    """
    if run.ledger_branch:
        repo, b = run.repos["home"], run.ledger_branch
        tree = worktree(run, "home", b, unit.id)
        return [{"repo": "home", "branch": b, "from": repo.base, "exists": bool(repo.fresh(b)), "worktree": tree}]
    out = []
    for name in unit.write or [unit.ledger_repo]:
        repo = run.repos[name]
        chain, branch = [], unit.branch
        parent = unit.ctx.parent
        while True:
            exists = bool(repo.fresh(branch))
            chain.append({"repo": name, "branch": branch, "from": run.parent_in(repo, parent), "exists": exists})
            if parent is None or repo.fresh(parent):
                break
            branch, parent = parent, run.branch_parents.get(parent)
        chain[0]["worktree"] = worktree(run, name, unit.branch, unit.id)
        out.extend(reversed(chain))
    return out


def input_text(run: Run, unit: Unit, name: str) -> list[str]:
    art = run.w.artifacts[name]
    lines = [f"### {name}", ""]
    if art["kind"] == "file":
        repo, path = run.artifact_location(name, unit.ctx)
        ref = repo.fresh(run.parent_in(repo, unit.ctx.parent)) or repo.base
        raw = repo.read_file(ref, path)
        if raw is None:
            lines.append(f"Not there yet: `{repo.name}:{path}`.")
        elif len(raw) <= INLINE_LIMIT:
            fmt = art["file"].get("format", "")
            lines += [f"`{repo.name}:{path}` at `{ref}`:", "", f"````{fmt}", raw.decode(errors="replace"), "````"]
        else:
            lines.append(f"Too large to inline. Read it with `git -C {repo.path} show {ref}:{path}`.")
    elif art["kind"] == "issue":
        item = run.item()
        part = art["issue"]["part"]
        if item is None or part not in item:
            lines.append(f"The run's tracker item has no {part} to show.")
        else:
            value = item[part]
            lines += [
                "From the run's tracker item:",
                "",
                "````",
                value if isinstance(value, str) else json.dumps(value),
                "````",
            ]
    else:
        lines.append(f"A {art['kind']} artifact; reading it comes in step R2c.")
    return lines + [""]


def brief(run: Run, unit: Unit, schema: dict, trees: dict[str, str]) -> str:
    step = unit.step
    title = f"# {step['step']}" + (f" for {unit.element_id}" if unit.element_id else "") + f": run {run.run}"
    lines = [title, "", f"Pipeline {run.name}, attempt {unit.attempt}.", f"Handed out by fab0 {read_version()}.", ""]
    if "instructions" in step:
        lines += ["## Instructions", "", step["instructions"], ""]
    if unit.ctx.element:
        lines += ["## Unit", "", "````json", json.dumps(unit.ctx.element["raw"], indent=2), "````", ""]
    inputs = [n for n in step.get("inputs", []) if n != "unit" and not n.startswith("unit.")]
    if inputs:
        lines += ["## Inputs", ""]
        for name in inputs:
            lines += input_text(run, unit, name)
    if step.get("outputs"):
        lines += ["## Outputs", ""]
        for name in step["outputs"]:
            art = run.w.artifacts[name]
            if art["kind"] == "file":
                repo, path = run.artifact_location(name, unit.ctx)
                lines.append(f"- {name}: write `{path}` in `{trees.get(repo.name, repo.path)}`")
            else:
                lines.append(f"- {name}")
        lines.append("")
    lines += ["## Where you work", ""]
    if unit.write:
        for repo in unit.write:
            globs = ", ".join(f"`{g}`" for g in unit.claims.get(repo, []))
            lines.append(f"- You may change `{trees.get(repo, run.repos[repo].path)}`, only {globs}.")
    else:
        lines.append("- You change no files.")
    reads = step.get("repos", {}).get("read", [])
    if reads:
        names = list(run.repos) if "all" in reads else reads
        lines.append("- You may read " + ", ".join(f"`{run.repos[n].path}`" for n in names if n in run.repos) + ".")
    lines += ["- Don't commit, push, create or switch branches, or run gh; that happens after you reply.", ""]
    if step.get("skills"):
        lines += ["## Skills", "", "Load these with the Skill tool first: " + ", ".join(step["skills"]) + ".", ""]
    lines += [
        "## Reply",
        "",
        "Reply with one JSON object and nothing else, valid against this schema:",
        "",
        "````json",
        json.dumps(schema, indent=2),
        "````",
    ]
    return "\n".join(lines) + "\n"


def handout(run: Run, unit: Unit) -> dict:
    step = unit.step
    executor = next(k for k in ("agent", "action", "command", "tool") if k in step)
    out = {
        "unit": unit.id,
        "step": step["step"],
        "attempt": unit.attempt,
        "executor": executor,
        "repo": unit.ledger_repo,
        "branch": run.ledger_branch or unit.branch,
        "branches": branches(run, unit),
        "write": unit.write,
        "claims": unit.claims,
        "gate": unit.gate,
        "fab0_version": read_version(),
    }
    if unit.element_id:
        out["element_id"] = unit.element_id
        out["element"] = unit.ctx.element["raw"]
    lookup = run.lookup(unit.ctx)
    try:
        if executor == "agent":
            schema = return_schema(run, step)
            out["agent"] = step["agent"]
            out["model"] = run.w.project["models"][HARNESS][step["model"]]
            out["skills"] = step.get("skills", [])
            out["return_schema"] = schema
            trees = {b["repo"]: b["worktree"] for b in out["branches"] if "worktree" in b}
            out["brief"] = brief(run, unit, schema, trees)
        elif executor == "command":
            out["command"] = render(step["command"], lookup)
        else:
            out[executor] = step[executor]
            out["with"] = render(step.get("with", {}), lookup)
            if executor == "tool":
                out["return_schema"] = return_schema(run, step)
    except Unresolved as exc:
        raise Fail(EXIT_REFUSED, f"{unit.id}: {{{exc}}} has no value yet") from None
    return out


def save_pending(run: Run, unit: Unit, out: dict) -> None:
    folder = run.pending_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # the unit's inputs are read at its parent's head; a parent that doesn't exist yet is cut from the oldest
    # branch of the chain that does, so that is the commit the unit starts from
    chain = [b for b in out["branches"] if b["repo"] == unit.ledger_repo]
    repo = run.repos[unit.ledger_repo]
    ref = repo.fresh(chain[-1]["from"]) or repo.fresh(chain[0]["from"])
    bases = {"branch": chain[-1]["from"], "sha": repo.sha(ref)} if ref else {}
    pending = {**out, "pipeline": run.name, "actor": run.actor, "base": bases, "ledger_dir": run.ledger_dir}
    pending.pop("brief", None)
    (folder / f"{unit.id}.json").write_text(json.dumps(pending, indent=2))


def record(run: Run, unit_id: str, returned: dict | None, exit_code: int | None) -> dict:
    path = run.pending_dir() / f"{unit_id}.json"
    if not path.is_file():
        raise Fail(EXIT_REFUSED, f"{unit_id} wasn't handed out to this checkout; run next first")
    pending = json.loads(path.read_text())
    executor = pending["executor"]
    entry: dict = {
        "step": pending["step"],
        "attempt": pending["attempt"],
        "executor": executor,
        "actor": pending["actor"],
        "branch": pending["branch"],
    }
    if "element_id" in pending:
        entry["unit"] = pending["element_id"]
    if "model" in pending:
        entry["model"] = pending["model"]
    if not pending["base"]:
        raise Fail(
            EXIT_REFUSED, f"{unit_id}: the branch it starts from, {pending['branches'][0]['from']}, doesn't exist"
        )
    entry["base"] = pending["base"]
    if pending["claims"]:
        entry["claims"] = [f"{repo}:{g}" for repo, globs in pending["claims"].items() for g in globs]
    gate = pending["gate"]
    entry["gate"] = {"type": "none" if gate == "none" else "human" if gate == "human" or "human" in gate else "auto"}
    if executor == "command":
        if exit_code is None:
            raise Fail(EXIT_USAGE, "a command step is recorded with --exit")
        entry["exit"] = exit_code
        entry["summary"] = f"exit {exit_code}"
    else:
        if returned is None:
            raise Fail(
                EXIT_USAGE, f"a{'n' if executor[0] in 'aeiou' else ''} {executor} step is recorded with --return"
            )
        schema = pending.get("return_schema") or core_schema("return")
        schema = {k: v for k, v in schema.items() if k != "$defs"}
        errors = schema_errors(returned, schema)
        if errors:
            raise Fail(EXIT_REFUSED, f"{unit_id}: the return doesn't match its schema", errors=errors)
        entry["summary"] = returned["summary"]
        if returned.get("questions"):
            entry["questions"] = returned["questions"]
        if "data" in returned:
            entry["data"] = returned["data"]
    repo = run.repos[pending["repo"]]
    tree = repo.worktrees().get(pending["branch"])
    if tree is None:
        raise Fail(EXIT_REFUSED, f"check out {pending['branch']} in {repo.path} before recording {unit_id}")
    name = entry_name(unit_id, pending["attempt"])
    target = Path(tree) / pending["ledger_dir"] / name
    if target.exists():
        raise Fail(EXIT_REFUSED, f"{target} exists; the ledger is append-only")
    parse(dump_entry(entry), name)  # the entry itself must be valid
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_entry(entry))
    path.unlink()
    return {"ok": True, "unit": unit_id, "attempt": pending["attempt"], "entry": str(target), "recorded": entry}
