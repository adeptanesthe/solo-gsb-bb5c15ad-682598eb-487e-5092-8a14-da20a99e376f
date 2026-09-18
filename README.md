# iCalendar Recurrence Expansion & Availability API

A local HTTP API that expands iCalendar (RFC 5545) recurring events into
concrete UTC instances inside a query window, and finds the **common free
time** across several named calendars when intersected with per-weekday
local working hours.

- Expands `DTSTART` + `RRULE` + `RDATE` − `EXDATE`
- Merges overrides by `UID` + `RECURRENCE-ID`: an override can **reschedule**
  an instance (judged against the window by its *actual* new start/end; the
  original slot is never kept) or **cancel** it via `STATUS:CANCELLED`
- Interprets `TZID` as IANA timezones with correct DST handling
- `/availability` builds working hours per **local date** (DST-correct),
  clips all busy instances to them, merges half-open intervals (adjacent
  included), and returns the complement shared by every calendar
- `/calendar-diff` compares two ICS versions over one UTC window by
  `UID + RECURRENCE-ID` and returns identity-sorted `added`/`removed`/
  `modified` instances (with `before`/`after`)
- `/alarms` expands `VALARM` reminders for every instance in a window:
  a master event's alarms apply to each recurring instance, overrides
  carry their own, and `REPEAT`/`DURATION` fan alarms out
- Results sorted by UTC start time, then `UID` (stable)
- Clear `422` errors for invalid rules, unknown timezones, or bad input

Stack: Python · FastAPI · icalendar · python-dateutil · zoneinfo

## Quick start (Docker Compose)

```bash
docker compose up --build
```

The API listens on **http://localhost:8000** (interactive docs at `/docs`,
health check at `/health`).

## API

### `POST /expand`

Request body:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

An instance is returned when it **overlaps** `[window_start, window_end)`
(a zero-duration event must start inside the window). Each instance carries
`uid`, `recurrence_id` (UTC, `null` for non-recurring events), `summary`,
`start`, `end`, and `status`; all timestamps are UTC (`Z`).

### Example

```bash
curl -s http://localhost:8000/expand \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART;TZID=America/New_York:20260301T090000\r\nDTEND;TZID=America/New_York:20260301T100000\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=America/New_York:20260308T090000\r\nSUMMARY:Weekly sync\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nRECURRENCE-ID;TZID=America/New_York:20260315T090000\r\nDTSTART;TZID=America/New_York:20260316T110000\r\nDTEND;TZID=America/New_York:20260316T120000\r\nSUMMARY:Weekly sync (moved)\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-02-25T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

Response — note the DST shift (14:00Z → 13:00Z), the `EXDATE`-removed
March 8 occurrence, and the March 15 instance replaced by its override at
the new time (the original slot is gone):

```json
{
  "window_start": "2026-02-25T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "count": 2,
  "instances": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-01T14:00:00Z",
      "summary": "Weekly sync",
      "start": "2026-03-01T14:00:00Z",
      "end": "2026-03-01T15:00:00Z",
      "status": "CONFIRMED"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-15T13:00:00Z",
      "summary": "Weekly sync (moved)",
      "start": "2026-03-16T15:00:00Z",
      "end": "2026-03-16T16:00:00Z",
      "status": "CONFIRMED"
    }
  ]
}
```

### `POST /availability`

Finds free slots that lie inside the working hours **and** avoid busy
instances in *every* supplied calendar. Request body:

| Field             | Type                | Description                                                       |
| ----------------- | ------------------- | ----------------------------------------------------------------- |
| `calendars`       | object              | Map of name → iCalendar text; may be empty or omitted             |
| `window_start`    | string              | ISO 8601 timestamp, converted to UTC (naive = UTC)                |
| `window_end`      | string              | ISO 8601 timestamp, converted to UTC (naive = UTC)                |
| `timezone`        | string              | IANA timezone in which `working_hours` are interpreted            |
| `working_hours`   | object              | Keys `monday`…`sunday`; each a list of `{start, end}` local times |
| `minimum_minutes` | integer             | Only free slots at least this long are returned (must be > 0)     |

Working periods are generated for each local date in the window and then
converted to UTC, so a fixed local time shifts correctly across DST
transitions. A missing weekday (or an empty list) means no working time
that day; multiple periods per day are supported (e.g. a lunch break).
Periods must not be inverted (`end <= start`) or cross midnight.

Busy time is every expanded instance that is not `STATUS:CANCELLED` and
has a positive duration; instances are clipped to the working periods and
merged as half-open `[start, end)` intervals, with touching intervals
merged (back-to-back meetings leave no free instant between them). The
complement inside the working periods is returned, sorted by `start`,
keeping only slots of at least `minimum_minutes` (inclusive at the
boundary). Each slot carries UTC `start`, `end`, and integer `minutes`.

### Example

```bash
curl -s http://localhost:8000/availability \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": {
      "alice": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART;TZID=Europe/Berlin:20260309T100000\r\nDTEND;TZID=Europe/Berlin:20260309T110000\r\nSUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "bob": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:b1\r\nDTSTART:20260309T140000Z\r\nDTEND:20260309T153000Z\r\nSUMMARY:Interview\r\nEND:VEVENT\r\nEND:VCALENDAR"
    },
    "window_start": "2026-03-09T00:00:00Z",
    "window_end": "2026-03-10T00:00:00Z",
    "timezone": "Europe/Berlin",
    "working_hours": {
      "monday": [
        {"start": "09:00", "end": "12:00"},
        {"start": "13:00", "end": "17:00"}
      ]
    },
    "minimum_minutes": 30
  }'
