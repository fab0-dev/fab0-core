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
- `done`: the commits its latest attempt recorded are in the branch it merges into; an attempt that committed
  nothing is done when its gate passes; for an action, its check passes.

A step with a `human` gate whose outputs are all store artifacts, and which writes no repository, is reviewed in
the runs repository (D80): its review pull request carries readable copies of its outputs. Merging it is the
approval only when the person who merged holds the gate's owner role, and only for the latest attempt's outputs.
A person's edit on the review branch becomes an attempt of that person (`apply` records it).

Actions (the pipeline's and its blocks' `onStart` and `onEnd`, and action steps) are units too. An action is
done when its check passes: its result is a fact in git or the tracker, so it writes no ledger entry (D37).

Branches (D10, D14): in `run` branching every unit works on its own branch and merges into its parent: the
run branch, or the branch of the gated block around it. In `flat` branching the parent is the base branch.
A unit that writes no repository still works on a branch of the home repo, to read its parent's state there.
Every unit's ledger is in the run store (D74).

The tracker's relations are dependencies too (P11, D49). A foreach element's item is the sub-issue
`tracker.child.sync` made for it; once it exists, its blocked-by relations replace the element's `after`.
Blocked-by relations of the run's item hold back the whole run, and its other sub-issues hold back its end.

