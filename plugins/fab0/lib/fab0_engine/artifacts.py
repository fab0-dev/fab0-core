"""Reading artifacts of every kind, hashing them for staleness, and verifying what a unit wrote.

Each kind has a hasher, so staleness stays generic:
- `file`: the file's bytes, or every file under a directory, at the parent branch's head (or in a checkout);
- `issue`: the part of a tracker item (body, title, labels, comments);
- `pr`: the part of a pull request (body, reviews that ask for changes or comment together with people's
  conversation comments, diff); no pull request hashes as no reviews;
- `tool`: the call itself, its name and rendered arguments. The engine can't call a tool; the agent reads the
  value, and a change of an outside value doesn't make work stale (D29);
- `none`: nothing.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .hashing import sha, sha_json, sha_tree
from .schemas import load_schema, resolve_schema_ref, schema_errors
from .templates import Unresolved, render

REVIEW_STATES = ("CHANGES_REQUESTED", "COMMENTED")
INLINE_LIMIT = 64 * 1024  # text up to this size goes in a brief, so agents need no git to read it


@dataclass
class View:
    """One artifact as a unit sees it: its hash (None when absent), and how to show it in a brief."""

    hash: str | None
    text: str
    where: str = ""


def read_tree(repo, ref: str, path: str) -> dict[str, bytes] | None:
    names = repo._git("ls-tree", "-r", "-z", "--name-only", ref, "--", path.rstrip("/") + "/")
    files = {}
    for name in filter(None, names.stdout.decode().split("\0")):
        files[name[len(path.rstrip("/")) + 1 :]] = repo.read_file(ref, name) or b""
    return files or None


def read_disk(root: Path, path: str) -> bytes | dict[str, bytes] | None:
    target = root / path
    if target.is_file():
        return target.read_bytes()
    if target.is_dir():
        files = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        return files or None
    return None


def content_hash(content) -> str | None:
    if content is None:
        return None
    return sha(content) if isinstance(content, bytes) else sha_tree(content)


def show(content, fmt: str, where: str, how_to_read: str) -> str:
    if isinstance(content, dict):
        names = "\n".join(f"- {n}" for n in sorted(content))
        return f"A directory, {where}, with these files:\n\n{names}\n\n{how_to_read}"
    if len(content) > INLINE_LIMIT:
        return f"Too large to inline: {where}. {how_to_read}"
    return f"{where}:\n\n````{fmt}\n{content.decode(errors='replace')}\n````"


def file_view(run, name: str, ctx, tree: Path | None = None) -> View:
    """A file artifact at the parent branch's head, or in a checkout when `tree` is given."""
    repo, path = run.artifact_location(name, ctx)
    fmt = run.w.artifacts[name]["file"].get("format", "")
    if tree is not None:
        content = read_disk(tree, path)
        where = f"`{path}` in `{tree}`"
        how = f"Read it in `{tree / path}`."
    else:
        ref = repo.fresh(run.parent_in(repo, ctx.parent)) or repo.fresh(repo.base)
        content = (repo.read_file(ref, path) or read_tree(repo, ref, path)) if ref else None
        if (
            content is None
            and ref
            and run.w.artifacts[name].get("keep") == "run"
            and (gone := repo.removed_at(ref, path))
        ):
            content = repo.read_file(gone, path) or read_tree(repo, gone, path)  # as it was before the run's end
        where = f"`{repo.name}:{path}` at `{ref}`"
        how = f"Read it with `git -C {repo.path} show {ref}:{path}`."
    if content is None:
        return View(None, f"Not there yet: `{repo.name}:{path}`.", where)
    return View(content_hash(content), show(content, fmt, where, how), where)


def issue_view(run, name: str, ctx) -> View:
    spec = run.w.artifacts[name]["issue"]
    part = spec["part"]
    if spec.get("of", "run") == "unit":
        ref = (ctx.element or {}).get("item")  # the element's item, once tracker.child.sync made it
        if not ref:
            return View(None, "This unit has no tracker item.")
        repo, number = ref.split("#")[0], int(ref.split("#")[1])
        item, whose = run.tracker.issue(repo, number), ref
    else:
        item = run.item()
        if item is None:
            return View(None, "The run has no tracker item.")
        repo, number, whose = run.home.github, int(run.run), "the run's tracker item"
    if part == "field":
        board = run.w.project.get("tracker", {})
        field = board.get("fields", {}).get(spec["field"], spec["field"])
        value = run.tracker.fields(repo, number, board.get("owner"), board.get("project")).get(field)
        part = f"{field} field"
    elif part == "comments":
        value = [{"user": c["user"], "body": c["body"]} for c in run.tracker.comments(repo, number)]
    else:
        value = item[part]
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    return View(sha_json(value), f"The {part} of {whose}:\n\n````\n{text}\n````")


