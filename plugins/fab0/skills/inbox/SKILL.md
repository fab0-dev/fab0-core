---
name: inbox
description: Show what every open fab0 run needs from the current person, what anyone can run, and what is waiting on others. Read-only; its suggestions are exactly what /fab0:run would do.
argument-hint: "[--as USER]"
---

# /fab0:inbox $ARGUMENTS

1. Run `fab0-resolve inbox $ARGUMENTS`. It changes nothing.
2. If it fails, show the error and stop.
3. Render its JSON as follows. Leave out empty sections, and keep each line short.

```
Ready for you (<user>):
  <command>   <type>: <task_title or title>   <stage>[, <note>]
Pending actions (anyone may run):
  <command>   <type>: <title>   <action>
Waiting on others:
  <run>[ <task>]   <type>: <task_title or title>   <reason>
Blocked:
  <run>   <type>: <title>   <reason>
Done, issue still open:
  <run>   <type>: <title>
```

Don't run anything it suggests. Running is the person's choice, through `/fab0:run`.
