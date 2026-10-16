"""`fab0-resolve log`: a run's events as text, oldest first, and followed as they are written; it reads only the
home checkout's git directory, and never writes."""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from conftest import SCRIPT, STORY_GH, Follower, git
from fab0_engine.events import append_event
from test_events import events, log_path

GOOD = [
    {"time": "2026-10-16T10:12:40Z", "kind": "record", "line": "spec, attempt 1: verified, 8m 28s", "unit": "spec"},
    {"time": "2026-10-16T10:13:02Z", "kind": "waiting", "line": "waiting: spec (gate human)", "unit": "spec"},
    {"time": "2026-10-16T10:20:00Z", "kind": "apply", "line": "apply: spec: merged into fab0/412/base"},
]


def put(f, *items, run: str = "412") -> None:
    for item in items:
        append_event(log_path(f, run), item)


def shown(event: dict) -> str:
    return f"{event['time']}  {event['line']}"


def command(f, *args: str) -> None:
    """Any command, whatever it prints."""
    f.text(*args) if args[0] == "log" else f.run(*args)


def test_log_prints_one_line_per_event_oldest_first(story):
    put(story, *GOOD)
    res = story.text("log", "412")
    assert res.stdout.splitlines() == [shown(e) for e in GOOD]
    assert res.stdout.splitlines()[0] == "2026-10-16T10:12:40Z  spec, attempt 1: verified, 8m 28s"


def test_log_of_a_run_without_events_says_so(story):
    assert story.text("log", "77").stdout == "run 77 has no events here yet\n"
    assert not log_path(story, "77").parent.exists()


def test_log_names_an_event_it_cannot_read_in_its_place(story):
    put(story, *GOOD[:2])
    with log_path(story).open("a") as out:
        out.write("not json\n" + json.dumps({"kind": "apply"}) + "\n")
    put(story, GOOD[2])
    assert story.text("log", "412").stdout.splitlines() == [
        shown(GOOD[0]),
        shown(GOOD[1]),
        "event 3 can't be read",
        "event 4 can't be read",
        shown(GOOD[2]),
    ]


def test_log_prints_an_event_on_one_line(story):
    put(story, {**GOOD[2], "line": "apply: x: first\nsecond"})
    assert story.text("log", "412").stdout == "2026-10-16T10:20:00Z  apply: x: first second\n"


def test_read_only_commands_write_no_events(story):
    put(story, *GOOD)
    before = log_path(story).read_bytes()
    for args in (("status", "412"), ("inbox",), ("validate",), ("log", "412"), ("log", "77")):
        command(story, *args)
    assert log_path(story).read_bytes() == before
    assert [p.name for p in log_path(story).parent.iterdir()] == ["412.jsonl"]


def test_read_only_commands_create_no_log(story):
    for args in (("status", "412"), ("inbox",), ("validate",), ("log", "412")):
        command(story, *args)
    assert not log_path(story).parent.exists()


def test_log_needs_only_the_home_checkout(story, tmp_path):
    put(story, *GOOD)
    gh = tmp_path / "stubs" / "gh"
    gh.parent.mkdir()
    gh.write_text("#!/bin/sh\necho 'gh: not logged in' >&2\nexit 1\n")
    gh.chmod(0o755)
    env = {k: v for k, v in story.env().items() if k != "FAB0_GH_FAKE"}
    env["PATH"] = f"{gh.parent}{os.pathsep}{env['PATH']}"
    yaml_path = story.home / ".fab0" / "fab0.yaml"
    yaml_path.write_text(yaml_path.read_text() + "\nunknown-key: 1\n")
    assert story.run("validate", expect=3)["ok"] is False  # the wiring is invalid; log doesn't read it
    want = [shown(e) for e in GOOD]
    assert story.text("log", "412", env=env).stdout.splitlines() == want
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = story.text("log", "412", "--project", str(story.home), cwd=elsewhere, env=env)
    assert res.stdout.splitlines() == want


def test_log_from_a_worktree_of_the_home_repo_reads_the_same_log(story, tmp_path):
    put(story, *GOOD)
    tree = tmp_path / "tree"
    git(story.repo("home"), "worktree", "add", "-q", "-b", "side", str(tree))
    res = story.text("log", "412", "--project", str(tree))
    assert res.stdout.splitlines() == [shown(e) for e in GOOD]


