"""Concurrency-conflict detection across named iCalendar calendars.

Every named calendar is expanded with the shared recurrence engine
(:func:`app.expander.expand_calendar`) over one UTC query window. Only
instances that are not ``STATUS:CANCELLED`` and have a strictly positive
duration participate; each interval is clipped to the half-open window
``[window_start, window_end)``.

The clipped intervals are swept over time as half-open ``[start, end)``
intervals: at any boundary instant, **ends are applied before starts**, so
two intervals that merely touch at an instant never overlap. Every
non-empty portion of the timeline on which the number of active instances
reaches ``minimum_concurrency`` (at least 2) is emitted as a conflict
segment, carrying the sorted participating instances. Adjacent segments are
merged only when their exact set of participants is identical (a gap keeps
them separate); the result is sorted by ``start``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import Instance, expand_calendar

UTC = dt.timezone.utc

# Sorts before any real RECURRENCE-ID, so a non-recurring instance
# (recurrence_id is None) comes first for the same calendar/UID.
_NO_RECURRENCE_ID = dt.datetime.min.replace(tzinfo=UTC)

# Sweep event kinds: at one instant, every end is applied before any start,
# honoring the half-open [start, end) semantics.
_END = 0
_START = 1


@dataclass(frozen=True)
class Participant:
    """An active instance together with the calendar name it came from."""

    calendar: str
    instance: Instance

    @property
    def identity(self) -> tuple[str, str, dt.datetime | None]:
        return (self.calendar, self.instance.uid, self.instance.recurrence_id)


@dataclass(frozen=True)
class ConflictSegment:
    """One UTC time range with at least ``minimum_concurrency`` participants."""

    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    participants: tuple[Participant, ...]

    @property
    def count(self) -> int:
        return len(self.participants)


def _participant_sort_key(participant: Participant) -> tuple[str, str, dt.datetime]:
    rid = participant.instance.recurrence_id
    return (participant.calendar, participant.instance.uid, rid or _NO_RECURRENCE_ID)


def find_conflicts(
    calendars: dict[str, str],
    window_start: dt.datetime,
    window_end: dt.datetime,
    minimum_concurrency: int,
) -> list[ConflictSegment]:
    """Sweep all named calendars and return the conflict segments.

    ``ExpansionError`` from any calendar propagates to the caller, which
    maps it to HTTP 422.
    """
    # (instant, kind, participant); ends (kind 0) sort before starts (1).
    events: list[tuple[dt.datetime, int, Participant]] = []
    for calendar_name, ics in calendars.items():
        for inst in expand_calendar(ics, window_start, window_end):
            if inst.status == "CANCELLED" or inst.end <= inst.start:
                continue
            start = max(inst.start, window_start)
            end = min(inst.end, window_end)
            if end <= start:
                continue
            participant = Participant(calendar=calendar_name, instance=inst)
            events.append((start, _START, participant))
            events.append((end, _END, participant))

    events.sort(key=lambda event: (event[0], event[1]))

    active: dict[tuple[str, str, dt.datetime | None], Participant] = {}
    raw: list[ConflictSegment] = []
    cursor = window_start

    for instant, kind, participant in events:
        # Snapshot the open portion [cursor, instant) before applying the
        # boundary change at ``instant``. Same-instant events are grouped
        # implicitly: instant == cursor skips the snapshot until the whole
        # group has been applied, so touching intervals never coexist.
        if instant > cursor and len(active) >= minimum_concurrency:
            raw.append(
                ConflictSegment(
                    start=cursor,
                    end=instant,
                    participants=tuple(
                        sorted(active.values(), key=_participant_sort_key)
                    ),
                )
            )
        if kind == _END:
            active.pop(participant.identity, None)
        else:
            active[participant.identity] = participant
        cursor = instant

    return _merge_segments(raw)


def _merge_segments(segments: list[ConflictSegment]) -> list[ConflictSegment]:
    """Sort by start and merge adjacent segments with identical participants.

    Two segments are joined only when one ends exactly where the next begins
    *and* both carry the same participant identities in the same order. A
    gap between them keeps the segments separate even if their participant
    sets agree.
    """
    ordered = sorted(segments, key=lambda segment: segment.start)
    merged: list[ConflictSegment] = []
    for segment in ordered:
        previous = merged[-1] if merged else None
        same_participants = (
            previous is not None
            and tuple(p.identity for p in previous.participants)
            == tuple(p.identity for p in segment.participants)
        )
        if previous is not None and previous.end == segment.start and same_participants:
            merged[-1] = ConflictSegment(
                start=previous.start,
                end=segment.end,
                participants=previous.participants,
            )
        else:
            merged.append(segment)
    return merged
