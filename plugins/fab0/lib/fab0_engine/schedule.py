"""A run of a pipeline, expanded into units, each with a state.

A unit is one step, or one step for one element of a `foreach`. The engine walks the pipeline's tree and
gives every unit a state:

- `ready`: it can start now; `next` hands it out.
- `open`: a gated block whose units are still under way.
- `running`: it was handed out and nothing is recorded yet; `next` hands it out again.
- `waiting`: recorded, and waiting on its gate: `merge` (gate none: `apply` merges it), `human`, `gate` (an
  auto gate decides), or `questions` (a person answers).
- `blocked`: it can't start, and `reason` says why: `order` (what comes first isn't done), `data` (an input
  is missing), `claim` (an earlier unit claims overlapping paths), `limit` (too many units at once),
  `invalid-dependency`, `condition` (a `when` can't be decided yet), `failed` (a command exited non-zero),
  or `start` (the run hasn't started).
- `done`: its ledger entry is on the branch it merges into.

Branches (D10, D14): in `run` branching every unit works on its own branch and merges into its parent: the
run branch, or the branch of the gated block around it. In `flat` branching the parent is the base branch.
A pipeline whose steps write no files keeps its ledger on `ledger.branch` (D18), and its units work there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .artifacts import input_hashes
from .claims import EVERYTHING, overlap
from .git import Repo
from .hashing import sha_json
from .ledger import DEFAULT_PATH, attempts, entry_name, gate_attempts, parse
from .templates import Unresolved
from .wiring import EXECUTORS, Wiring

ELEMENT_KEYS = ("id", "title", "repo", "after", "paths")
ELEMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
UNKNOWN = None  # a condition that can't be decided yet


@dataclass
class Ctx:
    parent: str | None  # the branch units here merge into; None is each repository's base branch
    prefix: str = ""  # added to unit names inside a gated block
    element: dict | None = None  # the foreach element, mapped: id, title, repo, after, paths, raw
    max: int | None = None  # the foreach block's own limit


@dataclass
class Unit:
    id: str
    step: dict | None
    label: str
    ctx: Ctx
    gate: object = "none"
    state: str = "blocked"
    reason: dict | None = None
    branch: str | None = None
    ledger_repo: str = "home"
    write: list[str] = field(default_factory=list)
    claims: dict = field(default_factory=dict)
    latest: dict | None = None
    attempt: int = 1
    rework: dict | None = None  # why the next attempt is needed: stale inputs, missing outputs, failed checks
    history: list = field(default_factory=list)  # every recorded attempt, oldest first
    gates: dict = field(default_factory=dict)  # attempt -> the auto gate's ledger entry
    gate_inputs: str | None = None  # a gated block: the hash of its units' attempts its gate must judge

    @property
    def auto(self) -> dict | None:
        return self.gate["auto"] if isinstance(self.gate, dict) and "auto" in self.gate else None

    @property
    def element_id(self) -> str | None:
        return self.ctx.element["id"] if self.ctx.element else None

    def summary(self) -> dict:
        out = {"unit": self.id, "label": self.label, "state": self.state}
        if self.step:
            out["step"] = self.step["step"]
        if self.element_id:
            out["element"] = self.element_id
        if self.reason:
            out["reason"] = self.reason
        if self.branch:
            out["branch"] = self.branch
        if self.step:
            out["attempt"] = self.attempt
        return out


class Run:
    def __init__(self, wiring: Wiring, pipeline: str, run: str, actor: str, tracker=None):
        self.w = wiring
        self.name = pipeline
        self.p = wiring.pipelines[pipeline]
        self.run = run
        self.actor = actor
        self.tracker = tracker
        self.policy = wiring.git_policy(self.p)
        project = wiring.project
        self.repos = {n: Repo(n, cfg, wiring.root, self.policy["base"]) for n, cfg in project["repos"].items()}
        self.home = self.repos["home"]
        branches = self.policy.get("branches", {})
        self.unit_template = branches.get("unit", "fab0/{run}/{unit}")
        self.run_branch = (
            branches.get("run", "fab0/{run}/base").replace("{run}", run) if self.policy["branching"] == "run" else None
        )
        ledger = project.get("ledger", {})
        self.ledger_dir = ledger.get("path", DEFAULT_PATH).replace("{run}", run)
        writes = any(wiring.writes_files(s) for s in self._steps(self.p["steps"]))
        self.ledger_branch = None if writes else ledger.get("branch")
        self.branch_parents: dict[str, str | None] = {self.run_branch: None} if self.run_branch else {}
        self.units: list[Unit] = []
        self._item: dict | None = None
        self._entries: dict[tuple, dict] = {}

    # ------------------------------------------------------------ helpers

    def _steps(self, nodes: list):
        for node in nodes:
            if isinstance(node, str):
                yield self.w.steps[node]
            elif "step" in node:
                yield self.step_of(node)
            else:
                for key in ("steps", "else"):
                    yield from self._steps(node.get(key, []))
                for branch in node.get("parallel", {}).values():
                    yield from self._steps(branch)

    def step_of(self, node) -> dict:
        if isinstance(node, str):
            return self.w.steps[node]
        if any(k in node for k in EXECUTORS):
            return {k: v for k, v in node.items() if k not in ("gate", "after")}
        return self.w.steps[node["step"]]

    def branch_name(self, unit: str) -> str:
        return self.unit_template.replace("{run}", self.run).replace("{unit}", unit)

    def parent_in(self, repo: Repo, parent: str | None) -> str:
        return parent or repo.base

    def item(self) -> dict | None:
        """The run's tracker item, for `{run.<field>}` and tracker conditions."""
        if self._item is None and self.tracker is not None and "issue" in self.p["trigger"]:
            if self.run.isdigit():
                self._item = self.tracker.issue(self.home.github, int(self.run))
        return self._item

    def started(self) -> bool:
        return self.run_branch is None or self.home.fresh(self.run_branch) is not None

    def pending_dir(self) -> Path:
        return Path(self.home.out("rev-parse", "--absolute-git-dir")) / "fab0" / "pending" / self.run

    # ------------------------------------------------------------ the walk

    def evaluate(self) -> dict:
        out = {"ok": True, "run": self.run, "pipeline": self.name}
        if not self.started():
            return {**out, "kind": "start", "actions": self.p["onStart"], "branch": self.run_branch, "units": []}
        done = self.walk(self.p["steps"], Ctx(parent=self.run_branch), True, None)
        self.apply_claims()
        self.apply_limits()
        out["units"] = [u.summary() for u in self.units]
        if done:
            out["kind"] = "end"
            out["actions"] = self.p["onEnd"]
            if self.run_branch:
                out["branch"] = self.run_branch
        else:
            out["kind"] = "units"
        return out

    def walk(self, nodes: list, ctx: Ctx, reachable: bool, reason: dict | None) -> bool:
        """Gives a state to every unit under `nodes`, in order; True when all of them are done."""
        all_done, first_open = True, None
        for i, node in enumerate(nodes):
            here_reachable = reachable and all_done
            here_reason = reason if not reachable else (None if all_done else {"order": first_open})
            done = self.walk_node(node, ctx, here_reachable, here_reason, f"{i + 1}")
            if not done and all_done:
                first_open = self.label(node)
            all_done = all_done and done
        return all_done

    def label(self, node) -> str:
        if isinstance(node, str):
            return node
        for key in ("step", "foreach", "call"):
            if key in node:
                return f"{key} {node[key]}" if key != "step" else node["step"]
        if "parallel" in node:
            return node.get("id") or "parallel " + ", ".join(node["parallel"])
        return node.get("id") or "when"

    def walk_node(self, node, ctx: Ctx, reachable: bool, reason: dict | None, pos: str) -> bool:
        if isinstance(node, str) or "step" in node:
            return self.walk_step(node, ctx, reachable, reason)
        gate = node.get("gate", "none")
        if gate == "none":
            return self.walk_block(node, ctx, reachable, reason, pos)
        kind = next(k for k in ("foreach", "parallel", "when") if k in node)
        block_id = ctx.prefix + (node.get("id") or f"{kind}-{pos}")
        inner = Ctx(parent=self.branch_name(block_id), prefix=block_id + "--", element=ctx.element)
        self.branch_parents[inner.parent] = ctx.parent
        block = Unit(id=block_id, step=None, label=self.label(node), ctx=ctx, gate=gate, branch=inner.parent)
        self.units.append(block)
        if not reachable:
            block.reason = reason
            self.walk_block(node, inner, False, {"order": block.label} if reason is None else reason, pos)
            return False
        start = len(self.units)
        children_done = self.walk_block(node, inner, True, None, pos)
        children = [u for u in self.units[start:] if u.step]
        if not children_done:
            block.state, block.reason = "open", None
            return False
        on_parent = all(self.on_branch(u, ctx.parent) for u in children)
        if on_parent:
            block.state, block.reason = "done", None
            return True
        block.state = "waiting"
        if block.auto is None:
            block.reason = {"gate": self.gate_kind(gate)}
            return False
        # a block's gate judges its units as they are now; a unit that moves on asks for a new decision
        block.gate_inputs = sha_json({u.id: u.attempt for u in children})
        self.load_latest(block)
        latest = block.gates.get(max(block.gates)) if block.gates else None
        if latest is None or latest.get("inputs", {}).get("units") != block.gate_inputs:
            block.attempt = max(block.gates, default=0) + 1
            block.reason = {"gate": self.gate_running(block)}
        elif latest["gate"]["decision"] == "approve":
            block.attempt = max(block.gates)
            block.reason = {"gate": "merge" if gate.get("merge") == "auto" else "human"}
        else:  # a block isn't retried as a whole: which unit to redo is a person's call (D35)
            block.attempt = max(block.gates)
            block.reason = {"escalated": latest["gate"]["decision"]}
        return False

    def walk_block(self, node, ctx: Ctx, reachable: bool, reason: dict | None, pos: str) -> bool:
        if "foreach" in node:
            return self.walk_foreach(node, ctx, reachable, reason)
        if "parallel" in node:
            return self.walk_parallel(node, ctx, reachable, reason)
        if "when" in node:
            return self.walk_when(node, ctx, reachable, reason)
        self.placeholder(node, ctx, {"unsupported": "call"})
        return False

    def placeholder(self, node, ctx: Ctx, reason: dict | None) -> None:
        self.units.append(
            Unit(
                id=ctx.prefix + self.label(node).replace(" ", "-"),
                step=None,
                label=self.label(node),
                ctx=ctx,
                reason=reason,
            )
        )

    # ------------------------------------------------------------ foreach

    def walk_foreach(self, node: dict, ctx: Ctx, reachable: bool, reason: dict | None) -> bool:
        if not reachable:
            self.placeholder(node, ctx, reason)
            return False
        elements, problem = self.elements(node["foreach"], ctx)
        if problem:
            self.placeholder(node, ctx, problem)
            return False
        ids = [e["id"] for e in elements]
        done: dict[str, bool] = {}
        invalid = self.invalid_after(elements)
        start = len(self.units)
        for element in self.in_dependency_order(elements, invalid):
            inner = Ctx(parent=ctx.parent, prefix=ctx.prefix, element=element, max=node.get("max"))
            if element["id"] in invalid:
                self.walk(node["steps"], inner, False, {"invalid-dependency": invalid[element["id"]]})
                done[element["id"]] = False
                continue
            waiting = [d for d in element["after"] if not done.get(d)]
            here_reason = {"order": waiting} if waiting else None
            done[element["id"]] = self.walk(node["steps"], inner, not waiting, here_reason)
        # walked dependencies first, so `done` is known; listed in the list's order, which decides claims
        position = {i: n for n, i in enumerate(ids)}
        self.units[start:] = sorted(self.units[start:], key=lambda u: position[u.element_id])
        return all(done.get(i) for i in ids)

    def elements(self, ref: str, ctx: Ctx) -> tuple[list, dict | None]:
        name, *at = ref.split(".")
        art = self.w.artifacts[name]
        mapping = {k: k for k in ELEMENT_KEYS} | {k: v for k, v in art.get("elements", {}).items() if k != "at"}
        at = at or ([art["elements"]["at"]] if "at" in art.get("elements", {}) else [])
        data, problem = self.read_artifact(name, ctx)
        if problem:
            return [], problem
        for key in ".".join(at).split(".") if at else []:
            data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, list):
            return [], {"data": f"{name} is not a list"}
        out = []
        for i, raw in enumerate(data):
            if not isinstance(raw, dict) or not ELEMENT_ID.match(str(raw.get(mapping["id"], ""))):
                return [], {"data": f"{name}[{i}] has no usable '{mapping['id']}' (letters, digits, . _ -)"}
            element = {k: raw.get(mapping[k]) for k in ELEMENT_KEYS}
            element["id"] = str(element["id"])
            element["after"] = [str(a) for a in element["after"] or []]
            paths = element["paths"]
            element["paths"] = [paths] if isinstance(paths, str) else paths if isinstance(paths, list) else None
            element["raw"] = raw
            out.append(element)
        if len({e["id"] for e in out}) != len(out):
            return [], {"data": f"{name} has duplicate ids"}
        return out, None

    @staticmethod
    def invalid_after(elements: list) -> dict[str, str]:
        ids = {e["id"] for e in elements}
        invalid = {}
        for e in elements:
            unknown = [a for a in e["after"] if a not in ids]
            if unknown:
                invalid[e["id"]] = f"after: no element {', '.join(unknown)}"
        deps = {e["id"]: [a for a in e["after"] if a in ids] for e in elements}
        state: dict[str, str] = {}

        def visit(n: str, path: list[str]) -> None:
            if state.get(n) == "done":
                return
            if state.get(n) == "open":
                cycle = path[path.index(n) :] + [n]
                for c in cycle:
                    invalid.setdefault(c, "after makes a cycle: " + " -> ".join(cycle))
                return
            state[n] = "open"
            for d in deps[n]:
                visit(d, path + [n])
            state[n] = "done"

        for e in elements:
            visit(e["id"], [])
        for e in elements:  # waiting on an invalid element is invalid too
            if e["id"] not in invalid and any(a in invalid for a in e["after"]):
                invalid[e["id"]] = "after: depends on an invalid element"
        return invalid

    @staticmethod
    def in_dependency_order(elements: list, invalid: dict) -> list:
        """Dependencies first, otherwise list order."""
        by_id = {e["id"]: e for e in elements}
        position = {e["id"]: i for i, e in enumerate(elements)}
        return sorted(elements, key=lambda e: (Run._depth(e, by_id, invalid), position[e["id"]]))

    @staticmethod
    def _depth(e: dict, by_id: dict, invalid: dict, seen: frozenset = frozenset()) -> int:
        if e["id"] in invalid or e["id"] in seen:
            return 0
        deps = [by_id[a] for a in e["after"] if a in by_id]
        return 1 + max((Run._depth(d, by_id, invalid, seen | {e["id"]}) for d in deps), default=-1)

    # ------------------------------------------------------------ parallel and when

    def walk_parallel(self, node: dict, ctx: Ctx, reachable: bool, reason: dict | None) -> bool:
        branches = node["parallel"]
        owner = {}
        for name, nodes in branches.items():
            for s in self._steps(nodes):
                owner[s["step"]] = name
            for n in nodes:
                if isinstance(n, dict) and "id" in n:
                    owner[n["id"]] = name
        deps = {
            name: {t if t in branches else owner[t] for n in nodes if isinstance(n, dict) for t in n.get("after", [])}
            for name, nodes in branches.items()
        }
        done: dict[str, bool] = {}
        remaining = list(branches)
        while remaining:
            ready = [b for b in remaining if deps[b] <= set(done)] or remaining[:1]
            for b in ready:
                waiting = sorted(d for d in deps[b] if not done.get(d))
                here_reason = reason if not reachable else ({"order": waiting} if waiting else None)
                done[b] = self.walk(branches[b], ctx, reachable and not waiting, here_reason)
                remaining.remove(b)
        return all(done.values())

    def walk_when(self, node: dict, ctx: Ctx, reachable: bool, reason: dict | None) -> bool:
        if not reachable:
            self.placeholder(node, ctx, reason)
            return False
        verdict, why = self.condition(node["when"], ctx)
        if verdict is UNKNOWN:
            self.placeholder(node, ctx, {"condition": why})
            return False
        chosen = node["steps"] if verdict else node.get("else", [])
        return self.walk(chosen, ctx, True, None) if chosen else True

    def condition(self, cond: dict, ctx: Ctx) -> tuple[bool | None, str | None]:
        if "all" in cond or "any" in cond:
            parts = [self.condition(c, ctx) for c in cond.get("all", cond.get("any"))]
            values = [v for v, _ in parts]
            decisive = False if "all" in cond else True
            if decisive in values:
                return decisive, None
            unknown = [w for v, w in parts if v is UNKNOWN]
            return (UNKNOWN, unknown[0]) if unknown else (not decisive, None)
        if "not" in cond:
            value, why = self.condition(cond["not"], ctx)
            return (UNKNOWN, why) if value is UNKNOWN else (not value, None)
        if "artifact" in cond:
            return self.artifact_condition(cond, ctx)
        if "verdict" in cond:
            return self.verdict(cond["verdict"]) == cond["is"], None
        tracker = cond["tracker"]
        if "field" in tracker:
            return UNKNOWN, "tracker fields are read from step R3"
        item = self.item()
        if item is None:
            return UNKNOWN, "the run has no tracker item"
        if "label" in tracker:
            return tracker["label"] in item["labels"], None
        return item["type"] == tracker["type"], None

    def artifact_condition(self, cond: dict, ctx: Ctx) -> tuple[bool | None, str | None]:
        name, *path = cond["artifact"].split(".")
        if "path" in cond:
            path = path + cond["path"].split(".")
        data, problem = self.read_artifact(name, ctx, parse="section" not in cond and "exists" not in cond)
        if "exists" in cond:
            return (problem is None) == cond["exists"], None
        if problem:
            return False, None
        if "section" in cond:
            text = data.decode() if isinstance(data, bytes) else str(data)
            heading = re.compile(rf"^#+\s+{re.escape(cond['section'])}\s*$", re.M)
            return bool(heading.search(text)), None
        for key in path:
            if isinstance(data, list) and key.isdigit() and int(key) < len(data):
                data = data[int(key)]
            elif isinstance(data, dict):
                data = data.get(key)
            else:
                data = None
        return (data == cond["equals"]) if "equals" in cond else (data in cond["in"]), None

    def verdict(self, step: str) -> str | None:
        """The decision of the auto gate on a step's latest attempt, if it has one yet."""
        for u in self.units:
            if u.step and u.step["step"] == step and u.element_id is None and u.latest:
                entry = u.gates.get(u.latest["attempt"])
                return entry["gate"].get("decision") if entry else None
        return None

    # ------------------------------------------------------------ artifacts

    def artifact_location(self, name: str, ctx: Ctx) -> tuple[Repo, str]:
        art = self.w.artifacts[name]
        repo = art["file"]["repo"]
        if repo.startswith("unit."):
            repo = self.element_field(ctx, repo.removeprefix("unit."))
        path = art["file"]["path"].replace("{run}", self.run)
        if ctx.element:
            path = path.replace("{unit}", ctx.element["id"])
        return self.repos[repo], path

    def read_artifact(self, name: str, ctx: Ctx, parse: bool = True) -> tuple[object, dict | None]:
        repo, path = self.artifact_location(name, ctx)
        ref = repo.fresh(self.parent_in(repo, ctx.parent)) or repo.fresh(repo.base)
        raw = repo.read_file(ref, path) if ref else None
        if raw is None:
            return None, {"data": f"{name} is missing: {repo.name}:{path}"}
        fmt = self.w.artifacts[name]["file"].get("format")
        if not parse or fmt not in ("yaml", "json"):
            return raw, None
        try:
            return (json.loads(raw) if fmt == "json" else yaml.safe_load(raw)), None
        except (ValueError, yaml.YAMLError) as exc:
            return None, {"data": f"{name} doesn't parse as {fmt}: {str(exc).splitlines()[0]}"}

    def element_field(self, ctx: Ctx, name: str):
        if ctx.element is None:
            raise Unresolved("unit")
        value = ctx.element[name] if name in ELEMENT_KEYS else ctx.element["raw"].get(name)
        if value is None:
            raise Unresolved(f"unit.{name}")
        return value

    # ------------------------------------------------------------ steps

    def lookup(self, ctx: Ctx):
        def resolve(name: str):
            if name == "run":
                return self.run
            if name == "unit":
                return self.element_field(ctx, "id")
            head, _, rest = name.partition(".")
            if head == "unit":
                return self.element_field(ctx, rest)
            if head == "run":
                item = self.item()
                if item is None or rest not in item:
                    raise Unresolved(name)
                return item[rest]
            for u in self.units:
                if u.step and u.step["step"] == head and u.state == "done" and u.latest:
                    data = u.latest.get("data") or {}
                    if rest in data:
                        return data[rest]
            raise Unresolved(name)

        return resolve

    def walk_step(self, node, ctx: Ctx, reachable: bool, reason: dict | None) -> bool:
        step = self.step_of(node)
        uid = ctx.prefix + step["step"] + (f"-{ctx.element['id']}" if ctx.element else "")
        gate = "none" if isinstance(node, str) else node.get("gate", "none")
        unit = Unit(id=uid, step=step, label=uid, ctx=ctx, gate=gate)
        self.units.append(unit)
        try:
            unit.write = [
                self.element_field(ctx, r.removeprefix("unit.")) if r.startswith("unit.") else r
                for r in step.get("repos", {}).get("write", [])
            ]
        except Unresolved as exc:
            unit.reason = {"data": f"{{{exc}}} has no value"}
            return False
        unit.ledger_repo = unit.write[0] if unit.write else "home"
        unit.branch = None if self.ledger_branch else self.branch_name(uid)
        unit.claims = self.claims(step, ctx, unit.write)
        if not reachable:
            unit.reason = reason
            self.load_latest(unit)
            return False
        self.state(unit)
        return unit.state == "done"

    def claims(self, step: dict, ctx: Ctx, write: list[str]) -> dict:
        if not write:
            return {}
        paths = step.get("paths")
        if paths is None:
            globs = [EVERYTHING]
        elif isinstance(paths, str):
            try:
                globs = self.element_field(ctx, paths.removeprefix("unit."))
            except Unresolved:
                globs = [EVERYTHING]
            globs = globs if isinstance(globs, list) else [globs]
        else:
            globs = [
                p.replace("{run}", self.run).replace("{unit}", ctx.element["id"] if ctx.element else "") for p in paths
            ]
        return {repo: globs for repo in write}

    def target(self, unit: Unit) -> str:
        """The branch the unit's ledger entry must reach for it to be done."""
        repo = self.repos[unit.ledger_repo]
        return self.ledger_branch or self.parent_in(repo, unit.ctx.parent)

    def sources(self, unit: Unit) -> list[tuple[str, object]]:
        """Where its entries may be, freshest first: the checkout of its branch, its branch, its target."""
        repo = self.repos[unit.ledger_repo]
        own = self.ledger_branch or unit.branch
        out = []
        tree = repo.worktrees().get(own)
        if tree is not None:
            out.append(("worktree", tree))
        for branch in (own, self.target(unit)):
            ref = repo.fresh(branch)
            if ref:
                out.append(("ref", ref))
        return out

    def load_latest(self, unit: Unit) -> None:
        repo = self.repos[unit.ledger_repo]
        names: dict[str, tuple] = {}
        for kind, where in reversed(self.sources(unit)):
            if kind == "worktree":
                listed = {p.name for p in (where / self.ledger_dir).glob("*.yaml")}
            else:
                listed = repo.names(where, self.ledger_dir)
            for n in listed:
                names[n] = (kind, where)
        for n in gate_attempts(set(names), unit.id):
            name = entry_name(unit.id, n, gate=True)
            kind, where = names[name]
            unit.gates[n] = parse(self._read_entry(repo, kind, where, name), f"{repo.name}:{self.ledger_dir}/{name}")
        found = attempts(set(names), unit.id)
        if not found:
            unit.attempt = 1
            return
        unit.attempt = found[-1]
        for n in found:
            name = entry_name(unit.id, n)
            kind, where = names[name]
            unit.history.append(
                parse(self._read_entry(repo, kind, where, name), f"{repo.name}:{self.ledger_dir}/{name}")
            )
        unit.latest = unit.history[-1]

    def _read_entry(self, repo: Repo, kind: str, where, name: str) -> bytes:
        if kind == "worktree":
            return (where / self.ledger_dir / name).read_bytes()
        return repo.read_file(where, f"{self.ledger_dir}/{name}")

    def gate_running(self, unit: Unit) -> str:
        """`running` while a gate hand-out waits for its record, else `gate`: one is due."""
        return "running" if (self.pending_dir() / f"{unit.id}-gate.json").is_file() else "gate"

    def on_branch(self, unit: Unit, branch: str | None) -> bool:
        if not unit.latest:
            return False
        repo = self.repos[unit.ledger_repo]
        ref = repo.fresh(self.ledger_branch or self.parent_in(repo, branch))
        return bool(ref) and entry_name(unit.id, unit.attempt) in repo.names(ref, self.ledger_dir)

    def gate_kind(self, gate) -> str:
        if isinstance(gate, str):
            return "merge" if gate == "none" else gate
        return "human" if "human" in gate else "gate"

    def state(self, unit: Unit) -> None:
        self.load_latest(unit)
        entry = unit.latest
        if entry:
            rework = self.rework(unit, entry)
            if rework:
                failing = 0
                for past in reversed(unit.history):
                    if not any(v != "pass" for v in past.get("verify", {}).values()):
                        break
                    failing += 1
                limit = unit.gate.get("retry", 2) if isinstance(unit.gate, dict) and "auto" in unit.gate else 2
                if set(rework) <= {"verify", "previous"} and failing > limit:
                    unit.state, unit.reason = "blocked", {"failed": f"checks failed {failing} times"}
                    return
                unit.rework, unit.attempt = rework, unit.attempt + 1
                running = (self.pending_dir() / f"{unit.id}.json").is_file()
                unit.state = "running" if running else "ready"
                unit.reason = {"rework": sorted(k for k in rework if k != "previous")}
                return
            self.gated_state(unit, entry)
            return
        problem = self.missing_inputs(unit)
        if problem:
            unit.state, unit.reason = "blocked", problem
            return
        running = (self.pending_dir() / f"{unit.id}.json").is_file()
        unit.state, unit.reason = ("running" if running else "ready"), None

    def gated_state(self, unit: Unit, entry: dict) -> None:
        """A recorded attempt that stands: its failure, its questions, or where its gate is."""
        if self.ledger_branch is None and self.on_branch(unit, unit.ctx.parent):
            unit.state, unit.reason = "done", None  # merged: whoever merged it decided
            return
        if entry.get("exit", 0) != 0:
            unit.state, unit.reason = "blocked", {"failed": f"exit {entry['exit']}"}  # until an input changes
            return
        if entry.get("questions"):
            unit.state, unit.reason = "waiting", {"questions": len(entry["questions"])}
            return
        auto = unit.auto
        if auto is not None:
            decided = unit.gates.get(unit.attempt)
            if decided is None:
                unit.state, unit.reason = "waiting", {"gate": self.gate_running(unit)}
                return
            decision = decided["gate"]["decision"]
            if decision == "reject":
                rejects = 0
                for n in range(unit.attempt, 0, -1):
                    if unit.gates.get(n, {}).get("gate", {}).get("decision") != "reject":
                        break
                    rejects += 1
                if rejects <= unit.gate.get("retry", 2):
                    unit.rework = {"rejected": decided.get("verdict", {}), "previous": entry.get("summary", "")}
                    unit.attempt += 1
                    running = (self.pending_dir() / f"{unit.id}.json").is_file()
                    unit.state, unit.reason = ("running" if running else "ready"), {"rework": ["rejected"]}
                elif unit.gate.get("then", "human") == "fail":
                    unit.state, unit.reason = "blocked", {"failed": f"rejected {rejects} times"}
                else:
                    unit.state, unit.reason = "waiting", {"escalated": f"rejected {rejects} times"}
                return
            if decision == "escalate":
                unit.state, unit.reason = "waiting", {"escalated": "the gate escalated"}
                return
        if self.ledger_branch is not None:  # nothing to merge: the gate is the decision
            human = unit.gate == "human" or isinstance(unit.gate, dict) and "human" in unit.gate
            done = self.on_branch(unit, None) and not human
            unit.state, unit.reason = ("done", None) if done else ("waiting", {"gate": "human" if human else "merge"})
            return
        merge = "merge" if unit.gate == "none" or (auto is not None and unit.gate.get("merge") == "auto") else "human"
        unit.state, unit.reason = "waiting", {"gate": merge}

    def rework(self, unit: Unit, entry: dict) -> dict:
        """Why the recorded attempt no longer stands: its checks failed, its inputs changed, or an output it
        wrote is gone. A person's edit of an output isn't a reason: it is reviewed like the rest (D31)."""
        why: dict = {}
        failed = {k: v for k, v in entry.get("verify", {}).items() if v != "pass"}
        if failed and not entry.get("questions"):  # an attempt that asks may not have written its outputs yet
            why["verify"] = failed
        now = input_hashes(self, unit)
        before = entry.get("inputs", {})
        stale = sorted(k for k in now.keys() | before.keys() if now.get(k) != before.get(k))
        if stale:
            why["stale"] = stale
        missing = [n for n in entry.get("outputs", {}) if not self.output_exists(unit, n)]
        if missing:
            why["missing"] = missing
        if why:
            why["previous"] = entry.get("summary", "")
        return why

    def output_exists(self, unit: Unit, name: str) -> bool:
        repo, path = self.artifact_location(name, unit.ctx)
        tree = repo.worktrees().get(unit.branch) if unit.branch else None
        if tree is not None:
            return (tree / path).exists()
        for branch in (unit.branch, self.parent_in(repo, unit.ctx.parent)):
            ref = repo.fresh(branch) if branch else None
            if ref:
                return repo.read_file(ref, path) is not None or bool(repo.names(ref, path))
        return False

    def missing_inputs(self, unit: Unit) -> dict | None:
        for name in unit.step.get("inputs", []):
            if name == "unit" or name.startswith("unit."):
                continue
            if self.w.artifacts[name]["kind"] != "file":
                continue
            _, problem = self.read_artifact(name, unit.ctx, parse=False)
            if problem:
                return problem
        return None

    # ------------------------------------------------------------ claims and limits

    def apply_claims(self) -> None:
        holders: list[Unit] = []
        for u in self.units:
            if not u.step or u.state not in ("ready", "running", "waiting", "blocked") or not u.claims:
                continue
            if u.state == "blocked" and (u.reason or {}).keys() != {"failed"}:
                continue
            if u.state == "ready":
                holder = next((h for h in holders if overlap(h.claims, u.claims)), None)
                if holder:
                    u.state, u.reason = "blocked", {"claim": holder.id}
                    continue
            holders.append(u)

    def apply_limits(self) -> None:
        """`parallel.max` caps the units of a run at once; a foreach's `max` caps its units per step."""
        total = self.w.project.get("parallel", {}).get("max")
        count = sum(1 for u in self.units if u.state == "running")
        per_step: dict[tuple, int] = {}
        for u in self.units:
            if u.state == "running" and u.ctx.max is not None:
                key = (u.ctx.prefix, u.step["step"])
                per_step[key] = per_step.get(key, 0) + 1
        for u in self.units:
            if u.state != "ready":
                continue
            if total is not None and count >= total:
                u.state, u.reason = "blocked", {"limit": f"parallel.max {total}"}
                continue
            if u.ctx.max is not None:
                key = (u.ctx.prefix, u.step["step"])
                if per_step.get(key, 0) >= u.ctx.max:
                    u.state, u.reason = "blocked", {"limit": f"foreach max {u.ctx.max}"}
                    continue
                per_step[key] = per_step.get(key, 0) + 1
            count += 1