def pr_view(run, name: str, ctx, unit=None) -> View:
    spec = run.w.artifacts[name]["pr"]
    part = spec["part"]
    if spec.get("of", "run") == "unit":
        if unit is None or not unit.branch:
            return View(sha_json([]) if part == "reviews" else None, "This unit has no pull request.")
        repo = run.repos[unit.ledger_repo]
        head, base = unit.branch, run.parent_in(repo, ctx.parent)
    else:
        repo = run.home
        head, base = run.run_branch, repo.base
    pulls = run.tracker.pulls(repo.github, head, base) if run.tracker and head else []
    pr = pulls[-1] if pulls else None
    if part == "reviews":
        # every pull request of the branch: one merged at an older attempt keeps its reviews (from R6)
        entries = []
        for p in pulls:
            found = [
                (r.get("at"), {"state": r["state"], "user": r["user"], "body": r["body"]})
                for r in run.tracker.reviews(repo.github, p["number"])
                if r["state"] in REVIEW_STATES
            ]
            # people's conversation comments count as feedback; fab0's own posts don't
            found += [
                (c.get("at"), {"state": "CONVERSATION_COMMENT", "user": c["user"], "body": c["body"]})
                for c in run.tracker.comments(repo.github, p["number"])
                if not c["body"].startswith("<!-- fab0:")
            ]
            if all(at for at, _ in found):
                found.sort(key=lambda e: e[0])  # stable: equal times keep their listed order
            entries.append(found)
        value = [e for found in entries for _, e in found]
        if not value:
            return View(sha_json([]), "No reviews ask for changes.")
        lines = "\n\n".join(f"**{r['user']}** ({r['state'].lower().replace('_', ' ')}):\n\n{r['body']}" for r in value)
        numbers = ", ".join(f"{repo.github}#{p['number']}" for p in pulls)
        return View(sha_json(value), f"Reviews on {numbers}, oldest first:\n\n{lines}")
    if pr is None:
        return View(None, "There is no pull request yet.")
    value = pr["body"] if part == "body" else run.tracker.diff(repo.github, pr["number"])
    return View(sha_json(value), f"The {part} of {repo.github}#{pr['number']}:\n\n````\n{value}\n````")


def tool_view(run, name: str, ctx) -> View:
    spec = run.w.artifacts[name]["tool"]
    try:
        args = render(spec.get("with", {}), run.lookup(ctx))
    except Unresolved as exc:
        return View(None, f"Not there yet: {{{exc}}} has no value.")
    call = {"tool": spec["name"], "with": args}
    return View(sha_json(call), f"Call the tool `{spec['name']}` with:\n\n````json\n{json.dumps(args, indent=2)}\n````")


def view(run, name: str, ctx, unit=None) -> View:
    """An input as a unit sees it now."""
    if name == "unit" or name.startswith("unit."):
        return View(sha_json(ctx.element["raw"]), "")
    kind = run.w.artifacts[name]["kind"]
    if kind == "file":
        return file_view(run, name, ctx)
    if kind == "issue":
        return issue_view(run, name, ctx)
    if kind == "pr":
        return pr_view(run, name, ctx, unit)
    if kind == "tool":
        return tool_view(run, name, ctx)
    return View(None, "")


def input_hashes(run, unit) -> dict[str, str]:
    """The hash of every input that exists now; the ledger keeps them, and a change makes the unit stale."""
    hashes = {}
    for name in unit.step.get("inputs", []):
        key = "unit" if name.startswith("unit") else name
        h = view(run, name, unit.ctx, unit).hash
        if h is not None:
            hashes[key] = h
    return hashes


# ------------------------------------------------------------ verification


def tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def verify(run, name: str, ctx, tree: Path) -> list[str]:
    """Rungs 2 and 3 of the ladder for an output a unit wrote: it exists, parses, matches its schema, and
    passes the artifact's own verifiers."""
    art = run.w.artifacts[name]
    _, path = run.artifact_location(name, ctx)
    content = read_disk(tree, path)
    if content is None:
        return [f"{path} is missing"]
    errors = []
    fmt = art["file"].get("format")
    if fmt in ("yaml", "json") and isinstance(content, bytes):
        try:
            data = json.loads(content) if fmt == "json" else yaml.safe_load(content)
        except (ValueError, yaml.YAMLError) as exc:
            return [f"{path}: not valid {fmt}: {str(exc).splitlines()[0]}"]
        if "schema" in art:
            schema = load_schema(resolve_schema_ref(art["schema"], run.w.root))
            errors += [f"{path}: {m}" for m in schema_errors(data, schema)]
    for check in art.get("verify", []):
        if "sections" in check:
            text = content.decode(errors="replace") if isinstance(content, bytes) else ""
            heads = {m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M)}
            missing = [s for s in check["sections"] if s not in heads]
            if missing:
                errors.append(f"{path}: missing sections: {', '.join(missing)}")
        elif "lint" in check:
            command = check["lint"].replace("{path}", shlex.quote(str(tree / path)))
            done = subprocess.run(command, shell=True, cwd=tree, capture_output=True, text=True)
            if done.returncode:
                errors.append(f"{path}: `{check['lint']}` failed: {tail(done.stdout + done.stderr)}")
        elif "script" in check:
            exe = shutil.which(check["script"])
            if exe is None:
                errors.append(f"{path}: the verifier {check['script']} is not on PATH")
                continue
            done = subprocess.run([exe, str(tree / path)], cwd=tree, capture_output=True, text=True)
            if done.returncode:
                errors.append(f"{path}: {check['script']} failed: {tail(done.stdout + done.stderr)}")
    return errors
