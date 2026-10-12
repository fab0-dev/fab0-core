"""The brief of a per-task stage lists the run's other tasks."""

HEADING = "## Other tasks of this run"

THREE = """- id: T1
  title: Backend part
  repo: backend
  files: [src/a.py]
  traces_to: [S1]
- id: T2
  title: Home part
  repo: home
  files: [web/b.html]
  traces_to: [S1, S2]
- id: T3
  title: Backend extra
  repo: backend
  files: [src/c.py]
  depends_on: [T2]
"""

ONE = """- id: T1
  title: Only task
  repo: backend
  files: [src/a.py]
  traces_to: [S1]
"""


def section(brief: str) -> str:
    return brief.split(HEADING)[1].split("\n## ")[0]


def test_lists_other_tasks_in_order_without_itself(ws):
    ws.do_spec(tasks=THREE)
    ws.do_plan()
    brief = ws.next("--task", "T2", user="po-user")["brief"]
    assert brief.index("## Task") < brief.index(HEADING) < brief.index("## Inputs")
    body = section(brief)
    assert body.index("`T1`") < body.index("`T3`")
    assert "Backend part" in body and "repo backend" in body and "delivers S1" in body
    assert "`T2`" not in body and "Home part" not in body


def test_lists_tasks_whatever_their_state(ws):
    ws.do_spec(tasks=THREE)
    ws.do_plan()
    ws.do_task("T1", "backend-dev", {"src/a.py": "x = 1\n"})
    body = section(ws.next("--task", "T2", user="po-user")["brief"])
    assert "`T1`" in body and "`T3`" in body


def test_task_without_scenarios(ws):
    ws.do_spec(tasks=THREE)
    ws.do_plan()
    body = section(ws.next("--task", "T2", user="po-user")["brief"])
    assert "Backend extra (repo backend; delivers no scenarios)" in body


def test_only_task(ws):
    ws.do_spec(tasks=ONE)
    ws.do_plan()
    body = section(ws.next("--task", "T1", user="backend-dev")["brief"])
    assert "no other tasks" in body and "`T" not in body


def test_other_briefs_unchanged(ws):
    first = ws.next()
    assert first["stage"] == "spec" and HEADING not in first["brief"]
    ws.do_spec(tasks=THREE)
    ws.do_plan()
    res = ws.next("--task", "T1", user="backend-dev")
    repo = ws.api
    ws.checkout(repo, res["branch"], res["start_from"])
    ws.write(repo, "src/a.py", "x = 1\n")
    ws.record("implement", {"summary": "done"}, task="T1")
    ws.commit(repo, "T1")
    gate = ws.next("--task", "T1", user="backend-dev")
    assert gate["kind"] == "gate" and HEADING not in gate["brief"]
