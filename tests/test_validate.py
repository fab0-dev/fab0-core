"""fab0-resolve validate: schema 2 wiring is checked across files, and schema 1 is refused."""

import pytest
from conftest import FACTORIES


@pytest.mark.parametrize("name", FACTORIES)
def test_each_fixture_factory_is_valid(factory, name):
    res = factory(name).run("validate")
    assert res["ok"] and res["schema"] == 2 and res["pipelines"] == [name]


def test_validate_lists_the_wiring_and_the_workspace(factory):
    res = factory("story").run("validate")
    assert res["steps"] == ["implement", "spec"]
    assert res["artifacts"] == ["intent", "notes", "reviews", "spec", "tasks"]
    assert res["workspace"]["home"]["present"] and not res["workspace"]["backend"]["present"]


def messages(f) -> str:
    res = f.run("validate", expect=3)
    assert res["ok"] is False and res["error"] == "invalid wiring"
    return "\n".join(f"{e['file']}: {e['message']}" for e in res["errors"])


def test_schema_1_is_refused_with_a_hint(factory):
    f = factory("story")
    f.edit("fab0.yaml", lambda d: d.update(schema=1))
    assert "this is schema 1, the fab0 0.5 wiring" in messages(f)


def test_a_stages_directory_is_refused(factory):
    f = factory("story")
    (f.home / ".fab0" / "stages").mkdir()
    assert "stages/ is schema 1" in messages(f)


def first_node(d):
    return d["steps"][0]


STORY = [
    ("fab0.yaml", lambda d: d["repos"]["home"].update(owner="ceo"), "repos.home.owner: no role 'ceo'"),
    ("fab0.yaml", lambda d: d["repos"]["home"].update(key="BE"), "'BE' is also the key of"),
    ("fab0.yaml", lambda d: d["pipelines"].append("hotfix"), "no pipeline 'hotfix'"),
    ("fab0.yaml", lambda d: d["profiles"].update(bad={"git": {"branching": "tree"}}), "profiles.bad makes the file"),
    ("fab0.yaml", lambda d: d.pop("tracker"), "an issue trigger needs a tracker"),
    ("fab0.yaml", lambda d: d["git"].update(auto_merge={"into": []}), "which git.auto_merge.into doesn't allow"),
    ("artifacts/spec.yaml", lambda d: d["file"].update(repo="web"), "file.repo: no repo 'web'"),
    ("artifacts/spec.yaml", lambda d: d.update(schema="x.json"), "only yaml and json files have a schema"),
    ("artifacts/tasks.yaml", lambda d: d.update(schema=".fab0/schemas/none.json"), "none.json not found"),
    ("artifacts/notes.yaml", lambda d: d.update(elements={"id": "slug"}), "elements: only yaml and json"),
    (
        "artifacts/notes.yaml",
        lambda d: d.update(verify=[{"sections": ["A"]}]) or d["file"].update(format="text"),
        "sections needs a markdown file",
    ),
    ("steps/spec.yaml", lambda d: d.update(agent="author"), "'author' is neither plugin:name nor a file"),
    ("steps/spec.yaml", lambda d: d.pop("model"), "an agent step needs 'model'"),
    ("steps/spec.yaml", lambda d: d.update(model="huge"), "model: no class 'huge'"),
    ("steps/spec.yaml", lambda d: d.update(skills=["house-style"]), "'house-style' is neither plugin:name nor a skill"),
    ("steps/spec.yaml", lambda d: d.update(owner="ceo"), "owner: no role 'ceo'"),
    ("steps/spec.yaml", lambda d: d["inputs"].append("brief"), "inputs: no artifact 'brief'"),
    ("steps/spec.yaml", lambda d: d["outputs"].append("intent"), "'intent' is issue, which steps only read"),
    ("steps/spec.yaml", lambda d: d["repos"].update(write=["web"]), "repos.write: no repo 'web'"),
    ("steps/spec.yaml", lambda d: d.update(**{"with": {"a": 1}}), "only action and tool steps take arguments"),
    ("steps/spec.yaml", lambda d: d.update(inputs=["spec"]), "reads 'spec' before a step writes it"),
    ("steps/spec.yaml", lambda d: d.update(paths="unit.files"), "uses paths from the unit, which needs foreach"),
    ("steps/spec.yaml", lambda d: d.update(stage="spec"), "Additional properties"),
    ("pipelines/story.yaml", lambda d: d["onStart"].append("git.deploy"), "no action 'git.deploy'"),
    ("pipelines/story.yaml", lambda d: d["steps"].append("review"), "no step 'review'"),
    ("pipelines/story.yaml", lambda d: d["steps"].append("spec"), "step 'spec' appears twice"),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"].insert(0, {"step": "spec", "model": "deep"}),
        "has fields but no executor",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"].append({"step": "spec", "command": "make"}),
        "defined inline and in .fab0/steps/",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"].append({"step": "lint", "command": "x", "model": "deep"}),
        "only agent steps take 'model'",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"].insert(0, {"foreach": "tasks", "steps": [{"step": "x", "command": "y"}]}),
        "foreach reads 'tasks' before a step writes it",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"][1]["steps"].append({"foreach": "tasks", "steps": [{"step": "x", "command": "y"}]}),
        "foreach inside foreach is not supported yet",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"].append({"foreach": "spec", "steps": [{"step": "x", "command": "y"}]}),
        "foreach needs a yaml or json file",
    ),
    (
        "pipelines/story.yaml",
        lambda d: (
            d["steps"].append("implement")
            or d["steps"][1]["steps"].clear()
            or d["steps"][1]["steps"].append({"step": "x", "command": "y"})
        ),
        "uses a unit reference, which needs foreach",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"].append({"call": "hotfix"}),
        "call (nested pipelines) is not supported",
    ),
    ("pipelines/story.yaml", lambda d: d.update(inputs={}), "pipeline inputs and outputs (call) are not supported"),
    ("pipelines/story.yaml", lambda d: first_node(d).update(after=["x"]), "after only orders the branches"),
    ("pipelines/story.yaml", lambda d: first_node(d).update(gate={"human": {"owner": "ceo"}}), "human.owner: no role"),
    ("pipelines/story.yaml", lambda d: first_node(d).update(gate={"auto": {"agent": "judge"}}), "'judge' is neither"),
    (
        "pipelines/story.yaml",
        lambda d: first_node(d).update(gate={"auto": {"agent": "reviewer", "model": "x"}}),
        "gate.auto.model: no class 'x'",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"][2].update(when={"artifact": "tasks", "section": "Docs"}),
        "section needs a markdown file",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"][2].update(when={"artifact": "plan", "exists": True}),
        "no artifact 'plan'",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["steps"][2].update(when={"verdict": "spec", "is": "approve"}),
        "'spec' is no earlier step with an auto gate",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["onEnd"].append({"tracker.comment": {"body": "{spec}"}}),
        "needs a field of an agent or tool step's return data",
    ),
    (
        "pipelines/story.yaml",
        lambda d: d["onStart"].append({"tracker.comment": {"body": "{docs-note.x}"}}),
        "names no earlier step",
    ),
    ("pipelines/story.yaml", lambda d: d.update(git={"branching": "flat"}), "would merge into the base branch"),
    ("pipelines/story.yaml", lambda d: d.update(git={"auto_merge": {"into": ["base"]}}), "'allow_base' is a required"),
]