```

Berlin is UTC+1 on this date, so the working periods are `08:00–11:00Z`
and `12:00–16:00Z`; Alice blocks `09:00–10:00Z` and Bob `14:00–15:30Z`.
The lunch break is not free time, and the final 30-minute tail is kept
because the minimum length is inclusive:

```json
{
  "window_start": "2026-03-09T00:00:00Z",
  "window_end": "2026-03-10T00:00:00Z",
  "timezone": "Europe/Berlin",
  "count": 4,
  "slots": [
    {"start": "2026-03-09T08:00:00Z", "end": "2026-03-09T09:00:00Z", "minutes": 60},
    {"start": "2026-03-09T10:00:00Z", "end": "2026-03-09T11:00:00Z", "minutes": 60},
    {"start": "2026-03-09T12:00:00Z", "end": "2026-03-09T14:00:00Z", "minutes": 120},
    {"start": "2026-03-09T15:30:00Z", "end": "2026-03-09T16:00:00Z", "minutes": 30}
  ]
}
```

### `POST /calendar-diff`

Compares an **old** and a **new** version of a calendar over one shared UTC
window. Both ICS texts are expanded with the same recurrence engine used by
`/expand`; instance identity is fixed to `UID + RECURRENCE-ID` for recurring
instances and to `UID` alone for non-recurring events (`recurrence_id` is
`null`). Only instances that overlap the common window take part in the
comparison.

| Field          | Type   | Description                                         |
| -------------- | ------ | --------------------------------------------------- |
| `old_ics`      | string | Old version of the iCalendar text                   |
| `new_ics`      | string | New version of the iCalendar text                   |
| `window_start` | string | Shared ISO 8601 window start, converted to UTC      |
| `window_end`   | string | Shared ISO 8601 window end, converted to UTC        |

The response lists:

- `added` — identities present only in the new calendar,
- `removed` — identities present only in the old calendar,
- `modified` — identities present on both sides whose `start`, `end`,
  `summary` or `status` changed, each with a `before` and an `after` object
  in the same shape as an `/expand` instance.

Completely identical instances are not returned. All three lists are sorted
by identity (`UID`, then `RECURRENCE-ID`; a `null` `RECURRENCE-ID` sorts
first). Cancelling an occurrence with a `STATUS:CANCELLED` override makes it
disappear from the expansion, so it is reported as `removed`; likewise an
instance rescheduled across the window boundary appears as
`removed`/`added`, while one that still overlaps the window on both sides is
`modified` (its identity never changes).

### Example

```bash
curl -s http://localhost:8000/calendar-diff \
  -H 'Content-Type: application/json' \
  -d '{
    "old_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:standup\r\nDTSTART:20260302T090000Z\r\nDTEND:20260302T091500Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:oneoff\r\nDTSTART:20260304T140000Z\r\nDTEND:20260304T150000Z\r\nSUMMARY:Old review\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "new_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:standup\r\nDTSTART:20260302T090000Z\r\nDTEND:20260302T091500Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260309T090000Z\r\nDTSTART:20260309T100000Z\r\nDTEND:20260309T101500Z\r\nSUMMARY:Standup (15 min later)\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:workshop\r\nDTSTART:20260320T130000Z\r\nDTEND:20260320T150000Z\r\nSUMMARY:Workshop\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-03-01T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

The new calendar adds `workshop`, drops `oneoff`, and moves the March 9
standup instance (its `RECURRENCE-ID` stays `2026-03-09T09:00:00Z`); the
unchanged March 2/16/23 standups are not reported:

