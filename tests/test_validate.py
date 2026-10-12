"""The resolver refuses to run on invalid wiring, and names what is wrong."""

import pytest
import yaml


def edit(ws, rel: str, change) -> None:
    path = ws.home / ".fab0" / rel
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def messages(ws) -> str:
    res = ws.run("validate", expect=3)
    return "\n".join(f"{e['file']}: {e['message']}" for e in res["errors"])


def test_the_fixture_is_valid(ws):
    res = ws.run("validate")
    assert res["ok"] and res["pipelines"] == ["story"]


@pytest.mark.parametrize(
    "rel, change, expected",
    [
        ("stages/spec.yaml", lambda d: d["inputs"].append("brief"), "inputs: no artifact type 'brief'"),
        ("stages/spec.yaml", lambda d: d.update(model="huge"), "model: no class 'huge' in models.claude-code"),
        ("stages/spec.yaml", lambda d: d.pop("owner"), "an agent stage needs 'owner'"),
        ("stages/spec.yaml", lambda d: d.update(owner="ceo"), "owner: no role 'ceo' in people"),
        (
            "stages/spec.yaml",
            lambda d: d.update(agent="spec-author"),
            "agent: 'spec-author' is neither plugin:name nor a file in .claude/agents/",
        ),
        ("stages/spec.yaml", lambda d: d.update(colour="blue"), "Additional properties are not allowed"),
        ("stages/spec.yaml", lambda d: d.update(**{"return": {"mood": "optional"}}), "return: 'mood' is not a known"),
        ("stages/spec.yaml", lambda d: d["outputs"].append("intent"), "outputs: 'intent' is read from GitHub"),
        ("stages/spec.yaml", lambda d: d.update(stage="specification"), "doesn't match the file name"),
        ("stages/plan.yaml", lambda d: d.update(owner="backend"), "an action stage takes no 'owner'"),
        ("stages/implement.yaml", lambda d: d["gate"].update(model="huge"), "gate.model: no class 'huge'"),
        (
            "artifacts/tasks.yaml",
            lambda d: d.update(schema="other-plugin:schemas/tasks.json"),
            "only fab0: and project-relative schema paths resolve for now",
        ),
        ("artifacts/spec.yaml", lambda d: d.update(schema="fab0:schemas/tasks.json"), "only yaml and json artifacts"),
        ("artifacts/tasks.yaml", lambda d: d["location"].update(repo="mobile"), "location.repo: no repo 'mobile'"),
        ("fab0.yaml", lambda d: d["repos"]["backend"].update(owner="nobody"), "repos.backend.owner: no role 'nobody'"),
        ("fab0.yaml", lambda d: d["repos"].pop("home"), "'home' is a required property"),
        ("fab0.yaml", lambda d: d.update(pipelines=["story", "bug"]), "pipelines: no pipeline 'bug'"),
        ("fab0.yaml", lambda d: d["profiles"].update(bad={"schema": 2}), "profiles.bad makes the file invalid"),
        (
            "pipelines/story.yaml",
            lambda d: d["stages"].insert(0, d["stages"].pop(1)),
            "'plan' reads 'tasks' before the stage that writes it",
        ),
        ("pipelines/story.yaml", lambda d: d["stages"].insert(0, d["stages"].pop()), "deliver must be the last stage"),
        (
            "pipelines/story.yaml",
            lambda d: d["stages"][2].update(call={"pipeline": "delivery"}),
            "'call' is not supported yet",
        ),
        (
            "pipelines/story.yaml",
            lambda d: d["stages"].__setitem__(0, {"stage": "spec", "when": "x"}),
            "'when' is not supported yet",
        ),
        (
            "pipelines/story.yaml",
            lambda d: d["stages"][2].update(stages=["implement", "spec"]),
            "stage 'spec' appears twice",
        ),
        (
            "pipelines/story.yaml",
            lambda d: d["stages"].insert(1, "implement"),
            "has owner repo-owner, which needs a foreach element",
        ),
    ],
)
def test_invalid_wiring_is_named(ws, rel, change, expected):
    edit(ws, rel, change)
    assert expected in messages(ws)


def test_a_project_agent_needs_its_file(ws):
    edit(ws, "stages/spec.yaml", lambda d: d.update(agent="spec-author"))
    assert "spec-author" in messages(ws)
    (ws.home / ".claude" / "agents").mkdir(parents=True)
    (ws.home / ".claude" / "agents" / "spec-author.md").write_text("---\nname: spec-author\n---\n")
    assert ws.run("validate")["ok"]


def test_next_refuses_invalid_wiring(ws):
    edit(ws, "stages/spec.yaml", lambda d: d.update(model="huge"))
    res = ws.next(expect=3)
    assert res["error"] == "invalid wiring"


def test_an_unknown_profile_is_refused(ws):
    res = ws.run("next", "412", "--as", "po-user", "--profile", "lavish", expect=3)
    assert "no profile 'lavish'" in res["errors"][0]["message"]


def test_an_issue_type_without_a_pipeline_blocks(ws):
    data = ws.gh
    data["issues"]["acme/shop#412"]["type"] = "Bug"
    ws.save_gh(data)
    res = ws.next()
    assert res["kind"] == "blocked" and "issue type 'Bug'" in res["reason"]


def test_child_run_ids_are_not_supported_yet(ws):
    res = ws.run("next", "412-BE-1", "--as", "po-user", expect=2)
    assert "only top-level runs" in res["error"]
