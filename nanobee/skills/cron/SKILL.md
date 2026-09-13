---
name: cron
description: Schedule reminders and recurring tasks.
---

# Cron

Use the `cron` tool to schedule reminders or recurring tasks.

## Three Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **One-time** - runs once at a specific time, then auto-deletes

## Minimum Interval (Safety)

All schedules have a hard safety floor: **the gap between two consecutive
occurrences must be at least 30 seconds**.

- `every_seconds` must be **at least 30**.
- `cron_expr` is checked on the **smallest interval between consecutive
  occurrences**, independent of when the job is created. Minute-level
  expressions (e.g. `*/2 * * * *`) always pass; sub-minute expressions are
  rejected (e.g. `* * * * * 0,45` fires at second 0 and 45 of every minute —
  15s apart — and is refused). In the 6-field form the seconds field goes
  **last**.
- one-time `at` must be **at least 30 seconds in the future**.

Prefer comfortable intervals like **60+ seconds** or longer for recurring tasks.

## Examples

Fixed reminder:
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Dynamic task (agent executes each time):
```
cron(action="add", message="Check server disk usage and report", every_seconds=600)
```

One-time scheduled task (compute ISO datetime from current time):
```
cron(action="add", message="Remind me about the meeting", at="2026-06-08T18:30:00")
```

Timezone-aware cron:
```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="Asia/Shanghai")
```

List/remove:
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 6:30 PM daily | cron_expr: "30 18 * * *", tz: "Asia/Shanghai" |
| at a specific time | at: ISO datetime string (compute from current time) |

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