```json
{
  "window_start": "2026-03-01T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "added": [
    {
      "uid": "workshop",
      "recurrence_id": null,
      "summary": "Workshop",
      "start": "2026-03-20T13:00:00Z",
      "end": "2026-03-20T15:00:00Z",
      "status": "CONFIRMED"
    }
  ],
  "removed": [
    {
      "uid": "oneoff",
      "recurrence_id": null,
      "summary": "Old review",
      "start": "2026-03-04T14:00:00Z",
      "end": "2026-03-04T15:00:00Z",
      "status": "CONFIRMED"
    }
  ],
  "modified": [
    {
      "before": {
        "uid": "standup",
        "recurrence_id": "2026-03-09T09:00:00Z",
        "summary": "Standup",
        "start": "2026-03-09T09:00:00Z",
        "end": "2026-03-09T09:15:00Z",
        "status": "CONFIRMED"
      },
      "after": {
        "uid": "standup",
        "recurrence_id": "2026-03-09T09:00:00Z",
        "summary": "Standup (15 min later)",
        "start": "2026-03-09T10:00:00Z",
        "end": "2026-03-09T10:15:00Z",
        "status": "CONFIRMED"
      }
    }
  ]
}
```

### `POST /alarms`

Expands the `VALARM` reminders of every `VEVENT` instance that overlaps the
query window, returning only reminders whose **trigger instant** falls
inside `[window_start, window_end)`. Request body is the same shape as
`/expand`:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

Alarm semantics:

- Only `ACTION:DISPLAY` alarms are supported (`AUDIO`, `EMAIL`, … → `422`);
  a `DESCRIPTION` is required.
- `TRIGGER` may be **absolute** (`VALUE=DATE-TIME`, optionally with a
  `TZID`; a floating value is assumed UTC) or a **duration** relative to
  the instance start (`TRIGGER:-PT15M`) or end
  (`TRIGGER;RELATED=END:PT10M`). Both positive and negative durations are
  accepted; `RELATED=START` is the default.
- `REPEAT` plus `DURATION` (the pair must appear together) fans one alarm
  out into `REPEAT + 1` reminders, spaced by `DURATION` (e.g.
  `REPEAT:2`/`DURATION:PT5M` fires at the initial trigger and twice more,
  five minutes apart). `REPEAT` must be non-negative and `DURATION`
  positive; a single alarm may not fan out beyond 1000 reminders.
- A **master** event's alarms apply to *every* expanded instance of the
  series; a `RECURRENCE-ID` override carries **its own** alarms and the
  master's alarms do not apply to it (an override without `VALARM`s alarms
  nobody). A cancelled occurrence produces no reminders.
- The window is half-open: a trigger exactly at `window_start` is included,
  one exactly at `window_end` is excluded.

Each reminder carries `uid`, `recurrence_id` (UTC, `null` for
non-recurring events), `summary` (of the instance it belongs to), `trigger`
(UTC, `Z`), and the alarm `description`. Reminders are stably sorted by
trigger time, then `UID`, then `RECURRENCE-ID` (a `null` `RECURRENCE-ID`
sorts first).

### Example

```bash
curl -s http://localhost:8000/alarms \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=2\r\nSUMMARY:Weekly sync\r\nBEGIN:VALARM\r\nTRIGGER:-PT15M\r\nACTION:DISPLAY\r\nDESCRIPTION:Join the call\r\nREPEAT:1\r\nDURATION:PT10M\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z"
  }'
```

