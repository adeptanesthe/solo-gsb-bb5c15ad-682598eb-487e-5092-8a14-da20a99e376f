"""Key tests for the /conflicts endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

import datetime as dt

from fastapi.testclient import TestClient

from app.conflicts import ConflictSegment, Participant, _merge_segments
from app.main import app
from app.expander import Instance

client = TestClient(app)

UTC = dt.timezone.utc
CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//conflict-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"

WINDOW_START = "2026-01-05T00:00:00Z"  # Monday
WINDOW_END = "2026-01-06T00:00:00Z"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def event_utc(uid: str, start: str, end: str, *extra: str) -> str:
    return event(f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{end}", *extra)


def conflicts(calendars=None, *, minimum_concurrency=2,
              window_start=WINDOW_START, window_end=WINDOW_END):
    return client.post(
        "/conflicts",
        json={
            "calendars": calendars or {},
            "window_start": window_start,
            "window_end": window_end,
            "minimum_concurrency": minimum_concurrency,
        },
    )


def participant_keys(segment):
    return [(p["calendar"], p["uid"], p["recurrence_id"])
            for p in segment["participants"]]


# ---------------------------------------------------------------------------
# Basic overlap detection
# ---------------------------------------------------------------------------

def test_no_calendars_returns_no_segments():
    resp = conflicts()
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["segments"] == []


def test_pairwise_overlap_returns_one_segment():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T120000Z"))
    bob = cal(event_utc("b", "20260105T110000Z", "20260105T130000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["start"] == "2026-01-05T11:00:00Z"
    assert segments[0]["end"] == "2026-01-05T12:00:00Z"
    assert segments[0]["count"] == 2
    assert participant_keys(segments[0]) == [
        ("alice", "a", None),
        ("bob", "b", None),
    ]


def test_three_way_overlap_reports_all_three_and_inner_segments():
    # a: 10-12, b: 11-13, c: 11:30-12:30
    a = cal(event_utc("a", "20260105T100000Z", "20260105T120000Z"))
    b = cal(event_utc("b", "20260105T110000Z", "20260105T130000Z"))
    c = cal(event_utc("c", "20260105T113000Z", "20260105T123000Z"))
    resp = conflicts({"alice": a, "bob": b, "carol": c})
    segments = resp.json()["segments"]
    # >= 2: 11:00-11:30 (a,b); 11:30-12:00 (a,b,c); 12:00-12:30 (b,c)
    assert [(s["start"], s["end"], s["count"]) for s in segments] == [
        ("2026-01-05T11:00:00Z", "2026-01-05T11:30:00Z", 2),
        ("2026-01-05T11:30:00Z", "2026-01-05T12:00:00Z", 3),
        ("2026-01-05T12:00:00Z", "2026-01-05T12:30:00Z", 2),
    ]
    assert participant_keys(segments[1]) == [
        ("alice", "a", None),
        ("bob", "b", None),
        ("carol", "c", None),
    ]


def test_threshold_above_overlap_yields_nothing():
    # The three-way overlap reaches only 3, so 4 yields no segment.
    a = cal(event_utc("a", "20260105T100000Z", "20260105T120000Z"))
    b = cal(event_utc("b", "20260105T110000Z", "20260105T130000Z"))
    c = cal(event_utc("c", "20260105T113000Z", "20260105T123000Z"))
    resp = conflicts({"alice": a, "bob": b, "carol": c}, minimum_concurrency=4)
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


def test_minimum_concurrency_equals_3_skips_double_segments():
    a = cal(event_utc("a", "20260105T100000Z", "20260105T120000Z"))
    b = cal(event_utc("b", "20260105T110000Z", "20260105T130000Z"))
    c = cal(event_utc("c", "20260105T113000Z", "20260105T123000Z"))
    resp = conflicts({"alice": a, "bob": b, "carol": c}, minimum_concurrency=3)
    segments = resp.json()["segments"]
    assert [(s["start"], s["end"]) for s in segments] == [
        ("2026-01-05T11:30:00Z", "2026-01-05T12:00:00Z"),
    ]


# ---------------------------------------------------------------------------
# Half-open semantics: ends before starts at the same instant
# ---------------------------------------------------------------------------

def test_touching_intervals_do_not_conflict():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T110000Z", "20260105T120000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.json()["segments"] == []


def test_end_and_start_at_same_instant_keeps_other_overlaps_open():
    # a: 10-11, b: 10-12, c: 11-12. At 11:00 a ends and c starts; ends are
    # applied first, so the snapshot at exactly 11:00 is empty/zero-length
    # and [11:00,12:00) keeps b+c at concurrency 2.
    a = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    b = cal(event_utc("b", "20260105T100000Z", "20260105T120000Z"))
    c = cal(event_utc("c", "20260105T110000Z", "20260105T120000Z"))
    resp = conflicts({"alice": a, "bob": b, "carol": c})
    segments = resp.json()["segments"]
    assert [(s["start"], s["end"], s["count"]) for s in segments] == [
        ("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", 2),
        ("2026-01-05T11:00:00Z", "2026-01-05T12:00:00Z", 2),
    ]
    # First segment is a+b, second is b+c — different participants, so they
    # stay separate despite being adjacent.
    assert participant_keys(segments[0]) == [("alice", "a", None), ("bob", "b", None)]
    assert participant_keys(segments[1]) == [("bob", "b", None), ("carol", "c", None)]


# ---------------------------------------------------------------------------
# Filtering and clipping
# ---------------------------------------------------------------------------

def test_cancelled_and_zero_duration_instances_are_ignored():
    alice = cal(
        event_utc("a", "20260105T100000Z", "20260105T120000Z", "STATUS:CANCELLED"),
        event_utc("z", "20260105T110000Z", "20260105T110000Z"),
    )
    bob = cal(event_utc("b", "20260105T100000Z", "20260105T120000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.json()["segments"] == []


def test_intervals_are_clipped_to_the_window():
    alice = cal(event_utc("a", "20260104T200000Z", "20260105T020000Z"))
    bob = cal(event_utc("b", "20260104T230000Z", "20260105T030000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    segments = resp.json()["segments"]
    assert [(s["start"], s["end"]) for s in segments] == [
        (WINDOW_START, "2026-01-05T02:00:00Z"),
    ]


def test_instance_ending_at_window_start_does_not_participate():
    alice = cal(event_utc("a", "20260104T230000Z", "20260105T000000Z"))
    bob = cal(event_utc("b", "20260105T000000Z", "20260105T010000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.json()["segments"] == []


def test_recurring_instances_are_expanded_with_identity():
    alice = cal(event(
        "UID:daily",
        "DTSTART:20260105T100000Z",
        "DTEND:20260105T110000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
    ))
    bob = cal(event_utc("b1", "20260105T103000Z", "20260105T113000Z"))
    resp = conflicts(
        {"alice": alice, "bob": bob},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-07T00:00:00Z",
    )
    segments = resp.json()["segments"]
    assert [(s["start"], s["end"]) for s in segments] == [
        ("2026-01-05T10:30:00Z", "2026-01-05T11:00:00Z"),
    ]
    assert participant_keys(segments[0]) == [
        ("alice", "daily", "2026-01-05T10:00:00Z"),
        ("bob", "b1", None),
    ]


def test_cancelled_recurrence_override_does_not_participate():
    alice = cal(
        event(
            "UID:daily",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:daily",
            "RECURRENCE-ID:20260105T100000Z",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "STATUS:CANCELLED",
        ),
    )
    bob = cal(event_utc("b1", "20260105T103000Z", "20260105T113000Z"))
    resp = conflicts(
        {"alice": alice, "bob": bob},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-07T00:00:00Z",
    )
    # Jan 5 occurrence is cancelled; the Jan 6 one does not meet Bob.
    assert resp.json()["segments"] == []


# ---------------------------------------------------------------------------
# Ordering, identity and response shape
# ---------------------------------------------------------------------------

def test_participants_sorted_by_calendar_then_uid_then_rid():
    zoe = cal(event_utc("z2", "20260105T100000Z", "20260105T120000Z"))
    amy = cal(
        event_utc("a2", "20260105T100000Z", "20260105T120000Z"),
        event_utc("a1", "20260105T100000Z", "20260105T120000Z"),
    )
    resp = conflicts({"zoe": zoe, "amy": amy})
    keys = participant_keys(resp.json()["segments"][0])
    assert keys == [
        ("amy", "a1", None),
        ("amy", "a2", None),
        ("zoe", "z2", None),
    ]


def test_segments_sorted_by_start():
    alice = cal(
        event_utc("a1", "20260105T090000Z", "20260105T100000Z"),
        event_utc("a2", "20260105T150000Z", "20260105T160000Z"),
    )
    bob = cal(
        event_utc("b1", "20260105T093000Z", "20260105T103000Z"),
        event_utc("b2", "20260105T153000Z", "20260105T163000Z"),
    )
    resp = conflicts({"alice": alice, "bob": bob})
    starts = [s["start"] for s in resp.json()["segments"]]
    assert starts == sorted(starts)
    assert starts == [
        "2026-01-05T09:30:00Z",
        "2026-01-05T15:30:00Z",
    ]


def test_participant_payload_is_instance_shape_with_calendar():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T120000Z",
                          "SUMMARY:Meet"))
    bob = cal(event_utc("b", "20260105T110000Z", "20260105T130000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    participant = resp.json()["segments"][0]["participants"][0]
    assert participant == {
        "calendar": "alice",
        "uid": "a",
        "recurrence_id": None,
        "summary": "Meet",
        "start": "2026-01-05T10:00:00Z",
        "end": "2026-01-05T12:00:00Z",
        "status": "CONFIRMED",
    }


def test_response_echoes_window_and_threshold():
    resp = conflicts(minimum_concurrency=3,
                     window_start="2026-02-01T00:00:00Z",
                     window_end="2026-02-02T00:00:00Z")
    assert resp.json() == {
        "window_start": "2026-02-01T00:00:00Z",
        "window_end": "2026-02-02T00:00:00Z",
        "minimum_concurrency": 3,
        "count": 0,
        "segments": [],
    }


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def test_threshold_below_two_returns_422():
    for bad in (1, 0, -2):
        resp = conflicts(minimum_concurrency=bad)
        assert resp.status_code == 422, bad


def test_missing_threshold_returns_422():
    resp = client.post(
        "/conflicts",
        json={
            "calendars": {},
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
        },
    )
    assert resp.status_code == 422


def test_inverted_window_returns_422():
    resp = conflicts(window_start=WINDOW_END, window_end=WINDOW_START)
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_equal_window_bounds_return_422():
    resp = conflicts(window_start=WINDOW_START, window_end=WINDOW_START)
    assert resp.status_code == 422


def test_invalid_ics_returns_422():
    resp = conflicts({"alice": "not an iCalendar document",
                      "bob": cal(event_utc("b", "20260105T100000Z",
                                           "20260105T110000Z"))})
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_unknown_tzid_inside_calendar_returns_422():
    ics = cal(event(
        "UID:a",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T100000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T110000",
    ))
    resp = conflicts({"alice": ics})
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Merge helper: adjacent segments merge only with identical participants
# ---------------------------------------------------------------------------

def _participant(name: str, uid: str, rid: dt.datetime | None = None) -> Participant:
    return Participant(
        calendar=name,
        instance=Instance(
            uid=uid,
            recurrence_id=rid,
            start=dt.datetime(2026, 1, 5, tzinfo=UTC),
            end=dt.datetime(2026, 1, 5, 1, tzinfo=UTC),
            summary=None,
            status="CONFIRMED",
        ),
    )


def _segment(start: str, end: str, participants) -> ConflictSegment:
    t0 = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    t1 = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    return ConflictSegment(start=t0, end=t1, participants=tuple(participants))


def test_merge_joins_adjacent_segments_with_identical_participants():
    a = _participant("alice", "x")
    b = _participant("bob", "y")
    raw = [
        _segment("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", [a, b]),
        _segment("2026-01-05T11:00:00Z", "2026-01-05T12:00:00Z", [a, b]),
    ]
    merged = _merge_segments(raw)
    assert len(merged) == 1
    assert merged[0].start.isoformat() == "2026-01-05T10:00:00+00:00"
    assert merged[0].end.isoformat() == "2026-01-05T12:00:00+00:00"


def test_merge_keeps_segments_separated_by_a_gap():
    a = _participant("alice", "x")
    b = _participant("bob", "y")
    raw = [
        _segment("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", [a, b]),
        _segment("2026-01-05T11:30:00Z", "2026-01-05T12:30:00Z", [a, b]),
    ]
    merged = _merge_segments(raw)
    assert len(merged) == 2


def test_merge_keeps_adjacent_segments_with_different_participants():
    a = _participant("alice", "x")
    b = _participant("bob", "y")
    c = _participant("carol", "z")
    raw = [
        _segment("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", [a, b]),
        _segment("2026-01-05T11:00:00Z", "2026-01-05T12:00:00Z", [b, c]),
    ]
    merged = _merge_segments(raw)
    assert len(merged) == 2
