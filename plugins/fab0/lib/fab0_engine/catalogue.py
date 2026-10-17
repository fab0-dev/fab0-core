"""The names of fab0's built-in actions. `apply` performs them (step R3); the wiring may only name these."""

GIT = ("git.run-branch", "git.sync-base")
PR = ("pr.open-run", "pr.ready", "pr.merge", "pr.comment")
TRACKER = (
    "tracker.child.sync",
    "tracker.child.close",
    "tracker.assign",
    "tracker.field",
    "tracker.label",
    "tracker.comment",
    "tracker.close",
)
ACTIONS = frozenset(GIT + PR + TRACKER)
