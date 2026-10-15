"""A factory's `.fab0/` wiring (schema 2): loaded, validated against the core schemas, and cross-checked.

The schemas check each file on its own. This module checks what spans files: names resolve, references
point at the right kind of thing, steps read artifacts only after something writes them, gates and
actions fit the project's tracker and git policy, and what the engine doesn't run yet is refused.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .actions import FOREACH_ONLY, RUN_LEVEL
from .catalogue import ACTIONS
from .merge import merge_patch
from .schemas import check_schema_file, core_schema, schema_errors

HARNESS = "claude-code"
EXECUTORS = ("agent", "action", "command", "tool")
READ_ONLY_KINDS = ("issue", "pr", "tool")  # read by steps, never written by them
LISTABLE = ("yaml", "json")
DEFAULT_GIT = {"base": "main", "branching": "run", "auto_merge": {"into": ["run"]}}
PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
SCHEMA_1_HINT = (
    "this is schema 1, the fab0 0.5 wiring; this engine reads schema 2 (fab0 0.6). "
    "Rewrite .fab0/ for schema 2, or pin the plugin to fab0--v0.5.0"
)


def plugin_name(name: str) -> bool:
    return ":" in name


class Wiring:
    """The project's `.fab0/` files, loaded and checked. `errors` lists `{file, message}`; empty means valid."""

    def __init__(self, root: Path, profile: str | None = None):
        self.root = root
        self.errors: list[dict] = []
        self.project = self._load_project()
        self.artifacts: dict[str, dict] = {}
        self.steps: dict[str, dict] = {}
        self.pipelines: dict[str, dict] = {}
        if self.project is None:
            return
        self.artifacts = self._load_dir("artifacts", "artifact")
        self.steps = self._load_dir("steps", "step")
        self.pipelines = self._load_dir("pipelines", "pipeline")
        if not self.errors:
            self._check()
        if profile is not None and not self.errors:
            self._apply_profile(profile)

    # ------------------------------------------------------------ loading

    def error(self, where: str, message: str) -> None:
        self.errors.append({"file": where, "message": message})

    def _read(self, rel: str):
        where = f".fab0/{rel}"
        try:
            return yaml.safe_load((self.root / ".fab0" / rel).read_text())
        except yaml.YAMLError as exc:
            self.error(where, f"invalid YAML: {exc}")
        except OSError as exc:
            self.error(where, f"cannot read: {exc.strerror or exc}")
        return None

    def _validated(self, rel: str, data, schema: str):
        errors = schema_errors(data, core_schema(schema))
        for message in errors:
            self.error(f".fab0/{rel}", message)
        return None if errors else data

    def _load_project(self) -> dict | None:
        if not (self.root / ".fab0" / "fab0.yaml").is_file():
            self.error(".fab0/fab0.yaml", "missing")
            return None
        data = self._read("fab0.yaml")
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("schema") != 2:
            self.error(
                ".fab0/fab0.yaml",
                SCHEMA_1_HINT if isinstance(data, dict) and data.get("schema") == 1 else "schema: 2 is required",
            )
            return None
        if (self.root / ".fab0" / "stages").is_dir():
            self.error(".fab0/stages/", "stages/ is schema 1; schema 2 keeps steps in .fab0/steps/")
        return self._validated("fab0.yaml", data, "project")

    def _load_dir(self, sub: str, kind: str) -> dict:
        out = {}
        folder = self.root / ".fab0" / sub
        if not folder.is_dir():
            return out
        for path in sorted(folder.iterdir()):
            rel = f"{sub}/{path.name}"
            if path.suffix == ".yml":
                self.error(f".fab0/{rel}", "use the .yaml extension")
            if path.suffix != ".yaml":
                continue
            data = self._read(rel)
            if data is None or self._validated(rel, data, kind) is None:
                continue
            if data[kind] != path.stem:
                self.error(f".fab0/{rel}", f"{kind} '{data[kind]}' doesn't match the file name")
                continue
            out[path.stem] = data
        return out

    def _apply_profile(self, name: str) -> None:
        profiles = self.project.get("profiles", {})
        if name not in profiles:
            self.error(".fab0/fab0.yaml", f"no profile '{name}'")
            return
        self.project = merge_patch(self.project, profiles[name])

    # ------------------------------------------------------------ the project

    @property
    def classes(self) -> set[str]:
        return set(self.project["models"][HARNESS])

    @property
    def people(self) -> dict:
        return self.project.get("people", {})

    @property
    def has_tracker(self) -> bool:
        return "tracker" in self.project

    def git_policy(self, pipeline: dict | None = None) -> dict:
        """The project's git policy with defaults filled in, and a pipeline's overrides applied."""
        policy = merge_patch(DEFAULT_GIT, self.project.get("git", {}))
        if pipeline:
            policy = merge_patch(policy, pipeline.get("git", {}))
        return policy

    def _check(self) -> None:
        self._check_project()
        for name, art in self.artifacts.items():
            self._check_artifact(f".fab0/artifacts/{name}.yaml", art)
        for name, step in self.steps.items():
            self._check_step(f".fab0/steps/{name}.yaml", step)
        triggers: dict[str, str] = {}
        for name, pipeline in self.pipelines.items():
            Pipeline(self, name, pipeline).check(triggers if name in self.project["pipelines"] else {})

    def _check_project(self) -> None:
        p, f0 = self.project, ".fab0/fab0.yaml"
        keys: dict[str, str] = {}
        for name, repo in p["repos"].items():
            if "owner" in repo and repo["owner"] not in self.people:
                self.error(f0, f"repos.{name}.owner: no role '{repo['owner']}' in people")
            if "key" in repo:
                if repo["key"] in keys:
                    self.error(f0, f"repos.{name}.key: '{repo['key']}' is also the key of {keys[repo['key']]}")
                keys[repo["key"]] = name
        for name in p["pipelines"]:
            if name not in self.pipelines:
                self.error(f0, f"pipelines: no pipeline '{name}' in .fab0/pipelines/")
        for name, patch in p.get("profiles", {}).items():
            for message in schema_errors(merge_patch(p, patch), core_schema("project")):
                self.error(f0, f"profiles.{name} makes the file invalid: {message}")

    # ------------------------------------------------------------ artifacts and steps

    def _check_repo(self, where: str, field: str, repo: str, allow: tuple = ()) -> None:
        if repo in self.project["repos"] or repo in allow or repo.startswith("unit."):
            return
        self.error(where, f"{field}: no repo '{repo}' in fab0.yaml")

    def _check_artifact(self, where: str, art: dict) -> None:
        kind = art["kind"]
        if kind == "file":
            self._check_repo(where, "file.repo", art["file"]["repo"])
        if kind == "issue" and not self.has_tracker:
            self.error(where, "kind issue needs a tracker in fab0.yaml")
        fmt = art.get("file", {}).get("format")
        if "schema" in art:
            if fmt not in LISTABLE:
                self.error(where, "schema: only yaml and json files have a schema")
            elif problem := check_schema_file(art["schema"], self.root):
                self.error(where, f"schema: {problem}")
        if "elements" in art and fmt not in LISTABLE:
            self.error(where, "elements: only yaml and json files are lists the engine reads")
        for check in art.get("verify", []):
            if "sections" in check and fmt != "markdown":
                self.error(where, "verify: sections needs a markdown file")

    def check_agent(self, where: str, field: str, agent: str) -> None:
        if not plugin_name(agent) and not (self.root / ".claude" / "agents" / f"{agent}.md").is_file():
            self.error(where, f"{field}: '{agent}' is neither plugin:name nor a file in .claude/agents/")

    def check_skills(self, where: str, field: str, skills: list) -> None:
        for skill in skills:
            if not plugin_name(skill) and not (self.root / ".claude" / "skills" / skill / "SKILL.md").is_file():
                self.error(where, f"{field}: '{skill}' is neither plugin:name nor a skill in .claude/skills/")

    def check_model(self, where: str, field: str, model: str) -> None:
        if model not in self.classes:
            self.error(where, f"{field}: no class '{model}' in models.{HARNESS}")

    def check_action(self, where: str, field: str, name: str) -> None:
        if name not in ACTIONS:
            self.error(where, f"{field}: no action '{name}' in fab0's catalogue")
        elif name.startswith("tracker.") and not self.has_tracker:
            self.error(where, f"{field}: '{name}' needs a tracker in fab0.yaml")

    def _check_step(self, where: str, step: dict) -> None:
        executor = next(k for k in EXECUTORS if k in step)
        only = {"agent": ("model", "skills", "instructions"), "action": (), "command": (), "tool": ()}
        for key in ("model", "skills", "instructions"):
            if key in step and key not in only[executor]:
                self.error(where, f"{key}: only agent steps take '{key}'")
        if "with" in step and executor not in ("action", "tool"):
            self.error(where, "with: only action and tool steps take arguments")
        if "return" in step and executor not in ("agent", "tool"):
            self.error(where, "return: only agent and tool steps return")
        if executor == "agent":
            self.check_agent(where, "agent", step["agent"])
            if "model" not in step:
                self.error(where, "an agent step needs 'model', a class from models")
            else:
                self.check_model(where, "model", step["model"])
            self.check_skills(where, "skills", step.get("skills", []))
        if executor == "action":
            self.check_action(where, "action", step["action"])
            if step["action"] in RUN_LEVEL + FOREACH_ONLY:
                self.error(
                    where,
                    f"action: {step['action']} can't be a step; it belongs in a pipeline's or a foreach's lifecycle",
                )
        if problem := step.get("return", {}).get("schema") and check_schema_file(step["return"]["schema"], self.root):
            self.error(where, f"return.schema: {problem}")
        for io in ("inputs", "outputs"):
            for ref in step.get(io, []):
                if ref == "unit" or ref.startswith("unit."):
                    if io == "outputs":
                        self.error(where, "outputs: a step can't write the unit")
                elif ref not in self.artifacts:
                    self.error(where, f"{io}: no artifact '{ref}'")
                elif io == "outputs" and self.artifacts[ref]["kind"] in READ_ONLY_KINDS:
                    self.error(where, f"outputs: '{ref}' is {self.artifacts[ref]['kind']}, which steps only read")
        for io in ("write", "read"):
            for repo in step.get("repos", {}).get(io, []):
                self._check_repo(where, f"repos.{io}", repo, ("all",) if io == "read" else ())
        owner = step.get("owner")
        if owner and owner != "repo-owner" and owner not in self.people:
            self.error(where, f"owner: no role '{owner}' in people")

    def uses_unit(self, step: dict) -> list[str]:
        """The fields of a step that only make sense inside `foreach`."""
        found = []
        refs = step.get("inputs", []) + step.get("repos", {}).get("write", []) + step.get("repos", {}).get("read", [])
        if any(r == "unit" or r.startswith("unit.") for r in refs):
            found.append("a unit reference")
        if isinstance(step.get("paths"), str):
            found.append("paths from the unit")
        if step.get("owner") == "repo-owner" and len(step.get("repos", {}).get("write", [])) != 1:
            found.append("owner repo-owner")
        for name in step.get("inputs", []) + step.get("outputs", []):
            art = self.artifacts.get(name, {})
            # a unit's own pull request exists for every step; a unit's tracker item only for a foreach element
            if "{unit}" in art.get("file", {}).get("path", "") or art.get("issue", {}).get("of") == "unit":
                found.append(f"artifact '{name}', which is per unit")
        return found

    def writes_files(self, step: dict) -> bool:
        outputs = [self.artifacts.get(o, {}) for o in step.get("outputs", [])]
        return bool(step.get("repos", {}).get("write")) or any(a.get("kind") == "file" for a in outputs)