def test_log_usage_errors(story, tmp_path):
    out = json.loads(story.text("log", "bad/run", expect=2).stdout)
    assert out["ok"] is False and "run id 'bad/run'" in out["error"]
    assert json.loads(story.text("log", "x y", expect=2).stdout)["ok"] is False
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    out = json.loads(story.text("log", "412", cwd=nowhere, expect=2).stdout)
    assert out["ok"] is False and ".fab0/fab0.yaml" in out["error"]
    out = json.loads(story.text("log", "412", "--project", str(nowhere), expect=2).stdout)
    assert out["ok"] is False


def test_follow_prints_what_is_logged_then_each_new_event(story):
    put(story, GOOD[0])
    follower = story.follow("412")
    try:
        assert follower.next() == shown(GOOD[0])
        started = time.monotonic()
        put(story, GOOD[1])
        assert follower.next(5) == shown(GOOD[1])
        assert time.monotonic() - started < 2.5
        put(story, GOOD[2])
        assert follower.next(5) == shown(GOOD[2])
    finally:
        code, err = follower.interrupt()
    assert (code, err) == (0, "")


def test_follow_prints_the_events_a_session_writes(story):
    follower = story.follow("412")
    try:
        assert follower.next() == "run 412 has no events here yet"
        story.run("apply", "412")
        got = [follower.next(5) for _ in range(3)]
        assert got == [shown(e) for e in events(story)]
        assert got[0].endswith("apply: onStart-1: git.run-branch: created fab0/412/base in home, backend")
        story.run("next", "412")
        assert follower.next(5).endswith(events(story)[-1]["line"])
    finally:
        code, err = follower.interrupt()
    assert (code, err) == (0, "")


def test_follow_before_the_run_starts_waits_for_it(story):
    follower = story.follow("78")
    try:
        assert follower.next() == "run 78 has no events here yet"
        assert follower.next(1.5) is None  # still waiting, and nothing more said
        put(story, GOOD[0], run="78")
        assert follower.next(5) == shown(GOOD[0])
    finally:
        code, err = follower.interrupt()
    assert (code, err) == (0, "")


def test_follow_prints_half_an_event_only_once_it_is_complete(story):
    put(story, GOOD[0])
    whole = json.dumps(GOOD[1], separators=(",", ":")) + "\n"
    follower = story.follow("412")
    try:
        assert follower.next() == shown(GOOD[0])
        with log_path(story).open("ab") as out:
            out.write(whole[:25].encode())
            out.flush()
            assert follower.next(2.5) is None, "half a line is not shown"
            out.write(whole[25:].encode())
        assert follower.next(5) == shown(GOOD[1])
        assert follower.next(2.5) is None, "and it is shown once"
    finally:
        follower.interrupt()


def test_follow_names_an_event_it_cannot_read_and_goes_on(story):
    put(story, *GOOD[:2])
    follower = story.follow("412")
    try:
        assert [follower.next(), follower.next()] == [shown(GOOD[0]), shown(GOOD[1])]
        with log_path(story).open("a") as out:
            out.write("{broken\n")
        assert follower.next(5) == "event 3 can't be read"
        put(story, GOOD[2])
        assert follower.next(5) == shown(GOOD[2])
    finally:
        code, err = follower.interrupt()
    assert (code, err) == (0, "")


def test_follow_ends_on_an_interrupt_its_parent_ignored(story):
    """A background job of a shell starts with SIGINT ignored; the follower still ends on Ctrl-C, with no traceback."""
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "log", "412", "--follow"],
        cwd=story.home,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=story.env(),
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
    )
    follower = Follower(proc)
    assert follower.next() == "run 412 has no events here yet"
    assert follower.interrupt() == (0, "")


def test_apply_says_how_to_follow_the_run(story):
    res = story.run("apply", "412")
    follow = shlex.split(res["follow"])
    assert Path(follow[0]).is_absolute() and Path(follow[0]) == SCRIPT.resolve()
    assert follow[1:] == ["log", "412", "--follow", "--project", str(story.home.resolve())]
    assert story.run("apply", "412")["follow"] == res["follow"]


def test_next_apply_says_how_to_follow_the_run_too(story):
    assert story.run("next", "412", "--apply")["follow"] == story.run("apply", "412")["follow"]
    assert "follow" not in story.run("next", "412"), "only the command that applies says it"


def test_the_follow_command_quotes_its_paths(factory, tmp_path):
    f = factory("story")
    spaced = tmp_path / "my shop"
    f.home.rename(spaced)
    f.home = spaced
    f.init_git(STORY_GH)
    res = f.run("apply", "412")
    assert shlex.split(res["follow"])[-1] == str(spaced.resolve()) and "'" in res["follow"]