@pytest.mark.parametrize("rel, change, expected", STORY)
def test_story_wiring_errors(factory, rel, change, expected):
    f = factory("story")
    f.edit(rel, change)
    assert expected in messages(f)


def parallel(d):
    return d["steps"][2]


DOCS = [
    (
        "pipelines/docs.yaml",
        lambda d: parallel(d)["parallel"]["index"][0].update(after=["build"]),
        "after: 'build' is no branch, step or id",
    ),
    (
        "pipelines/docs.yaml",
        lambda d: parallel(d)["parallel"]["index"][0].update(after=["index"]),
        "after: 'index' is in the same branch",
    ),
    (
        "pipelines/docs.yaml",
        lambda d: (
            parallel(d)["parallel"]["spelling"].clear()
            or parallel(d)["parallel"]["spelling"].append({"step": "spelling", "after": ["index"]})
        ),
        "after makes a cycle",
    ),
    ("pipelines/docs.yaml", lambda d: d["trigger"]["manual"].update(id="docs-("), "not a regular expression"),
    (
        "pipelines/docs.yaml",
        lambda d: parallel(d).update(id="pages") or d["steps"][1].update(id="pages"),
        "id 'pages' is used twice",
    ),
]


@pytest.mark.parametrize("rel, change, expected", DOCS)
def test_docs_wiring_errors(factory, rel, change, expected):
    f = factory("docs")
    f.edit(rel, change)
    assert expected in messages(f)


def test_reading_a_sibling_branch_needs_after(factory):
    f = factory("docs")
    f.edit("steps/spelling.yaml", lambda d: d.update(command="codespell", inputs=["notes"]))
    f.edit("steps/links.yaml", lambda d: d.update(command="lychee", outputs=["notes"]))
    (f.home / ".fab0" / "artifacts" / "notes.yaml").write_text(
        "artifact: notes\nkind: file\nfile: {repo: home, path: notes.md}\n"
    )
    assert "reads 'notes' before a step writes it" in messages(f)
    f.edit(
        "pipelines/docs.yaml",
        lambda d: parallel(d)["parallel"].update(spelling=[{"step": "spelling", "after": ["links"]}]),
    )
    assert f.run("validate")["ok"]


TRIAGE = [
    ("fab0.yaml", lambda d: d.pop("ledger"), "the run opens no pull request: set ledger.branch"),
    ("fab0.yaml", lambda d: d.pop("tracker"), "an issue trigger needs a tracker"),
    (
        "pipelines/triage.yaml",
        lambda d: d["steps"][1].update(**{"with": {"add": ["{page.x}"]}}),
        "'{page.x}' names no earlier step",
    ),
    (
        "pipelines/triage.yaml",
        lambda d: d["onEnd"].append({"tracker.comment": {"body": "{label.added}"}}),
        "needs a field of an agent or tool step's return data",
    ),
]


@pytest.mark.parametrize("rel, change, expected", TRIAGE)
def test_triage_wiring_errors(factory, rel, change, expected):
    f = factory("triage")
    f.edit(rel, change)
    assert expected in messages(f)


def test_two_pipelines_cannot_share_a_trigger(factory):
    f = factory("story")
    story = f.read("pipelines/story.yaml")
    story["pipeline"] = "story-copy"
    (f.home / ".fab0" / "pipelines" / "story-copy.yaml").write_text(__import__("yaml").safe_dump(story))
    f.edit("fab0.yaml", lambda d: d["pipelines"].append("story-copy"))
    assert "issue type 'Story' also starts pipeline story" in messages(f)


def test_a_profile_is_applied(factory):
    f = factory("story")
    assert f.run("validate", "--profile", "cheap")["ok"]
    res = f.run("validate", "--profile", "dear", expect=3)
    assert res["errors"] == [{"file": ".fab0/fab0.yaml", "message": "no profile 'dear'"}]


@pytest.mark.parametrize("command", ["apply", "inbox"])
def test_run_commands_are_refused_until_built(factory, command):
    res = factory("story").run(command, "412", expect=4)
    assert "not built yet in schema 2" in res["error"]