def base_allowed(policy: dict) -> bool:
    """Merging into the base branch needs both `into: [base]` and `allow_base: true`."""
    merge = policy.get("auto_merge", {})
    return "base" in merge.get("into", []) and merge.get("allow_base") is True


class Pipeline:
    """One pipeline's tree, walked once to check what spans its nodes."""

    def __init__(self, wiring: Wiring, name: str, data: dict):
        self.w = wiring
        self.name = name
        self.data = data
        self.where = f".fab0/pipelines/{name}.yaml"
        self.policy = wiring.git_policy(data)
        self.steps: dict[str, dict] = {}  # every step this pipeline runs, files and inline, by name
        self.gated: set[str] = set()  # steps with an auto gate, for `verdict` conditions
        self.ids: set[str] = set()

    def error(self, message: str) -> None:
        self.w.error(self.where, message)

    def check(self, triggers: dict) -> None:
        d = self.data
        for key in ("inputs", "outputs"):
            if key in d:
                self.error(f"{key}: pipeline inputs and outputs (call) are not supported yet")
        self._check_trigger(d["trigger"], triggers)
        if "auto_merge" in d.get("git", {}):
            for message in schema_errors(
                d["git"]["auto_merge"], core_schema("project")["$defs"]["git"]["properties"]["auto_merge"]
            ):
                self.error(f"git.auto_merge: {message}")
        self._check_actions("onStart", d["onStart"])
        names = [a if isinstance(a, str) else next(iter(a)) for a in d["onStart"]]
        if self.policy["branching"] == "run" and "git.run-branch" not in names:
            self.error("onStart: run branching needs git.run-branch, which creates the run branch")
        self._walk(d["steps"], "steps", available=self._read_only(), in_foreach=False, in_parallel=None)
        self._check_actions("onEnd", d["onEnd"])
        if not any(self.w.writes_files(s) for s in self.steps.values()) and "branch" not in self.w.project.get(
            "ledger", {}
        ):
            self.error("no step writes files, so the run opens no pull request: set ledger.branch in fab0.yaml")

    def _read_only(self) -> set[str]:
        return {n for n, a in self.w.artifacts.items() if a["kind"] in READ_ONLY_KINDS}

    def _check_trigger(self, trigger: dict, triggers: dict) -> None:
        if "issue" in trigger:
            if not self.w.has_tracker:
                self.error("trigger: an issue trigger needs a tracker in fab0.yaml")
            for key, value in trigger["issue"].items():
                if f"{key}:{value}" in triggers:
                    self.error(f"trigger: issue {key} '{value}' also starts pipeline {triggers[f'{key}:{value}']}")
                triggers[f"{key}:{value}"] = self.name
        elif pattern := trigger["manual"].get("id"):
            try:
                re.compile(pattern)
            except re.error as exc:
                self.error(f"trigger.manual.id: not a regular expression: {exc}")

    def _check_actions(self, at: str, actions: list, place: str = "pipeline") -> None:
        """`place`: `pipeline` (its onStart and onEnd), `foreach` or `parallel` (a block's), or `step`."""
        for i, action in enumerate(actions):
            name = action if isinstance(action, str) else next(iter(action))
            self.w.check_action(self.where, f"{at}[{i}]", name)
            if name in RUN_LEVEL and place != "pipeline":
                self.error(f"{at}[{i}]: {name} acts on the whole run; it belongs in the pipeline's onStart or onEnd")
            if name == "pr.merge" and not base_allowed(self.policy):
                self.error(f"{at}[{i}]: pr.merge merges into the base branch, which git.auto_merge doesn't allow")
            if name == "tracker.child.sync" and not (place == "foreach" and at.endswith("onStart")):
                self.error(f"{at}[{i}]: {name} makes an item per element; it belongs in a foreach's onStart")
            if name == "tracker.child.close" and not (place == "foreach" and at.endswith("onEnd")):
                self.error(f"{at}[{i}]: {name} closes the elements' items; it belongs in a foreach's onEnd")
            if not isinstance(action, str):
                self._check_templates(f"{at}[{i}]", action[name])

    def _check_templates(self, at: str, value) -> None:
        if isinstance(value, dict):
            for v in value.values():
                self._check_templates(at, v)
        elif isinstance(value, list):
            for v in value:
                self._check_templates(at, v)
        elif isinstance(value, str):
            for ref in PLACEHOLDER.findall(value):
                head = ref.split(".")[0]
                if head in ("run", "unit"):
                    continue
                step = self.steps.get(head)
                if step is None:
                    self.error(f"{at}: '{{{ref}}}' names no earlier step")
                elif "." not in ref or not ({"agent", "tool"} & set(step)):
                    self.error(f"{at}: '{{{ref}}}' needs a field of an agent or tool step's return data")

    # ------------------------------------------------------------ the tree

    def _step(self, node, at: str) -> dict | None:
        name = node if isinstance(node, str) else node["step"]
        inline = isinstance(node, dict) and any(k in node for k in EXECUTORS)
        if inline:
            if name in self.w.steps:
                self.error(f"{at}: step '{name}' is defined inline and in .fab0/steps/")
                return None
            data = {k: v for k, v in node.items() if k not in ("gate", "after")}
            errors = schema_errors(data, core_schema("step"))
            for message in errors:
                self.error(f"{at}: {message}")
            if errors:
                return None
            self.w._check_step(f"{self.where} {at}", data)
            return data
        if isinstance(node, dict) and set(node) - {"step", "gate", "after"}:
            self.error(f"{at}: step '{name}' has fields but no executor; define it inline or in .fab0/steps/")
            return None
        if name not in self.w.steps:
            self.error(f"{at}: no step '{name}'")
            return None
        return self.w.steps[name]

    def _walk(self, nodes: list, at: str, available: set, in_foreach: bool, in_parallel: dict | None) -> set:
        """Checks `nodes` in order; returns the artifacts available after them."""
        for i, node in enumerate(nodes):
            here = f"{at}[{i}]"
            if isinstance(node, dict):
                if "id" in node:
                    if node["id"] in self.ids:
                        self.error(f"{here}: id '{node['id']}' is used twice")
                    self.ids.add(node["id"])
                if "after" in node and in_parallel is None:
                    self.error(f"{here}: after only orders the branches of a parallel block")
                if "gate" in node:
                    self._check_gate(here, node["gate"], node)
                for key in ("onStart", "onEnd"):
                    if key in node:
                        kind = "foreach" if "foreach" in node else "parallel"
                        self._check_actions(f"{here}.{key}", node[key], kind)
            if isinstance(node, str) or "step" in node:
                available = self._walk_step(node, here, available, in_foreach)
            elif "foreach" in node:
                available = self._walk_foreach(node, here, available, in_foreach)
            elif "parallel" in node:
                available = self._walk_parallel(node, here, available, in_foreach)
            elif "when" in node:
                self._check_condition(f"{here}.when", node["when"])
                after = self._walk(node["steps"], f"{here}.steps", set(available), in_foreach, None)
                if "else" in node:
                    after |= self._walk(node["else"], f"{here}.else", set(available), in_foreach, None)
                available = after
            elif "call" in node:
                self.error(f"{here}: call (nested pipelines) is not supported yet")
        return available

    def _walk_step(self, node, at: str, available: set, in_foreach: bool) -> set:
        step = self._step(node, at)
        if step is None:
            return available
        name = step["step"]
        if name in self.steps:
            self.error(f"{at}: step '{name}' appears twice; its ledger entries would collide")
        self.steps[name] = step
        if not in_foreach:
            for what in self.w.uses_unit(step):
                self.error(f"{at}: step '{name}' uses {what}, which needs foreach")
        for ref in step.get("inputs", []):
            art = ref.split(".")[0]
            if art in self.w.artifacts and art not in available and self._written_later(art):
                self.error(f"{at}: step '{name}' reads '{art}' before a step writes it")
        self._check_templates(f"{at}.with", step.get("with", {}))
        return available | set(step.get("outputs", []))

    def _written_later(self, art: str) -> bool:
        return any(art in s.get("outputs", []) for s in self._all_steps(self.data["steps"]))

    def _all_steps(self, nodes: list):
        for node in nodes:
            if isinstance(node, str):
                if node in self.w.steps:
                    yield self.w.steps[node]
            elif "step" in node:
                yield node if any(k in node for k in EXECUTORS) else self.w.steps.get(node["step"], {})
            else:
                for key in ("steps", "else"):
                    yield from self._all_steps(node.get(key, []))
                for branch in node.get("parallel", {}).values():
                    yield from self._all_steps(branch)

    def _walk_foreach(self, node: dict, at: str, available: set, in_foreach: bool) -> set:
        if in_foreach:
            self.error(f"{at}: foreach inside foreach is not supported yet")
        name = node["foreach"].split(".")[0]
        art = self.w.artifacts.get(name)
        if art is None:
            self.error(f"{at}: foreach: no artifact '{name}'")
        else:
            if art.get("file", {}).get("format") not in LISTABLE:
                self.error(f"{at}: foreach needs a yaml or json file")
            if name not in available and self._written_later(name):
                self.error(f"{at}: foreach reads '{name}' before a step writes it")
        return self._walk(node["steps"], f"{at}.steps", set(available), True, None)

    def _walk_parallel(self, node: dict, at: str, available: set, in_foreach: bool) -> set:
        branches = node["parallel"]
        names = {n: set() for n in branches}  # branch → the step names and ids in it
        for branch, nodes in branches.items():
            for s in self._all_steps(nodes):
                if s.get("step"):
                    names[branch].add(s["step"])
            names[branch] |= {n["id"] for n in nodes if isinstance(n, dict) and "id" in n}
        owner = {item: b for b, items in names.items() for item in items}
        deps: dict[str, set] = {b: set() for b in branches}
        for branch, nodes in branches.items():
            for node_ in nodes:
                for target in node_.get("after", []) if isinstance(node_, dict) else []:
                    dep = target if target in branches else owner.get(target)
                    if dep is None:
                        self.error(f"{at}.parallel.{branch}: after: '{target}' is no branch, step or id of this block")
                    elif dep == branch:
                        self.error(f"{at}.parallel.{branch}: after: '{target}' is in the same branch")
                    else:
                        deps[branch].add(dep)
        order = self._topological(deps, at)
        produced: dict[str, set] = {}
        for branch in order:
            start = set(available).union(*(produced.get(d, set()) for d in self._closure(deps, branch)))
            produced[branch] = self._walk(branches[branch], f"{at}.parallel.{branch}", start, in_foreach, deps)
        return set(available).union(*produced.values()) if produced else available

    def _topological(self, deps: dict, at: str) -> list[str]:
        order, state = [], {}

        def visit(b: str, path: list[str]) -> None:
            if state.get(b) == "done":
                return
            if state.get(b) == "open":
                self.error(f"{at}: after makes a cycle: {' -> '.join(path + [b])}")
                return
            state[b] = "open"
            for d in sorted(deps[b]):
                visit(d, path + [b])
            state[b] = "done"
            order.append(b)

        for b in deps:
            visit(b, [])
        return order

    @staticmethod
    def _closure(deps: dict, branch: str) -> set:
        seen, todo = set(), list(deps[branch])
        while todo:
            d = todo.pop()
            if d not in seen and d != branch:
                seen.add(d)
                todo.extend(deps[d])
        return seen

    # ------------------------------------------------------------ gates and conditions

    def _check_gate(self, at: str, gate, node: dict) -> None:
        if gate in ("none", "human"):
            return
        if "human" in gate:
            human = gate["human"]
            if "owner" in human and human["owner"] not in self.w.people:
                self.error(f"{at}.gate.human.owner: no role '{human['owner']}' in people")
            if human.get("via") == "label" and not self.w.has_tracker:
                self.error(f"{at}.gate.human: via label needs a tracker in fab0.yaml")
            return
        auto = gate["auto"]
        if "agent" in auto:
            self.w.check_agent(self.where, f"{at}.gate.auto.agent", auto["agent"])
            if "model" in auto:
                self.w.check_model(self.where, f"{at}.gate.auto.model", auto["model"])
            self.w.check_skills(self.where, f"{at}.gate.auto.skills", auto.get("skills", []))
        if gate.get("merge") == "auto":
            into = self.policy.get("auto_merge", {}).get("into", [])
            target = "run" if self.policy["branching"] == "run" else "base"
            if target not in into:
                self.error(
                    f"{at}.gate: merge auto would merge into the {target} branch, which git.auto_merge.into "
                    f"doesn't allow"
                )
        if "step" in node or isinstance(node, str):
            self.gated.add(node if isinstance(node, str) else node["step"])

    def _check_condition(self, at: str, cond: dict) -> None:
        for key in ("all", "any"):
            for i, c in enumerate(cond.get(key, [])):
                self._check_condition(f"{at}.{key}[{i}]", c)
        if "not" in cond:
            self._check_condition(f"{at}.not", cond["not"])
        if "artifact" in cond:
            name = cond["artifact"].split(".")[0]
            art = self.w.artifacts.get(name)
            if art is None:
                self.error(f"{at}: no artifact '{name}'")
            elif art["kind"] != "file":
                self.error(f"{at}: conditions read file artifacts only, and '{name}' is {art['kind']}")
            elif "section" in cond and art.get("file", {}).get("format") != "markdown":
                self.error(f"{at}: section needs a markdown file, and '{name}' isn't one")
        if "verdict" in cond and cond["verdict"] not in self.gated:
            self.error(f"{at}: verdict: '{cond['verdict']}' is no earlier step with an auto gate")
        if "tracker" in cond and not self.w.has_tracker:
            self.error(f"{at}: tracker conditions need a tracker in fab0.yaml")