The tracker is read once per evaluation, and what is independent is read at the same time. While `evaluate` walks
the pipeline, a scout walks it too and settles a copy of each unit, on threads, as soon as the units before it
are done: what that reads is its inputs' pull requests and its required checks. A unit that the evaluation
doesn't reach (something before it isn't done) is not settled. The walk itself stays sequential, so the units,
their states and their order are what they were; it finds those answers already read, or waits on the same read,
since `Memo` shares one read between its callers. The scout only reads, and a failure it meets is the
evaluation's to meet again.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .actions import check, element_items
from .artifacts import input_hashes, store_name
from .claims import EVERYTHING, overlap
from .git import Repo
from .hashing import sha as content_sha
from .hashing import sha_json
from .ledger import ATTEMPT, attempts, entry_name, gate_attempts, seconds_between, took
from .ledger import check as check_entry
from .store import open_store
from .templates import Unresolved
from .tracker import MAX_IN_FLIGHT
from .wiring import EXECUTORS, Wiring, base_allowed, fmt_of

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
    pr_repo: str = "home"  # where its pull request is: the first repository it writes
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

    def duration(self) -> int | None:
        """Whole seconds over every recorded attempt and gate decision of the unit, from the ledger's times.

        None when nothing is recorded, or when any of those entries has no times (written before they existed)."""
        entries = [*self.history, *self.gates.values()]
        if not entries or not all(e.get("started") and e.get("ended") for e in entries):
            return None
        return sum(seconds_between(e["started"], e["ended"]) for e in entries)

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
        seconds = self.duration()
        if seconds is not None:
            out["duration"] = seconds
            out["took"] = took(seconds)
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
        self.store = open_store(project, wiring.root)
        self._facts: dict[str, dict] | None = None
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

    def facts(self) -> dict[str, dict]:
        """The run's ledger, from the local checkout of the store, each fact checked: read once per evaluation."""
        if self._facts is None:
            found = self.store.facts(self.run) if self.store else {}
            self._facts = {n: check_entry(f, f"{self.run}/{n}") for n, f in found.items()}
        return self._facts

    def recorded(self) -> bool:
        """The run has a ledger entry."""
        return bool(self.facts())

    def pending_dir(self) -> Path:
        return self.home.common_dir() / "fab0" / "pending" / self.run  # shared by every checkout of home

    def owner(self, unit: Unit) -> str | None:
        """The role whose turn a unit is: its human gate's owner, the step's, or its repository's."""
        human = unit.gate.get("human") if isinstance(unit.gate, dict) else None
        if isinstance(human, dict) and human.get("owner"):
            return human["owner"]
        if unit.step and unit.step.get("owner") not in (None, "repo-owner"):
            return unit.step["owner"]
        return self.w.project["repos"].get(unit.pr_repo, {}).get("owner")

    # ------------------------------------------------------------ review pull requests (D80)

    def store_github(self) -> str | None:
        """The runs repository on GitHub, where review pull requests are."""
        return self.w.project.get("store", {}).get("github")

    def store_reviewed(self, unit: Unit) -> bool:
        """A person reviews the unit's work in the runs repository: a step with a human gate decided by a pull
        request, whose outputs are all store artifacts, and which writes no repository."""
        step, gate = unit.step, unit.gate
        if self.store is None or not step or "action" in step or step.get("repos", {}).get("write"):
            return False
        human = gate == "human" or isinstance(gate, dict) and "human" in gate
        via = gate["human"].get("via") if isinstance(gate, dict) and isinstance(gate.get("human"), dict) else None
        outputs = step.get("outputs", [])
        return (
            human and via != "label" and bool(outputs) and all(self.w.artifacts[n]["kind"] == "store" for n in outputs)
        )

    def review_branch(self, unit: Unit) -> str:
        return self.store.review_branch(self.run, unit.id)

    def review_pulls(self, unit: Unit) -> list[dict]:
        """The unit's review pull requests, oldest first."""
        github = self.store_github()
        if github is None or self.tracker is None:
            return []
        return self.tracker.pulls(github, self.review_branch(unit), "main")

    def review_copies(self, unit: Unit) -> dict[str, str]:
        """The review folder's files as the latest attempt has them, by hash."""
        outputs = (unit.latest or {}).get("outputs", {})
        return {store_name(n, self.w.artifacts[n]): h for n, h in outputs.items() if n in unit.step.get("outputs", [])}

    def holds_latest(self, unit: Unit, at: str | None = None) -> bool:
        """The review folder holds the latest attempt's outputs: at commit `at`, else on the store's main."""
        found = self.store.review_files(self.run, unit.id, at)
        return {n: content_sha(data) for n, data in found.items()} == self.review_copies(unit)

    def owners(self, unit: Unit) -> list[str] | None:
        """The logins of the gate's owner role; None when the unit has no owner role, so no merge is refused."""
        role = self.owner(unit)
        return None if role is None else list(self.w.people.get(role, {}).get("github", []))

    def review_state(self, unit: Unit) -> tuple[str, dict | None]:
        """Done when the latest merged review pull request brought the latest attempt's outputs into main, and an
        owner merged it, or approved it after someone else did. A merge by anyone else is reported, and waits."""
        merged = [p for p in self.review_pulls(unit) if p["state"] == "merged"]
        if not merged or not self.holds_latest(unit):
            return "waiting", {"gate": "human"}
        last, owners = merged[-1], self.owners(unit)
        approvals = [
            r["user"] for r in self.tracker.reviews(self.store_github(), last["number"]) if r["state"] == "APPROVED"
        ]
        if owners is None or last.get("merged_by") in owners or set(approvals) & set(owners):
            return "done", None
        who = last.get("merged_by") or "someone"
        return "waiting", {"gate": "human", "merged": f"by {who}, who isn't {self.owner(unit)}"}

    def review_edit(self, unit: Unit) -> dict | None:
        """A person's commit on the unit's review branch that changed its outputs and isn't recorded yet: the
        commit, its author and the files. fab0's own commits, and the commits recorded as edits, aren't."""
        if not self.store_reviewed(unit) or unit.latest is None:
            return None
        head = self.store.review_head(self.run, unit.id)
        if (
            head is None
            or self.store.by_fab0(head)
            or any((e.get("edited") or {}).get("commit") == head for e in unit.history)
        ):
            return None
        if self.holds_latest(unit, head):
            return None
        pulls = self.review_pulls(unit)
        author = pulls[-1].get("author") if pulls and pulls[-1].get("head") in (None, head) else None
        out = {
            "commit": head,
            "author": author or self.store.author(head),
            "files": self.store.review_files(self.run, unit.id, head),
        }
        if pulls:
            out["pr"] = f"{self.store_github()}#{pulls[-1]['number']}"
        return out

    # ------------------------------------------------------------ the walk

    def evaluate(self) -> dict:
        """Every unit and its state; and the run's: `units` while work is left, `waiting` for the merge of the
        run's pull request, `done`. The tracker reads it needs are started ahead, on threads (see the top)."""
        if self.tracker is None:
            return self.walk_run()
        pool = ThreadPoolExecutor(MAX_IN_FLIGHT + 1, thread_name_prefix="fab0-read")  # the scout, and four reads
        try:
            pool.submit(quietly, self.item)  # the run's item is read whatever the pipeline is
            pool.submit(quietly, Scout(self, pool).scout)
            return self.walk_run()
        finally:  # what was started and isn't needed is dropped; what is under way is awaited
            pool.shutdown(wait=True, cancel_futures=True)

    def gather(self, calls: list) -> list:
        """The results of independent calls, in the order given, made at the same time. Every one is made; the
        first failure in the order given is raised, as a loop would have raised it. The tracker adapter lets at
        most `MAX_IN_FLIGHT` requests out at once, whatever the number of threads."""
        if len(calls) < 2:
            return [call() for call in calls]
        with ThreadPoolExecutor(min(len(calls), MAX_IN_FLIGHT)) as pool:
            futures = [pool.submit(call) for call in calls]
        return [f.result() for f in futures]

    def warm(self, unit: Unit) -> bool:
        """Settles a copy of a unit, which reads what `state` reads, and says whether it is done: for the scout."""
        self.load_latest(unit)
        self.settle(unit)
        return unit.state == "done"

    def walk_run(self) -> dict:
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
        if block.auto is not None:  # its gate decisions stay on record, whatever its state: they count in its duration
            self.load_latest(block)
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
        committed = [u for u in children if (u.latest or {}).get("heads")]
        if committed and all(self.reached(u, ctx.parent) for u in committed):
            block.state, block.reason = "done", None
            return True
        block.state = "waiting"
        if block.auto is None:
            block.reason = {"gate": self.gate_kind(gate)}
            return False
        # a block's gate judges its units as they are now; a unit that moves on asks for a new decision
        block.gate_inputs = sha_json({u.id: u.attempt for u in children})
        latest = block.gates.get(max(block.gates)) if block.gates else None
        if latest is None or latest.get("inputs", {}).get("units") != block.gate_inputs:
            block.attempt = max(block.gates, default=0) + 1
            block.reason = {"gate": self.gate_running(block)}
        elif latest["gate"]["decision"] == "approve":
            block.attempt = max(block.gates)
            if not committed:  # nothing to merge: the gate is the decision
                block.state, block.reason = "done", None
                return True
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

    def latest_object(self, name: str, ctx: Ctx | None) -> str | None:
        """The hash of a store artifact's latest version: the output of the most recent attempt that wrote it and
        passed its checks. A foreach unit reads its own element's, else the run's (one written outside a foreach)."""
        found = [
            f
            for n, f in self.facts().items()
            if ATTEMPT.match(n) and name in f.get("outputs", {}) and f.get("verify", {}).get(name) == "pass"
        ]
        element = ctx.element["id"] if ctx is not None and ctx.element else None
        mine = [f for f in found if f.get("unit") == element] or [f for f in found if not f.get("unit")]
        if not mine:
            return None
        return max(mine, key=lambda f: (f.get("ended", ""), f["attempt"]))["outputs"][name]

    def read_artifact(self, name: str, ctx: Ctx, parse: bool = True) -> tuple[object, dict | None]:
        art = self.w.artifacts[name]
        if art["kind"] == "store":
            digest = self.latest_object(name, ctx)
            raw = self.store.get(self.run, digest) if digest else None
            if raw is None:
                return None, {"data": f"{name} is missing: no attempt has written it yet"}
        else:
            repo, path = self.artifact_location(name, ctx)
            ref = repo.fresh(self.parent_in(repo, ctx.parent)) or repo.fresh(repo.base)
            raw = repo.read_file(ref, path) if ref else None
            if raw is None:
                return None, {"data": f"{name} is missing: {repo.name}:{path}"}
        fmt = fmt_of(art)
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
        unit.pr_repo = unit.write[0] if unit.write else "home"
        unit.branch = self.branch_name(uid)
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

    def load_latest(self, unit: Unit) -> None:
        facts = self.facts()
        for n in gate_attempts(facts, unit.id):
            unit.gates[n] = facts[entry_name(unit.id, n, gate=True)]
        found = attempts(facts, unit.id)
        if not found:
            unit.attempt = 1
            return
        unit.attempt = found[-1]
        unit.history = [facts[entry_name(unit.id, n)] for n in found]
        unit.latest = unit.history[-1]

    def gate_running(self, unit: Unit) -> str:
        """`running` while a gate hand-out waits for its record, else `gate`: one is due."""
        return "running" if (self.pending_dir() / f"{unit.id}-gate.json").is_file() else "gate"

    def reached(self, unit: Unit, branch: str | None) -> bool:
        """The commits the unit's latest attempt recorded are in `branch`, in every repository it committed to.
        False when it committed nothing: then its gate decides (D34)."""
        heads = (unit.latest or {}).get("heads") or {}
        if not heads:
            return False
        for name, sha in heads.items():
            repo = self.repos[name]
            ref = repo.fresh(self.parent_in(repo, branch))
            if not ref or not repo.ref_exists(sha) or not repo.is_ancestor(sha, ref):
                return False
        return True

    def pull_merged(self, unit: Unit) -> bool:
        """GitHub reports the unit's pull request merged at the commits its latest attempt recorded, in every
        repository it committed to, though they aren't in its parent: a person squashed or rebased it. For a unit
        reviewed in the runs repository, its review pull request merged as the gate asks."""
        if self.store_reviewed(unit):
            return unit.latest is not None and self.review_state(unit)[0] == "done"
        heads = (unit.latest or {}).get("heads") or {}
        if not heads or not unit.branch or self.tracker is None:
            return False
        for name in heads:
            repo = self.repos[name]
            pr = self.tracker.pull(repo.github, unit.branch, self.parent_in(repo, unit.ctx.parent))
            if pr is None or pr["state"] != "merged" or pr.get("head") != heads[name]:
                return False
        return True

    def gate_kind(self, gate) -> str:
        if isinstance(gate, str):
            return "merge" if gate == "none" else gate
        return "human" if "human" in gate else "gate"

    def state(self, unit: Unit) -> None:
        self.load_latest(unit)
        self.settle(unit)

    def settle(self, unit: Unit) -> None:
        """The state of a unit whose records are loaded."""
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
        if self.reached(unit, unit.ctx.parent):
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
        if self.store_reviewed(unit):
            unit.state, unit.reason = self.review_state(unit)
            return
        labelled = self.labelled(unit)
        if not entry.get("heads"):  # nothing to merge: the gate is the decision
            human = (unit.gate == "human" or isinstance(unit.gate, dict) and "human" in unit.gate) and not labelled
            unit.state, unit.reason = ("waiting", {"gate": "human"}) if human else ("done", None)
            return
        if self.pull_merged(unit):
            unit.state, unit.reason = "done", None  # a person squashed or rebased it: the merge decided
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
        for name in dict.fromkeys(unit.write or [unit.pr_repo]):
            repo = self.repos[name]
            base, head = repo.fresh(self.parent_in(repo, unit.ctx.parent)), repo.fresh(unit.branch)
            if base and head and not repo.is_ancestor(head, base):
                files = repo.merge_conflicts(base, head)
                if files:
                    found[name] = files
        return found

    def failing_checks(self, unit: Unit) -> dict[str, list[str]]:
        def failing(name: str) -> tuple[str, list[str]] | None:
            repo = self.repos[name]
            pr = self.tracker.pull(repo.github, unit.branch, self.parent_in(repo, unit.ctx.parent))
            if pr is not None and pr["state"] == "open":
                state, failed = self.tracker.checks(repo.github, pr["number"])
                if state == "fail":
                    return f"{repo.github}#{pr['number']}", failed
            return None

        names = dict.fromkeys(unit.write or [unit.pr_repo])  # one repository's checks don't wait on another's
        return dict(f for f in self.gather([lambda n=n: failing(n) for n in names]) if f)

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
        if stale and (self.reached(unit, unit.ctx.parent) or self.pull_merged(unit)):
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
        if self.w.artifacts[name]["kind"] == "store":
            return self.store.has_object(self.run, unit.latest["outputs"][name]) if unit.latest else False
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
            if self.w.artifacts[name]["kind"] not in ("file", "store"):
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
        """The claims of other open runs: (run, unit, {repo: [globs]}) from the latest attempt of each of their
        units whose commits aren't in their repository's base yet (D47), read from the run store."""
        if self.store is None:
            return []
        found = []
        for other in self.store.runs():
            if other == self.run:
                continue
            latest: dict[str, tuple[int, dict]] = {}
            for name, fact in self.store.facts(other).items():
                m = ATTEMPT.match(name)
                if m and int(m["attempt"]) > latest.get(m["unit"], (0, None))[0]:
                    latest[m["unit"]] = (int(m["attempt"]), fact)
            for unit, (_, fact) in latest.items():
                heads = (fact or {}).get("heads") or {}
                if not heads or all(self.in_base(name, sha) for name, sha in heads.items()):
                    continue
                claims: dict[str, list[str]] = {}
                for item in fact.get("claims") or []:
                    name, _, glob = item.partition(":")
                    claims.setdefault(name, []).append(glob)
                if claims:
                    found.append((other, unit, claims))
        return found

    def in_base(self, name: str, sha: str) -> bool:
        repo = self.repos.get(name)
        if repo is None or not repo.exists():
            return True  # a repository this project doesn't have can't conflict here
        base = repo.fresh(repo.base)
        return bool(base) and repo.ref_exists(sha) and repo.is_ancestor(sha, base)

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


