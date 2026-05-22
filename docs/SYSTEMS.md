# Ryze Education Bot — How the Core Systems Work

This document explains the three main automated systems in the bot: lesson reminders,
attendance tracking, and voice channel session logging.

---

## 1. Lesson Reminders

**Files involved:**
- `bot/cogs/reminders.py` — the loop that fires checks every minute
- `bot/services/reminder_service.py` — message building and deduplication logic
- `bot/config.py` — configures which offsets are active

### How it works

A background task (`_reminder_loop`) wakes up **every minute** while the bot is running.
Each tick it:

1. Fetches all upcoming lessons within the next 25 hours from the database.
2. For every lesson, iterates over the configured reminder offsets (see below).
3. Calculates `fire_at = lesson.start_time - offset`.
4. Checks whether `now` falls inside the 1-minute window `[fire_at, fire_at + 1 min)`.
5. If yes, sends the reminder — otherwise skips.

### Configured offsets

Defined in `bot/config.py`:

```python
REMINDER_OFFSETS_MINUTES: list[int] = [24 * 60, 60, 15]
# → 24 hours before, 1 hour before, 15 minutes before
```

To add a 30-minute reminder, change this to:

```python
REMINDER_OFFSETS_MINUTES: list[int] = [24 * 60, 60, 30, 15]
```

### Two reminder types per offset

For each offset that fires, the bot sends **two** kinds of messages:

| Type | Recipient | Content |
|------|-----------|---------|
| **Channel post** | Class text channel | Mentions the `@class role`, shows lesson time and Meet link |
| **DM** | Every active enrolled student + assigned tutor | Personal message with lesson time, Meet link or location, prep reminder |

### Deduplication

Every successfully sent reminder is logged to the `reminder_logs` table with columns:
`lesson_id`, `reminder_type` (e.g. `"15min"`), `channel` (`dm` or `class_channel`),
`user_id` (for DMs), `success`, `sent_at`.

Before sending, `has_reminder_been_sent()` queries that table. If a successful log already
exists for that combination, the reminder is skipped. This means:
- The bot can restart mid-day without re-sending reminders that already went out.
- If the reminder loop fires twice in one minute (rare), no duplicate is sent.
- **Failed reminders are retried** — a failed log entry does not count as "sent".

### What happens if the bot is offline?

If the bot is down during the 1-minute fire window for a reminder, that reminder is
permanently missed for that offset. It will not be sent retroactively when the bot
comes back online. The deduplication table only suppresses re-sends of *successful*
reminders, so this is safe.

---

## 2. Attendance Tracking

**Files involved:**
- `bot/cogs/attendance.py` — voice state listener + finalise loop + slash commands
- `bot/services/attendance_service.py` — all business logic
- `bot/config.py` — configures thresholds

### Overview

Attendance goes through three stages:

```
Real-time voice tracking  →  Status calculation at lesson end  →  Manual override (optional)
```

### Stage 1 — Real-time voice tracking

Discord fires `on_voice_state_update` every time someone joins or leaves a voice channel.
The bot catches this event and:

**On join:**
1. Looks up which class group owns that voice channel (`get_class_group_by_voice_channel`).
2. Finds an active lesson for that class within the detection window (±15 min by default).
3. Creates a `VoiceSession` row: `joined_at = now`, `left_at = NULL`.
4. Creates (or finds) an `AttendanceRecord` for that student + lesson, records `joined_at`.
5. If this is the first join of the lesson, sets the lesson status to `active`.

**On leave:**
1. Finds the open `VoiceSession` for that user in that channel.
2. Calculates `duration_minutes = left_at - joined_at`.
3. Adds that duration to `AttendanceRecord.total_minutes`.
4. Updates `AttendanceRecord.left_at`.

**Multiple joins/rejoins** are fully supported — each creates a new `VoiceSession`
row, and durations accumulate in `total_minutes`. For example:
- Join 4:00, leave 4:20 → VoiceSession 1: 20 min
- Rejoin 4:22, leave 5:00 → VoiceSession 2: 38 min
- `AttendanceRecord.total_minutes = 58`

### Stage 2 — Automatic finalisation at lesson end

