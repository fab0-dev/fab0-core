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
  `tracker` (an item the tracker says comes first is open), `invalid-dependency`, `condition` (a `when`
  can't be decided yet), `failed` (a command exited non-zero), or `start` (the run hasn't started).
- `done`: its ledger entry is on the branch it merges into; for an action, its check passes.

Actions (the pipeline's and its blocks' `onStart` and `onEnd`, and action steps) are units too. An action is
done when its check passes: its result is a fact in git or the tracker, so it writes no ledger entry (D37).

Branches (D10, D14): in `run` branching every unit works on its own branch and merges into its parent: the
run branch, or the branch of the gated block around it. In `flat` branching the parent is the base branch.
A pipeline whose steps write no files keeps its ledger on `ledger.branch` (D18), and its units work there.

The tracker's relations are dependencies too (P11, D49). A foreach element's item is the sub-issue
`tracker.child.sync` made for it; once it exists, its blocked-by relations replace the element's `after`.
Blocked-by relations of the run's item hold back the whole run, and its other sub-issues hold back its end.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .actions import check, element_items
from .artifacts import input_hashes
from .claims import EVERYTHING, overlap
from .git import Repo
from .hashing import sha_json
from .ledger import DEFAULT_PATH, attempts, entry_name, gate_attempts, parse
from .templates import Unresolved
from .wiring import EXECUTORS, Wiring, base_allowed

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
    lifecycle: str | None = None  # `onStart` or `onEnd`, for an action of the pipeline or a block
    warnings: list = field(default_factory=list)
    elements: list | None = None  # a foreach's elements, for its own actions

    @property
    def auto(self) -> dict | None:
        return self.gate["auto"] if isinstance(self.gate, dict) and "auto" in self.gate else None

    @property
    def element_id(self) -> str | None:
        return self.ctx.element["id"] if self.ctx.element else None

    def summary(self) -> dict:
        out = {"unit": self.id, "label": self.label, "state": self.state}
        if self.warnings:
            out["warnings"] = self.warnings
        if self.step:
            out["step"] = self.step["step"]
        if self.element_id:
            out["element"] = self.element_id
        if self.step and "action" in self.step:
            out["action"] = self.step["action"]
        if self.lifecycle:
            out["lifecycle"] = self.lifecycle
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
        self._children: list | None = None
        self.element_refs: set[str] = set()  # the tracker items of the run's foreach elements
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

    def item_ref(self) -> str:
        return f"{self.home.github}#{self.run}"

    def children(self) -> list[dict]:
        """The sub-issues of the run's item."""
        if self._children is None:
            self._children = [] if self.item() is None else self.tracker.sub_issues(self.home.github, int(self.run))
        return self._children

    def blockers(self, repo: str, number: int, inside: dict[str, str]) -> tuple[list, list, str | None]:
        """What an item's blocked-by relations mean: the elements of this block it waits on (`inside` maps
        their items to their ids), the open items outside it, and why the relations are invalid, if they
        are."""
        after, open_, invalid = [], [], None
        for b in self.tracker.blocked_by(repo, number):
            ref = f"{b['repo']}#{b['number']}"
            if ref in inside:
                after.append(inside[ref])
            elif ref == self.item_ref():
                invalid = f"blocked by its own run's item {ref}"
            elif b["state"] != "closed":
                open_.append(ref)
            elif b.get("state_reason") == "not_planned":
                invalid = f"blocked by {ref}, closed as not planned"
        return after, open_, invalid

    def recorded(self) -> bool:
        """The run has a ledger entry somewhere: on its run branch, its ledger branch or a unit branch."""
        unit_prefix = self.unit_template.replace("{run}", self.run).split("{unit}")[0]
        for repo in self.repos.values():
            if not repo.exists():
                continue
            refs = repo.out("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
            for ref in refs.splitlines():
                branch = ref.removeprefix("origin/")
                if branch in (self.run_branch, self.ledger_branch) or branch.startswith(unit_prefix):
                    if any(n.endswith(".yaml") for n in repo.names(ref, self.ledger_dir)):
                        return True
        return False

    def pending_dir(self) -> Path:
        return self.home.common_dir() / "fab0" / "pending" / self.run  # shared by every checkout of home

    # ------------------------------------------------------------ the walk

    def evaluate(self) -> dict:
        """Every unit and its state; and the run's: `units` while work is left, `waiting` for the merge of the
        run's pull request, `done`."""
        out = {"ok": True, "run": self.run, "pipeline": self.name}
        ctx = Ctx(parent=self.run_branch)
        if self.recorded():  # started: what onStart did may be undone on purpose, by onEnd or a person (D53)
            for i, action in enumerate(self.p["onStart"]):
                name = action if isinstance(action, str) else next(iter(action))
                uid = f"onStart-{i + 1}"
                step = {"step": uid, "action": name, "with": {} if isinstance(action, str) else action[name]}
                self.units.append(Unit(id=uid, step=step, label=name, ctx=ctx, lifecycle="onStart", state="done"))
            started = True
        else:
            started = self.walk_actions(self.p["onStart"], "onStart", "", ctx, True, None)
        steps = self.walk(self.p["steps"], ctx, started, None if started else {"order": "onStart"})
        end = len(self.units)
        ended = self.walk_actions(self.p["onEnd"], "onEnd", "", ctx, steps, None if steps else {"order": "steps"})
        self.apply_tracker(self.units[end:])
        self.apply_claims()
        self.apply_limits()
        out["units"] = [u.summary() for u in self.units]
        on = self.home.out("branch", "--show-current").strip()
        if on and on != self.home.base:  # from R6: its .fab0/ may not be the project's current wiring
            out["warnings"] = [f"the home checkout is on {on}, not {self.home.base}; its .fab0/ wiring is the one used"]
        if self.run_branch:
            out["branch"] = self.run_branch
        if not ended:
            out["kind"] = "units"
        elif self.run_branch and not self.merged():
            out["kind"], out["reason"] = "waiting", {"gate": "human", "merge": f"{self.run_branch} into the base"}
        else:
            out["kind"] = "done"
        return out

    def merged(self) -> bool:
        """The run branch is in the base of every repository that has it."""
        for repo in self.repos.values():
            head, base = repo.fresh(self.run_branch), repo.fresh(repo.base)
            if head and (not base or not repo.is_ancestor(head, base)):
                return False
        return True

    def walk_actions(
        self,
        actions: list,
        phase: str,
        prefix: str,
        ctx: Ctx,
        reachable: bool,
        reason: dict | None,
        elements: list | None = None,
    ) -> bool:
        """A list of actions, in order; True when every one's check passes."""
        all_done = True
        for i, action in enumerate(actions):
            name = action if isinstance(action, str) else next(iter(action))
            args = {} if isinstance(action, str) else action[name]
            uid = f"{prefix}{phase}-{i + 1}"
            unit = Unit(
                id=uid,
                step={"step": uid, "action": name, "with": args},
                label=name,
                ctx=ctx,
                lifecycle=phase,
                elements=elements,
            )
            self.units.append(unit)
            if not (reachable and all_done):
                unit.reason = reason if not reachable else {"order": f"{phase}-{i}"}
                all_done = False
                continue
            all_done = self.action_state(unit) and all_done
        return all_done

    def action_state(self, unit: Unit) -> bool:
        try:
            missing = check(self, unit.step["action"], unit.step.get("with", {}), unit.ctx, unit.elements)
        except Unresolved as exc:
            unit.state, unit.reason = "blocked", {"data": f"{{{exc}}} has no value"}
            return False
        if missing is None:
            unit.state, unit.reason = "done", None
            return True
        unit.state, unit.reason = "ready", {"missing": missing}
        return False

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
            return self.walk_foreach(node, ctx, reachable, reason, pos)
        if "parallel" in node:
            return self.walk_parallel(node, ctx, reachable, reason, pos)
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

    def block_prefix(self, node: dict, ctx: Ctx, kind: str, pos: str) -> str:
        """What a block's own actions are named under: inside a gated block, its prefix is already there."""
        block_id = node.get("id") or f"{kind}-{pos}"
        return ctx.prefix if ctx.prefix.endswith(block_id + "--") else f"{ctx.prefix}{block_id}--"

    # ------------------------------------------------------------ foreach

    def walk_foreach(self, node: dict, ctx: Ctx, reachable: bool, reason: dict | None, pos: str) -> bool:
        if not reachable:
            self.placeholder(node, ctx, reason)
            return False
        elements, problem = self.elements(node["foreach"], ctx)
        if problem:
            self.placeholder(node, ctx, problem)
            return False
        prefix = self.block_prefix(node, ctx, "foreach", pos)
        started = self.walk_actions(node.get("onStart", []), "onStart", prefix, ctx, True, None, elements)
        ids = [e["id"] for e in elements]
        elements, items, outside, invalid = (
            self.element_dependencies(node, ctx, elements) if started else (elements, {}, {}, {})
        )
        done: dict[str, bool] = {}
        invalid = self.invalid_after(elements, invalid)
        start = len(self.units)
        for element in self.in_dependency_order(elements, invalid):
            inner = Ctx(parent=ctx.parent, prefix=ctx.prefix, element=element, max=node.get("max"))
            if element["id"] in invalid:
                self.walk(node["steps"], inner, False, {"invalid-dependency": invalid[element["id"]]})
                done[element["id"]] = False
                continue
            waiting = [d for d in element["after"] if not done.get(d)]
            closed = [items[d] for d in waiting if items.get(d, {}).get("state") == "closed"]
            if closed:  # a person closed it, but its work isn't merged: which way to go is theirs to say
                refs = ", ".join(f"{c['repo']}#{c['number']}" for c in closed)
                why = {"invalid-dependency": f"{refs} closed, but its work isn't merged"}
                self.walk(node["steps"], inner, False, why)
                done[element["id"]] = False
                continue
            here_reason = {"order": waiting} if waiting else None if started else {"order": "onStart"}
            first = len(self.units)
            done[element["id"]] = self.walk(node["steps"], inner, started and not waiting, here_reason)
            for u in self.units[first:]:
                if outside.get(element["id"]) and u.state == "ready":
                    u.state, u.reason = "blocked", {"tracker": outside[element["id"]]}
        # walked dependencies first, so `done` is known; listed in the list's order, which decides claims
        position = {i: n for n, i in enumerate(ids)}
        self.units[start:] = sorted(self.units[start:], key=lambda u: position[u.element_id])
        units_done = all(done.get(i) for i in ids)
        why = None if units_done else {"order": "the block's units"}
        ended = self.walk_actions(node.get("onEnd", []), "onEnd", prefix, ctx, units_done, why, elements)
        return units_done and ended

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

    def element_dependencies(self, node: dict, ctx: Ctx, elements: list) -> tuple[list, dict, dict, dict]:
        """The elements as their tracker items have them: for an element with an item, its `after` becomes the
        elements its item is blocked by, except that an `after` naming no element stays, so it is reported.
        Also the items, the open items outside the block each element waits on, and invalid relations."""
        sync = None
        for action in node.get("onStart", []):
            name = action if isinstance(action, str) else next(iter(action))
            if name == "tracker.child.sync":
                sync = {} if isinstance(action, str) else action[name]
        if sync is None or self.item() is None:
            return elements, {}, {}, {}
        items = element_items(self, sync, ctx, elements, self.children())
        inside = {f"{c['repo']}#{c['number']}": i for i, c in items.items()}
        self.element_refs |= set(inside)
        ids = {e["id"] for e in elements}
        out, outside, invalid = [], {}, {}
        for element in elements:
            item = items.get(element["id"])
            if item is None:
                out.append(element)
                continue
            after, open_, why = self.blockers(item["repo"], item["number"], inside)
            unknown = [a for a in element["after"] if a not in ids]
            out.append({**element, "after": after + unknown, "item": f"{item['repo']}#{item['number']}"})
            if open_:
                outside[element["id"]] = open_
            if why:
                invalid[element["id"]] = why
        return out, items, outside, invalid

    @staticmethod
    def invalid_after(elements: list, invalid: dict | None = None) -> dict[str, str]:
        ids = {e["id"] for e in elements}
        invalid = dict(invalid or {})
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
        spread = True
        while spread:  # waiting on an invalid element is invalid too
            spread = False
            for e in elements:
                if e["id"] not in invalid and any(a in invalid for a in e["after"]):
                    invalid[e["id"]] = "after: depends on an invalid element"
                    spread = True
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

    def walk_parallel(self, node: dict, ctx: Ctx, reachable: bool, reason: dict | None, pos: str) -> bool:
        prefix = self.block_prefix(node, ctx, "parallel", pos)
        started = self.walk_actions(node.get("onStart", []), "onStart", prefix, ctx, reachable, reason)
        if reachable and not started:
            reachable, reason = False, {"order": "onStart"}
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
        units_done = all(done.values())
        why = reason if not reachable else None if units_done else {"order": "the block's units"}
        ended = self.walk_actions(node.get("onEnd", []), "onEnd", prefix, ctx, reachable and units_done, why)
        return units_done and ended

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
        item = self.item()
        if item is None:
            return UNKNOWN, "the run has no tracker item"
        if "field" in tracker:
            board = self.w.project["tracker"]
            have = self.tracker.fields(self.home.github, int(self.run), board.get("owner"), board.get("project"))
            name = board.get("fields", {}).get(tracker["field"]["name"], tracker["field"]["name"])
            return have.get(name) == tracker["field"]["equals"], None
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
        if raw is None and ref and self.w.artifacts[name].get("keep") == "run":
            before = repo.removed_at(ref, path)  # a run artifact the run's end removed: read it as it was (D38)
            raw = repo.read_file(before, path) if before else None
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
        if "action" in step:  # applied, not recorded: its check is its state
            if not reachable:
                unit.reason = reason
                return False
            return self.action_state(unit)
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
        labelled = self.labelled(unit)
        if self.ledger_branch is not None:  # nothing to merge: the gate is the decision
            human = (unit.gate == "human" or isinstance(unit.gate, dict) and "human" in unit.gate) and not labelled
            done = self.on_branch(unit, None) and not human
            unit.state, unit.reason = ("done", None) if done else ("waiting", {"gate": "human" if human else "merge"})
            return
        merge = (
            "merge"
            if unit.gate == "none" or labelled or (auto is not None and unit.gate.get("merge") == "auto")
            else "human"
        )
        if merge == "merge" and unit.ctx.parent is None and not base_allowed(self.policy):
            merge = "human"  # into the base branch, only a person merges unless the policy allows it (from R8)
        if self.merge_trouble(unit, entry, merge):
            return
        unit.state, unit.reason = "waiting", {"gate": merge}

    def labelled(self, unit: Unit) -> bool:
        """A human gate decided through the tracker: the run's item carries the gate's label."""
        human = unit.gate.get("human") if isinstance(unit.gate, dict) else None
        if not isinstance(human, dict) or human.get("via") != "label":
            return False
        item = self.item()
        return item is not None and human["label"] in item["labels"]

    def merge_trouble(self, unit: Unit, entry: dict, merge: str) -> bool:
        """A unit ready to merge whose branch conflicts with its parent, or whose required checks fail, goes
        back to its producer once with the details; if the attempt made to fix it has the same trouble, a
        person decides (D45)."""
        trouble = {}
        conflicts = self.conflicts(unit)
        if conflicts:
            trouble["conflict"] = conflicts
        elif merge == "merge" and unit.auto is not None:
            failing = self.failing_checks(unit)
            if failing:
                trouble["checks"] = failing
        if not trouble:
            return False
        kind = next(iter(trouble))
        if kind in entry.get("rework", []):
            again = "its branch conflicts with its parent again" if kind == "conflict" else "its checks fail again"
            unit.state, unit.reason = "waiting", {"escalated": again}
            return True
        unit.rework = {**trouble, "previous": entry.get("summary", "")}
        unit.attempt += 1
        running = (self.pending_dir() / f"{unit.id}.json").is_file()
        unit.state, unit.reason = ("running" if running else "ready"), {"rework": [kind]}
        return True

    def conflicts(self, unit: Unit) -> dict[str, list[str]]:
        """Per repository, the files a unit's branch would conflict on when merged into its parent."""
        found = {}
        for name in dict.fromkeys(unit.write or [unit.ledger_repo]):
            repo = self.repos[name]
            base, head = repo.fresh(self.parent_in(repo, unit.ctx.parent)), repo.fresh(unit.branch)
            if base and head and not repo.is_ancestor(head, base):
                files = repo.merge_conflicts(base, head)
                if files:
                    found[name] = files
        return found

    def failing_checks(self, unit: Unit) -> dict[str, list[str]]:
        found = {}
        for name in dict.fromkeys(unit.write or [unit.ledger_repo]):
            repo = self.repos[name]
            pr = self.tracker.pull(repo.github, unit.branch, self.parent_in(repo, unit.ctx.parent))
            if pr is not None and pr["state"] == "open":
                state, failed = self.tracker.checks(repo.github, pr["number"])
                if state == "fail":
                    found[f"{repo.github}#{pr['number']}"] = failed
        return found

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
        if stale and self.ledger_branch is None and self.on_branch(unit, unit.ctx.parent):
            # merged: its own pull request's reviews were the gate, and the merge decided (D34, D61)
            stale = [k for k in stale if not self.own_pull_input(k)]
        if stale:
            why["stale"] = stale
        missing = [n for n in entry.get("outputs", {}) if not self.output_exists(unit, n)]
        if missing:
            why["missing"] = missing
        if why:
            why["previous"] = entry.get("summary", "")
        return why

    def own_pull_input(self, name: str) -> bool:
        art = self.w.artifacts.get(name, {})
        return art.get("kind") == "pr" and art["pr"].get("of", "run") == "unit"

    def output_exists(self, unit: Unit, name: str) -> bool:
        repo, path = self.artifact_location(name, unit.ctx)
        tree = repo.worktrees().get(unit.branch) if unit.branch else None
        if tree is not None:
            return (tree / path).exists()
        for branch in (unit.branch, self.parent_in(repo, unit.ctx.parent)):
            ref = repo.fresh(branch) if branch else None
            if ref:
                if repo.read_file(ref, path) is not None or repo.names(ref, path):
                    return True
                # a run artifact the run's end removed still counts as written (D38)
                return self.w.artifacts[name].get("keep") == "run" and repo.removed_at(ref, path) is not None
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

    # ------------------------------------------------------------ the tracker, claims and limits

    def apply_tracker(self, end: list[Unit]) -> None:
        """The run's item: its blocked-by relations hold back every unit not under way, and its sub-issues
        other than the elements' items hold back the run's end."""
        item = self.item()
        if item is None:
            return
        _, open_, invalid = self.blockers(self.home.github, int(self.run), {})
        if open_ or invalid:
            why = {"invalid-dependency": invalid} if invalid else {"tracker": open_}
            for u in self.units:
                if u.state == "ready":
                    u.state, u.reason = "blocked", why
            return
        if not any(u.state == "ready" for u in end):
            return
        children = [c for c in self.children() if f"{c['repo']}#{c['number']}" not in self.element_refs]
        left = [f"{c['repo']}#{c['number']}" for c in children if c["state"] != "closed"]
        if left:
            for u in end:
                if u.state == "ready":
                    u.state, u.reason = "blocked", {"tracker": left}

    def other_claims(self) -> list[tuple[str, str, dict]]:
        """The claims of other open runs: (run, unit, {repo: [globs]}) from the latest ledger entry on each of
        their unit branches that isn't in its repository's base yet (D47)."""
        unit_pattern = re.compile(
            "^"
            + re.escape(self.unit_template).replace(r"\{run\}", "(?P<run>[^/]+)").replace(r"\{unit\}", "(?P<unit>.+)")
            + "$"
        )
        run_template = self.w.git_policy(self.p).get("branches", {}).get("run", "fab0/{run}/base")
        ledger_path = self.w.project.get("ledger", {}).get("path", DEFAULT_PATH)
        found = []
        for repo in self.repos.values():
            if not repo.exists():
                continue
            base = repo.fresh(repo.base)
            refs = repo.out("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
            seen = set()
            for ref in refs.splitlines():
                branch = ref.removeprefix("origin/")
                m = unit_pattern.match(branch)
                if not m or m["run"] == self.run or branch in seen or branch == run_template.replace("{run}", m["run"]):
                    continue
                seen.add(branch)
                head = repo.fresh(branch)
                if base and repo.is_ancestor(head, base):
                    continue
                folder = ledger_path.replace("{run}", m["run"])
                entries = sorted(attempts(repo.names(head, folder), m["unit"]))
                if not entries:
                    continue
                raw = repo.read_file(head, f"{folder}/{entry_name(m['unit'], entries[-1])}")
                claims: dict[str, list[str]] = {}
                for item in (parse(raw, branch).get("claims") or []) if raw else []:
                    name, _, glob = item.partition(":")
                    claims.setdefault(name, []).append(glob)
                if claims:
                    found.append((m["run"], m["unit"], claims))
        return found

    def apply_claims(self) -> None:
        policy = self.w.project.get("parallel", {}).get("cross_run", "warn")
        ready = [u for u in self.units if u.step and u.state == "ready" and u.claims]
        others = self.other_claims() if ready else []
        for u in ready:
            hits = [f"run {r}, {unit}" for r, unit, claims in others if overlap(claims, u.claims)]
            if hits and policy == "block":
                u.state, u.reason = "blocked", {"cross-run": hits}
            elif hits:
                u.warnings = [f"claims overlap with {h}" for h in hits]
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
            if u.state != "ready" or "action" in u.step:
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