def quietly(call, *args) -> None:
    """A speculative read: a failure of it is the evaluation's to meet when it reads the same thing."""
    try:
        call(*args)
    except Exception:
        pass


class Scout(Run):
    """A walk of a run's steps that decides nothing: it assumes every action it passes is done, and for each unit
    starts, on the pool, what settling it would read. It has no tracker, so it never reads itself, and a unit that
    needs the run's item (a tracker condition, a foreach's sub-issues) is walked as the evaluation would walk it
    without one.

    The evaluation settles a unit only when everything before it is done. So a unit's reads wait until the units
    before it have settled, and are dropped when one of them isn't done (a stale spec, a unit sent back for
    rework, a unit waiting on its gate): the evaluation never reaches the unit. Elements of one foreach don't wait
    for one another, except for the ones their `after` names. What it starts is read once: the evaluation's own
    read of the same thing waits for it."""

    def __init__(self, run: Run, pool: ThreadPoolExecutor):
        super().__init__(run.w, run.name, run.run, run.actor, None)
        self.real, self.pool = run, pool
        self.warmed: list[tuple[Unit, Future]] = []

    def scout(self) -> None:
        self.walk(self.p["steps"], Ctx(parent=self.run_branch), True, None)

    def action_state(self, unit: Unit) -> bool:
        unit.state, unit.reason = "done", None
        return True

    def state(self, unit: Unit) -> None:
        unit.state, unit.reason = "done", None
        # a copy: settling it sets its state, attempt and history, which are the evaluation's to set
        copy = replace(unit, history=[], gates={})
        done: Future = Future()  # whether the unit is done, once it has been settled
        before = [f for earlier, f in self.warmed if self.comes_before(earlier, copy)]
        self.warmed.append((copy, done))
        self.after(before, lambda: self.warm_into(copy, done), done)

    @staticmethod
    def comes_before(earlier: Unit, unit: Unit) -> bool:
        """The evaluation settles `unit` only once `earlier` is done. It doesn't wait for a unit of another element
        of the same foreach, unless that element is one this element is `after`."""
        a, b = earlier.ctx.element, unit.ctx.element
        return a is None or b is None or a["id"] == b["id"] or a["id"] in b["after"]

    def after(self, before: list[Future], task, done: Future) -> None:
        """Starts `task` on the pool once every future in `before` is done with a true result; if one isn't, the
        evaluation doesn't reach the unit and `done` is false. What waits on one future starts in the order it was
        asked for, so the pool reads for the first units first, as the evaluation does."""
        lock, waiting = threading.Lock(), [len(before)]

        def settled(f: Future | None) -> None:
            with lock:
                if waiting[0] < 0:  # already decided
                    return
                if f is not None:
                    waiting[0] -= 1
                    if f.cancelled() or f.exception() or not f.result():
                        waiting[0] = -1  # one that isn't done: never start
                        done.set_result(False)
                        return
                if waiting[0] != 0:
                    return
                waiting[0] = -1
            try:
                self.pool.submit(task)  # raises once evaluate is over
            except RuntimeError:
                pass

        if not before:
            settled(None)
        for f in before:
            f.add_done_callback(settled)

    def warm_into(self, unit: Unit, done: Future) -> None:
        try:
            done.set_result(self.real.warm(unit))
        except Exception:  # the evaluation meets it again, if it reaches the unit
            done.set_result(False)
