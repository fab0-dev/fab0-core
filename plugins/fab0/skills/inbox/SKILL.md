---
name: inbox
description: Show every open fab0 run of this project, what waits on the current person, what anyone can run now, and what is blocked and why. Read-only.
argument-hint: "[--as USER]"
---

# /fab0:inbox $ARGUMENTS

1. Run `fab0-resolve inbox $ARGUMENTS`. It changes nothing.
2. If it fails, show the `error` and every `fix` line verbatim (an engine older than the installed plugin fails with a `fix` list), and stop. Don't try another engine.
3. Render its JSON as below. Leave out empty sections and keep each line short.

```
Yours (<user>: <roles>):
  <run> <unit>   <reason>   <pull, if any>
Ready to run:
  <command>   <units, comma-separated>
Waiting on others:
  <run> <unit>   <owner>: <reason>
Blocked:
  <run> <unit>   <reason>
```

Write a reason as its key and value, for example `gate human`, `questions 2`, `claimed elsewhere`, `tracker acme/shop#9`, `invalid-dependency after makes a cycle: T1 -> T2 -> T1`. A `pull` may be in the runs repository: the review pull request of a unit whose outputs are in the run store. A unit with `owner` empty is another machine's, under way there. Under the sections, list the open runs once (`runs`: run, pipeline, title), and any `skipped` runs with why. Runs are found in the run store, in run branches, and in tracker items that match a trigger. Under each run, when it has `latest`, show its time and line on one line, for example `latest 2026-10-16T10:12:40Z  spec, attempt 1: verified, 8m 28s`; a run without `latest` has no events on this machine, so show nothing for it.

Don't run anything it suggests. Running is the person's choice, through `/fab0:run`.