The one master alarm applies to both Mondays and repeats once ten minutes
after the initial trigger:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "count": 4,
  "alarms": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-05T08:45:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-05T08:55:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-12T08:45:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-12T08:55:00Z",
      "description": "Join the call"
    }
  ]
}
```

### Errors (`422 Unprocessable Entity`)

Returned with a descriptive `detail` message for: unparsable iCalendar
text, invalid `RRULE` (e.g. `FREQ=BOGUS`), unknown `TZID`
(non-IANA name), missing `DTSTART`/`UID`, `RECURRENCE-ID` with
`RANGE=THISANDFUTURE` (unsupported), `window_start >= window_end`, and
expansions exceeding 10 000 occurrences per UID within the window.

`/availability` additionally returns `422` for: an unknown `timezone`
(non-IANA name), a working period with `end <= start` or crossing
midnight, an offset-bearing working time, an unknown weekday key, and a
non-positive `minimum_minutes`.

`/calendar-diff` reuses the same calendar and window validation, so it
returns `422` when `window_start >= window_end` or when either `old_ics` or
`new_ics` fails any of the `/expand` checks above.

`/alarms` additionally returns `422` for: an `ACTION` other than
`DISPLAY` (or a missing `ACTION`), a `DISPLAY` alarm without
`DESCRIPTION`, a missing or unsupported `TRIGGER` (only `DURATION` and
`DATE-TIME` values are supported; `RELATED` other than `START`/`END`, or
`RELATED` on an absolute trigger), exactly one of `REPEAT`/`DURATION`
present, a negative `REPEAT`, a non-positive alarm `DURATION`, a fan-out
beyond 1000 reminders, or an unknown `TZID` on an absolute trigger. All
`VALARM`s are validated eagerly, so an unsupported alarm yields `422`
even when its trigger lies outside the window; calendar-level failures
(unparsable ICS, bad window, invalid recurrence) reuse the `/expand`
checks.

## Semantics and limitations

- **Window overlap**: an instance is included when
  `start < window_end and end > window_start`; zero-duration instances when
  `window_start <= start < window_end`.
- **Overrides** replace the occurrence identified by `RECURRENCE-ID` even
  when the original falls outside the expansion window (e.g. moved into the
  window from outside). A `STATUS:CANCELLED` override removes the instance.
  An override without `DTEND`/`DURATION` inherits the master's duration.
- **Floating times** (no `Z`, no `TZID`) are interpreted as UTC.
- **All-day events** (`DATE`-valued `DTSTART`) start at local midnight and
  last one day when no `DTEND` is given.
- `RDATE`/`EXDATE` support `DATE` and `DATE-TIME` values (not `PERIOD`);
  `RECURRENCE-ID` with `RANGE=THISANDFUTURE` is not supported.
- A master event with `STATUS:CANCELLED` suppresses its whole series.

### Alarm semantics

- **Instance binding**: a master `VEVENT`'s `VALARM`s apply to every
  instance the recurrence engine expands from it; a `RECURRENCE-ID`
  override replaces the master's alarms for that instance with its own
  (an override with no `VALARM`s has no reminders). Cancelled instances
  and `EXDATE`d occurrences never produce reminders.
- **Triggers**: a duration-valued `TRIGGER` is added to the instance's
  DTSTART (default / `RELATED=START`) or DTEND (`RELATED=END`); signed
  durations point before (negative) or after (positive) the anchor. An
  absolute `TRIGGER;VALUE=DATE-TIME` is independent of the instance time
  and is normalized with its `Z`/`TZID` (floating = UTC).
- **Repeats**: `REPEAT:n` plus `DURATION:d` produces `n + 1` triggers at
  `base, base+d, …, base+n*d`. Reminders are filtered afterwards, so only
  the ones landing in the half-open window are returned.
- **Eager validation**: every `VALARM` in the supplied calendar is
  validated regardless of whether its event or trigger intersects the
  window; an unsupported or malformed alarm fails the whole request with
  `422`. Events without any `VALARM` simply contribute no reminders.

### Availability semantics

- **Working hours are local**: each weekday's periods are materialized per
  local date with `zoneinfo` and converted to UTC, so local times shift at
  DST transitions (the UTC timeline is built day by day — never by a fixed
  offset). Periods are clipped to the query window.
- **Busy intervals** are the expanded instances of every calendar,
  excluding `STATUS:CANCELLED` and non-positive-duration instances. Each is
  intersected with the working periods before merging, so events outside
  working time never remove free slots.
- **Half-open intervals** `[start, end)` are used throughout: two busy
  intervals that touch at an instant merge, and a busy event ending exactly
  when a free slot begins does not consume it.
- **Common free time** is the complement of the merged union of *all*
  calendars' busy intervals within the merged working periods; gaps between
  a day's working periods (e.g. lunch) and non-working weekdays are never
  returned.
- `minimum_minutes` is applied to slot length and is inclusive at the
  boundary; `minutes` is the integer UTC duration. Slots are sorted by
  `start` and serialized with the `Z` suffix.
- Working periods crossing local midnight are not supported (a range must
  start and end on the same local date).

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v          # run the test suite
uvicorn app.main:app --reload       # run locally on port 8000
```

## Project layout

```
app/main.py          FastAPI app: POST /expand, POST /alarms,
                     POST /calendar-diff, POST /availability, GET /health
app/expander.py      recurrence expansion, override merging, TZ/DST handling
app/alarms.py        VALARM parsing, REPEAT/DURATION fan-out, window filtering
app/diff.py          two-calendar instance diff by UID + RECURRENCE-ID
app/availability.py  working-period timeline, busy clipping/merge, free complement
tests/               key tests for all endpoints (recurrences, DST, 422s)
Dockerfile           python:3.12-slim image running uvicorn on port 8000
docker-compose.yml   single-command startup
```
