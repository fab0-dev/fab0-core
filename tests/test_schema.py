"""Schema 2: the three fixture factories validate, and together they use every building block."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "plugins" / "fab0" / "schemas"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACTORIES = sorted(p.name for p in FIXTURES.iterdir() if (p / ".fab0").is_dir())
EXECUTORS = ("agent", "action", "command", "tool")


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.json").read_text())


def errors(doc, name: str) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema(name))
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(doc)]


def load(path: Path):
    return yaml.safe_load(path.read_text())


def files(factory: str, kind: str) -> dict[str, dict]:
    return {p.stem: load(p) for p in sorted((FIXTURES / factory / ".fab0" / kind).glob("*.yaml"))}


def nodes(tree: list):
    """Every node of a steps tree, depth first, with the branch lists of `parallel` flattened."""
    for node in tree:
        yield node
        if isinstance(node, dict):
            for key in ("steps", "else"):
                yield from nodes(node.get(key, []))
            for branch in node.get("parallel", {}).values():
                yield from nodes(branch)


def inline_steps(pipeline: dict) -> dict[str, dict]:
    found = {}
    for node in nodes(pipeline["steps"]):
        if isinstance(node, dict) and "step" in node and any(k in node for k in EXECUTORS):
            found[node["step"]] = {k: v for k, v in node.items() if k not in ("gate", "after")}
    return found


def test_the_schemas_are_valid_json_schemas():
    for path in SCHEMAS.glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))


@pytest.mark.parametrize("factory", FACTORIES)
def test_each_fixture_validates(factory):
    home = FIXTURES / factory
    problems = [f"fab0.yaml {e}" for e in errors(load(home / ".fab0" / "fab0.yaml"), "project")]
    for kind, name in (("pipelines", "pipeline"), ("steps", "step"), ("artifacts", "artifact")):
        for stem, doc in files(factory, kind).items():
            problems += [f"{kind}/{stem}.yaml {e}" for e in errors(doc, name)]
            assert doc[name] == stem, f"{kind}/{stem}.yaml names itself {doc[name]!r}"
    for pipeline in files(factory, "pipelines").values():
        for stem, doc in inline_steps(pipeline).items():
            problems += [f"inline step {stem} {e}" for e in errors(doc, "step")]
    assert not problems, "\n".join(problems)


def features() -> set[str]:
    """What the fixtures use, named the way REFACTOR.md's R1 exit check lists it."""
    seen = set()
    for factory in FACTORIES:
        project = load(FIXTURES / factory / ".fab0" / "fab0.yaml")
        steps = files(factory, "steps")
        seen |= {f"artifact:{a['kind']}" for a in files(factory, "artifacts").values()}
        for pipeline in files(factory, "pipelines").values():
            seen.add(f"trigger:{next(iter(pipeline['trigger']))}")
            default = project.get("git", {}).get("branching", "run")
            seen.add(f"branching:{pipeline.get('git', {}).get('branching', default)}")
            for step in {**steps, **inline_steps(pipeline)}.values():
                seen |= {f"executor:{k}" for k in EXECUTORS if k in step}
            for node in nodes(pipeline["steps"]):
                if isinstance(node, str):
                    seen.add("gate:none")
                    continue
                seen |= {f"operator:{k}" for k in ("foreach", "parallel", "when", "else", "after") if k in node}
                if "onStart" in node or "onEnd" in node:
                    seen.add("operator:block-lifecycle")
                gate = node.get("gate", "none")
                if isinstance(gate, str):
                    seen.add(f"gate:{gate}")
                elif "human" in gate:
                    seen.add(f"gate:human-{gate['human'].get('via', 'pr')}")
                else:
                    seen.add(f"gate:auto-{next(iter(gate['auto']))}")
                    seen |= {f"gate:merge-{gate.get('merge', 'human')}", f"gate:then-{gate.get('then', 'human')}"}
                    if "retry" in gate:
                        seen.add("gate:retry")
            if len(pipeline["steps"]) > 1:
                seen.add("operator:sequence")
    return seen


def test_the_fixtures_use_every_building_block():
    expected = {
        *(f"operator:{o}" for o in ("sequence", "foreach", "parallel", "when", "else", "after", "block-lifecycle")),
        *(f"gate:{g}" for g in ("none", "human-pr", "human-label", "auto-agent", "auto-command")),
        *(f"gate:{g}" for g in ("merge-auto", "merge-human", "retry", "then-human", "then-fail")),
        *(f"artifact:{k}" for k in ("file", "issue", "pr", "tool", "none")),
        *(f"executor:{e}" for e in EXECUTORS),
        "branching:run",
        "branching:flat",
        "trigger:issue",
        "trigger:manual",
    }
    assert expected - features() == set()


def story(path: str) -> dict:
    return load(FIXTURES / "story" / ".fab0" / path)


def changed(doc: dict, change) -> dict:
    doc = copy.deepcopy(doc)
    change(doc)
    return doc


@pytest.mark.parametrize(
    "doc, name, expected",
    [
        (changed(story("fab0.yaml"), lambda d: d.update(schema=1)), "project", "2 was expected"),
        (
            changed(story("fab0.yaml"), lambda d: d["git"].update(auto_merge={"into": ["run", "base"]})),
            "project",
            "'allow_base' is a required property",
        ),
        (changed(story("steps/spec.yaml"), lambda d: d.update(command="make")), "step", "is not valid under any"),
        (changed(story("steps/spec.yaml"), lambda d: d.pop("agent")), "step", "is not valid under any"),
        (changed(story("steps/spec.yaml"), lambda d: d.update(gate="human")), "step", "Additional properties"),
        (changed(story("artifacts/spec.yaml"), lambda d: d.pop("file")), "artifact", "'file' is a required"),
        (changed(story("artifacts/intent.yaml"), lambda d: d.update(keep="run")), "artifact", "Additional properties"),
        (changed(story("artifacts/tasks.yaml"), lambda d: d.pop("store")), "artifact", "'store' is a required"),
        (
            changed(story("artifacts/tasks.yaml"), lambda d: d.update(file={"repo": "home", "path": "t.yaml"})),
            "artifact",
            "should not be valid",
        ),
        (changed(story("pipelines/story.yaml"), lambda d: d.pop("onEnd")), "pipeline", "'onEnd' is a required"),
        (
            changed(story("pipelines/story.yaml"), lambda d: d["steps"][0].update(gate={"human": {}, "merge": "auto"})),
            "pipeline",
            "is not valid under any",
        ),
        (
            changed(story("pipelines/story.yaml"), lambda d: d["steps"][2]["when"].update(exists=True)),
            "pipeline",
            "is not valid under any",
        ),
        (
            changed(story("pipelines/story.yaml"), lambda d: d["onStart"].append("deploy.prod")),
            "pipeline",
            "is not valid under any",
        ),
    ],
)
def test_invalid_wiring_is_refused(doc, name, expected):
    found = errors(doc, name)
    assert found and any(expected in e for e in found), found