A background task (`_finalise_loop`) runs **every minute**. It looks for lessons where
`end_time <= now` and `status == active`, then calls `finalise_lesson_attendance()`.

Finalisation does two things for **every enrolled student**:

1. **Never joined** → marks their `AttendanceRecord` as `absent`.
2. **Joined at least once** → calls `calculate_attendance_status()`:

```
Thresholds (bot/config.py):
  ATTENDANCE_PRESENT_THRESHOLD = 0.70   # must attend ≥70 % of lesson duration
  ATTENDANCE_LATE_MINUTES      = 15     # first join >15 min after start = late

Rules:
  total_minutes ≥ 70% AND joined on time  → present
  total_minutes ≥ 70% AND joined late     → late
  total_minutes < 70%                     → left_early
  never joined                            → absent
```

After finalisation, the lesson status is set to `completed`.

### Stage 3 — Manual override

`/mark_attendance <lesson_id> <student> <status>` lets a tutor or admin override
any status after the fact. The record is saved with `marked_by = tutor` so you can
distinguish manual changes from system-calculated ones.

### Voice channel detection window

Configured in `bot/config.py`:

```python
LESSON_WINDOW_BEFORE_MINUTES = 15   # joins up to 15 min early are linked to the lesson
LESSON_WINDOW_AFTER_MINUTES  = 120  # joins up to 2 hours after start still link
```

If someone joins a voice channel and no lesson falls within this window, a `VoiceSession`
is still created (with `lesson_id = NULL`) but no `AttendanceRecord` is made.

---

## 3. Voice Channel Session Logging

**Tables involved:**
- `voice_sessions` — raw join/leave events, one row per session segment
- `attendance_records` — aggregated result per student per lesson

### `voice_sessions` table columns

| Column | Type | Description |
|--------|------|-------------|
| `id` | int | Primary key |
| `lesson_id` | int (FK, nullable) | Linked lesson, or NULL if no lesson was active |
| `user_id` | int (FK) | The Ryze Education user record |
| `discord_voice_channel_id` | bigint | Which voice channel they joined |
| `joined_at` | timestamptz | When they entered the channel |
| `left_at` | timestamptz (nullable) | When they left — NULL if still in channel |
| `duration_minutes` | int (nullable) | `left_at - joined_at` in whole minutes |

### `attendance_records` table columns

| Column | Type | Description |
|--------|------|-------------|
| `lesson_id` | int (FK) | The lesson |
| `user_id` | int (FK) | The student |
| `status` | enum | `present` · `late` · `left_early` · `absent` · `unknown` |
| `joined_at` | timestamptz | First voice join of the lesson |
| `left_at` | timestamptz | Last voice leave of the lesson |
| `total_minutes` | int | Sum of all `VoiceSession.duration_minutes` for this lesson |
| `marked_by` | enum | `system` (auto-calculated) or `tutor` (manual override) |

### How to query voice sessions in pgAdmin

To see raw join/leave events for a lesson:

```sql
SELECT u.full_name, vs.joined_at AT TIME ZONE 'Australia/Sydney' AS joined_sydney,
       vs.left_at  AT TIME ZONE 'Australia/Sydney' AS left_sydney,
       vs.duration_minutes
FROM   voice_sessions vs
JOIN   users u ON u.id = vs.user_id
WHERE  vs.lesson_id = <lesson_id>
ORDER  BY u.full_name, vs.joined_at;
```

To see the finalised attendance summary for a lesson:

```sql
SELECT u.full_name, ar.status, ar.total_minutes, ar.marked_by
FROM   attendance_records ar
JOIN   users u ON u.id = ar.user_id
WHERE  ar.lesson_id = <lesson_id>
ORDER  BY u.full_name;
```

### Discord commands for attendance

| Command | What it shows |
|---------|---------------|
| `/attendance <lesson_id>` | Final status + total minutes per student for one lesson |
| `/voice_attendance <date> [lesson_id]` | Per-session join → leave times and durations for every student on a given day |
| `/student_attendance <student>` | Last 20 lessons for one student with date and status |
| `/mark_attendance <lesson_id> <student> <status>` | Override a student's status manually |
