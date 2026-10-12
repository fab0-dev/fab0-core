"""Write scope is checked after a stage: changes to repos the stage may not write fail `record`."""

from conftest import git


def _hand_out_t1(ws):
    ws.do_spec()
    ws.do_plan()
    res = ws.next("--task", "T1", user="backend-dev")
    assert res["write"] == ["backend"]
    ws.checkout(ws.api, res["branch"], res["start_from"])
    ws.write(ws.api, "src/booking.py", "def extend(b):\n    return b\n")
    return res


def test_a_change_outside_the_write_scope_is_refused(ws):
    _hand_out_t1(ws)
    ws.write(ws.home, "README.md", "# shop\n\nChanged by the implement stage.\n")
    res = ws.record("implement", {"summary": "Done."}, task="T1", expect=4)
    assert res["violations"] == {"home": ["README.md"]}
    # Undo it, and the same hand-out records.
    git(ws.home, "checkout", "--", "README.md")
    assert ws.record("implement", {"summary": "Done."}, task="T1")["entry"] == "work/412/ledger/implement-T1-1.yaml"


def test_earlier_unrelated_edits_do_not_fail_the_stage(ws):
    ws.do_spec()
    ws.do_plan()
    ws.write(ws.home, "notes.txt", "my own local notes\n")
    res = ws.next("--task", "T1", user="backend-dev")
    ws.checkout(ws.api, res["branch"], res["start_from"])
    ws.write(ws.api, "src/booking.py", "def extend(b):\n    return b\n")
    assert ws.record("implement", {"summary": "Done."}, task="T1")["verified"]


def test_a_new_commit_outside_the_scope_is_refused(ws):
    _hand_out_t1(ws)
    ws.write(ws.home, "other.txt", "x\n")
    ws.commit(ws.home, "unrelated commit during the stage")
    res = ws.record("implement", {"summary": "Done."}, task="T1", expect=4)
    assert any(v.startswith("HEAD moved") for v in res["violations"]["home"])


def test_a_gate_may_not_write_anything(ws):
    _hand_out_t1(ws)
    ws.record("implement", {"summary": "Done."}, task="T1")
    ws.commit(ws.api, "T1")
    assert ws.next("--task", "T1", user="backend-dev")["kind"] == "gate"
    ws.write(ws.api, "src/booking.py", "# the reviewer fixed it itself\n")
    res = ws.record("implement", {"decision": "approve", "reasons": ["ok"]}, task="T1", gate=True, expect=4)
    assert res["violations"] == {"backend": ["src/booking.py"]}


def test_an_invalid_gate_return_is_refused(ws):
    _hand_out_t1(ws)
    ws.record("implement", {"summary": "Done."}, task="T1")
    ws.commit(ws.api, "T1")
    ws.next("--task", "T1", user="backend-dev")
    res = ws.record("implement", {"decision": "maybe"}, task="T1", gate=True, expect=4)
    assert "isn't a valid review" in res["error"]
