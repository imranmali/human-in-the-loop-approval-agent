from __future__ import annotations

import asyncio
import dataclasses
import enum
import io
import json
import threading

import pytest

from hitl.audit import (
    GENESIS_HASH,
    AuditBackend,
    AuditLog,
    AuditRecord,
    InMemoryAuditBackend,
    JsonlAuditBackend,
    VerificationReport,
)
from hitl.core import (
    Event,
    EventBus,
    IntegrityError,
    ManualClock,
    NotFoundError,
    ValidationError,
    fingerprint,
)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(1000.0)


@pytest.fixture
def log(clock: ManualClock) -> AuditLog:
    return AuditLog(clock=clock)


def _fill(log: AuditLog, n: int, clock: ManualClock | None = None) -> list[AuditRecord]:
    records = []
    for i in range(n):
        records.append(log.append("approval.requested", f"apr_{i}", data={"i": i}))
        if clock is not None:
            clock.advance(1)
    return records


def _mem(log: AuditLog) -> list[AuditRecord]:
    backend = log.backend
    assert isinstance(backend, InMemoryAuditBackend)
    return backend._records


# ------------------------------------------------------------------------------------ hash chain


def test_first_record_links_to_genesis(log: AuditLog) -> None:
    record = log.append("approval.requested", "apr_1")
    assert record.seq == 0
    assert record.prev_hash == GENESIS_HASH == "0" * 64
    assert record.record_id.startswith("aud_")
    assert len(record.hash) == 64


def test_records_form_contiguous_chain(log: AuditLog) -> None:
    records = _fill(log, 5)
    assert [r.seq for r in records] == list(range(5))
    for prev, cur in zip(records, records[1:], strict=False):
        assert cur.prev_hash == prev.hash
    assert log.head_hash == records[-1].hash
    assert len(log) == 5
    assert list(log) == records


def test_hash_covers_every_field(log: AuditLog) -> None:
    record = log.append("approval.granted", "apr_1", actor="alice", data={"a": 1}, correlation_id="run_1")
    assert record.compute_hash() == record.hash
    payload = record.to_dict()
    payload.pop("hash")
    assert fingerprint(payload) == record.hash
    changes = {
        "seq": 7,
        "record_id": "aud_other",
        "kind": "approval.denied",
        "subject_id": "apr_2",
        "actor": "mallory",
        "data": {"a": 2},
        "timestamp": 1.5,
        "prev_hash": "f" * 64,
        "correlation_id": "run_2",
    }
    for name, value in changes.items():
        altered = dataclasses.replace(record, **{name: value})
        assert altered.compute_hash() != record.hash, name


def test_record_round_trips_through_dict(log: AuditLog) -> None:
    record = log.append("run.started", "run_1", data={"nested": {"x": [1, 2]}, "run_id": "run_1"})
    again = AuditRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert again == record
    assert again.compute_hash() == record.hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("hash"),
        lambda d: d.pop("data"),
        lambda d: d.update(seq=-1),
        lambda d: d.update(seq="1"),
        lambda d: d.update(kind=5),
        lambda d: d.update(timestamp="now"),
        lambda d: d.update(correlation_id=3),
        lambda d: d.update(data=[1]),
    ],
)
def test_from_dict_rejects_malformed(log: AuditLog, mutate) -> None:
    raw = log.append("run.started", "run_1").to_dict()
    mutate(raw)
    with pytest.raises(ValidationError):
        AuditRecord.from_dict(raw)


def test_from_dict_rejects_non_mapping() -> None:
    with pytest.raises(ValidationError):
        AuditRecord.from_dict([1, 2])  # type: ignore[arg-type]


def test_record_data_is_read_only_and_normalised(log: AuditLog) -> None:
    class Color(enum.Enum):
        RED = "red"

    record = log.append("run.started", "run_1", data={"color": Color.RED, "tags": ("a", "b"), "s": {2, 1}})
    assert dict(record.data) == {"color": "red", "tags": ["a", "b"], "s": [1, 2]}
    with pytest.raises(TypeError):
        record.data["color"] = "blue"  # type: ignore[index]
    assert log.verify().ok


# ------------------------------------------------------------------------------------ recording events


def test_event_timestamp_is_preserved(log: AuditLog) -> None:
    record = log.record(Event("approval.requested", "apr_1", data={}, timestamp=42.5))
    assert record.timestamp == 42.5


def test_missing_timestamp_uses_clock(log: AuditLog, clock: ManualClock) -> None:
    clock.advance(5)
    record = log.record(Event("approval.requested", "apr_1"))
    assert record.timestamp == 1005.0


def test_event_fields_are_recorded(log: AuditLog) -> None:
    record = log(Event("approval.granted", "apr_9", actor="bob", data={"note": "ok"}))
    assert (record.kind, record.subject_id, record.actor, dict(record.data)) == (
        "approval.granted",
        "apr_9",
        "bob",
        {"note": "ok"},
    )


def test_subscribed_to_event_bus(log: AuditLog) -> None:
    bus = EventBus()
    bus.subscribe(log)
    for kind in ("approval.requested", "approval.granted", "run.resumed"):
        bus.emit(Event(kind, "apr_1", actor="alice"))
    assert [r.kind for r in log] == ["approval.requested", "approval.granted", "run.resumed"]
    assert log.verify().ok


@pytest.mark.parametrize("kind", ["Bad Kind", "", "UPPER"])
def test_invalid_kind_is_rejected(log: AuditLog, kind: str) -> None:
    with pytest.raises(ValidationError):
        log.append(kind, "apr_1")
    assert len(log) == 0
    assert log.head_hash == GENESIS_HASH


def test_correlation_from_run_id(log: AuditLog) -> None:
    record = log.record(Event("run.started", "run_7", data={"run_id": "run_7"}))
    assert record.correlation_id == "run_7"


def test_correlation_key_wins_over_run_id(log: AuditLog) -> None:
    record = log.append("run.started", "apr_1", data={"run_id": "run_7", "correlation_id": "corr_1"})
    assert record.correlation_id == "corr_1"


def test_explicit_correlation_and_none(log: AuditLog) -> None:
    explicit = log.append("run.started", "apr_1", data={"run_id": "run_7"}, correlation_id="corr_x")
    numeric = log.append("run.started", "apr_1", data={"run_id": 12})
    absent = log.append("run.started", "apr_1")
    assert explicit.correlation_id == "corr_x"
    assert numeric.correlation_id == "12"
    assert absent.correlation_id is None


# ------------------------------------------------------------------------------------ concurrency


def test_concurrent_thread_appends(log: AuditLog) -> None:
    threads_n, per_thread = 8, 50
    barrier = threading.Barrier(threads_n)

    def worker(t: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            log.append("approval.requested", f"apr_{t}", data={"i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(log) == threads_n * per_thread
    assert [r.seq for r in log] == list(range(threads_n * per_thread))
    report = log.verify()
    assert report.ok and report.checked == threads_n * per_thread
    assert all(len(log.history(f"apr_{t}")) == per_thread for t in range(threads_n))


def test_concurrent_thread_appends_jsonl(tmp_path, clock: ManualClock) -> None:
    backend = JsonlAuditBackend(tmp_path / "audit.jsonl")
    log = AuditLog(backend, clock=clock)

    def worker(t: int) -> None:
        for i in range(25):
            log.append("approval.requested", f"apr_{t}", data={"i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    backend.close()
    reopened = AuditLog(JsonlAuditBackend(tmp_path / "audit.jsonl"), clock=clock)
    assert len(reopened) == 150
    assert reopened.verify().ok


async def test_async_tasks_append_concurrently(log: AuditLog) -> None:
    async def producer(t: int) -> None:
        for i in range(20):
            if i % 2:
                await asyncio.to_thread(log.record, Event("run.step", f"run_{t}", data={"i": i}))
            else:
                log.record(Event("run.step", f"run_{t}", data={"i": i}))
            await asyncio.sleep(0)

    await asyncio.gather(*(producer(t) for t in range(10)))
    assert len(log) == 200
    assert log.verify().ok


# ------------------------------------------------------------------------------------ redaction


def test_nested_keys_are_redacted(log: AuditLog) -> None:
    data = {
        "Password": "hunter2",
        "user": "alice",
        "items": [{"api_key": "k-123", "name": "svc"}, "plain"],
        "deep": {"inner": {"TOKEN": "t", "ssn": "123-45-6789", "keep": 1}},
        "secret": {"whole": "subtree"},
    }
    record = log.append("approval.requested", "apr_1", data=data)
    assert dict(record.data) == {
        "Password": "[REDACTED]",
        "user": "alice",
        "items": [{"api_key": "[REDACTED]", "name": "svc"}, "plain"],
        "deep": {"inner": {"TOKEN": "[REDACTED]", "ssn": "[REDACTED]", "keep": 1}},
        "secret": "[REDACTED]",
    }
    assert data["Password"] == "hunter2"  # caller's mapping is not mutated
    assert "hunter2" not in json.dumps(record.to_dict())


def test_originals_never_reach_storage(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "audit.jsonl"
    with JsonlAuditBackend(path) as backend:
        log = AuditLog(backend, clock=clock)
        log.append("approval.requested", "apr_1", data={"creds": {"password": "s3cr3t-value"}})
        assert log.verify().ok
    assert b"s3cr3t-value" not in path.read_bytes()
    assert AuditLog(JsonlAuditBackend(path)).verify().ok


def test_custom_redaction_settings(clock: ManualClock) -> None:
    log = AuditLog(clock=clock, redact_keys={"Card"}, redaction="***")
    record = log.append("pay.made", "pay_1", data={"card": "4111", "password": "visible", "x": {"CARD": 1}})
    assert dict(record.data) == {"card": "***", "password": "visible", "x": {"CARD": "***"}}


# ------------------------------------------------------------------------------------ verification


def test_intact_log_verifies(log: AuditLog) -> None:
    _fill(log, 10)
    report = log.verify()
    assert report == VerificationReport(True, 10, None, None)
    assert bool(report)
    assert log.verify_or_raise().ok


def test_empty_log_verifies(log: AuditLog) -> None:
    assert log.verify() == VerificationReport(True, 0)
    assert log.head_hash == GENESIS_HASH


def test_edited_record_detected_in_memory(log: AuditLog) -> None:
    _fill(log, 6)
    records = _mem(log)
    records[3] = dataclasses.replace(records[3], data={"i": 999})
    report = log.verify()
    assert not report.ok
    assert report.first_bad_seq == 3
    assert report.checked == 3
    assert "hash mismatch" in (report.error or "")


def test_deleted_record_detected(log: AuditLog) -> None:
    _fill(log, 6)
    del _mem(log)[2]
    report = log.verify()
    assert (report.ok, report.first_bad_seq) == (False, 2)


def test_reordered_records_detected(log: AuditLog) -> None:
    _fill(log, 6)
    records = _mem(log)
    records[1], records[2] = records[2], records[1]
    report = log.verify()
    assert (report.ok, report.first_bad_seq) == (False, 1)


def test_forged_hash_detected_by_next_link(log: AuditLog) -> None:
    _fill(log, 5)
    records = _mem(log)
    forged = dataclasses.replace(records[2], actor="mallory")
    records[2] = dataclasses.replace(forged, hash=forged.compute_hash())
    report = log.verify()
    assert (report.ok, report.first_bad_seq) == (False, 3)
    assert "chain link" in (report.error or "")


def test_range_verification(log: AuditLog) -> None:
    _fill(log, 10)
    records = _mem(log)
    records[1] = dataclasses.replace(records[1], data={"i": -1})
    assert log.verify(4, 8) == VerificationReport(True, 4)
    assert log.verify(0, 1).ok
    assert log.verify(0, 2).first_bad_seq == 1
    assert log.verify(5).checked == 5
    with pytest.raises(ValidationError):
        log.verify(-1)
    with pytest.raises(ValidationError):
        log.verify(5, 2)


def test_range_verification_links_to_previous_record(log: AuditLog) -> None:
    _fill(log, 6)
    records = _mem(log)
    records[4] = dataclasses.replace(records[4], prev_hash="a" * 64)
    assert log.verify(4).first_bad_seq == 4


def test_raising_verification(log: AuditLog) -> None:
    _fill(log, 4)
    del _mem(log)[1]
    with pytest.raises(IntegrityError, match="seq 1"):
        log.verify_or_raise()


def test_verify_end_is_clamped(log: AuditLog) -> None:
    _fill(log, 3)
    report = log.verify(0, 10)
    assert report.ok and report.checked == 3


# ------------------------------------------------------------------------------------ anchors


def test_anchor_matches_after_growth(log: AuditLog) -> None:
    _fill(log, 4)
    anchor, anchor_seq = log.head_hash, len(log) - 1
    _fill(log, 3)
    assert log.verify_against(anchor, anchor_seq).ok
    assert log.head_hash != anchor


def test_anchor_detects_truncation(log: AuditLog) -> None:
    _fill(log, 5)
    anchor, anchor_seq = log.head_hash, 4
    del _mem(log)[3:]
    report = log.verify_against(anchor, anchor_seq)
    assert not report.ok
    assert report.first_bad_seq == 3
    assert log.verify().ok  # plain verification cannot see tail truncation


def test_anchor_detects_rewritten_chain(clock: ManualClock) -> None:
    original = AuditLog(clock=clock)
    _fill(original, 3)
    anchor = original.head_hash
    rewritten = AuditLog(clock=clock)
    _fill(rewritten, 3)  # fresh record ids -> different, internally consistent chain
    assert rewritten.verify().ok
    report = rewritten.verify_against(anchor, 2)
    assert (report.ok, report.first_bad_seq) == (False, 2)
    with pytest.raises(ValidationError):
        rewritten.verify_against(anchor, -1)


# ------------------------------------------------------------------------------------ queries


@pytest.fixture
def populated(clock: ManualClock) -> AuditLog:
    log = AuditLog(clock=clock)
    log.append("approval.requested", "apr_1", actor="agent", data={"run_id": "run_a"})  # t=1000
    clock.advance(10)
    log.append("approval.granted", "apr_1", actor="alice", data={"run_id": "run_a"})  # t=1010
    clock.advance(10)
    log.append("run.started", "run_a", actor="agent", data={"run_id": "run_a"})  # t=1020
    clock.advance(10)
    log.append("approval.requested", "apr_2", actor="agent", data={"run_id": "run_b"})  # t=1030
    clock.advance(10)
    log.append("approval.denied", "apr_2", actor="bob", data={"run_id": "run_b"})  # t=1040
    return log


def test_query_kind_prefix(populated: AuditLog) -> None:
    kinds = [r.kind for r in populated.query(kind="approval.*")]
    assert kinds == ["approval.requested", "approval.granted", "approval.requested", "approval.denied"]
    assert [r.seq for r in populated.query(kind="approval.requested")] == [0, 3]
    assert populated.query(kind="approval") == []
    assert len(populated.query(kind="*")) == 5


def test_query_combined_filters(populated: AuditLog) -> None:
    assert [r.seq for r in populated.query(subject_id="apr_1", actor="alice")] == [1]
    assert [r.seq for r in populated.query(correlation_id="run_a", actor="agent")] == [0, 2]
    assert [r.seq for r in populated.query(subject_id="apr_2", correlation_id="run_b")] == [3, 4]
    assert populated.query(subject_id="apr_1", correlation_id="run_b") == []
    assert [r.seq for r in populated.query(since=1010, until=1040)] == [1, 2, 3]
    assert [r.seq for r in populated.query(kind="approval.*", since=1030)] == [3, 4]
    assert populated.query(subject_id="missing") == []
    assert populated.query(correlation_id="missing") == []


def test_query_pagination(populated: AuditLog) -> None:
    assert [r.seq for r in populated.query(limit=2)] == [0, 1]
    assert [r.seq for r in populated.query(limit=2, offset=2)] == [2, 3]
    assert [r.seq for r in populated.query(limit=2, offset=4)] == [4]
    assert populated.query(limit=2, offset=10) == []
    assert populated.query(limit=0) == []
    assert [r.seq for r in populated.query(kind="approval.*", limit=1, offset=1)] == [1]
    assert [r.seq for r in populated.query(correlation_id="run_a", limit=1, offset=1)] == [1]


@pytest.mark.parametrize("kwargs", [{"limit": -1}, {"offset": -1}, {"limit": 1.5}])
def test_query_invalid_pagination(populated: AuditLog, kwargs) -> None:
    with pytest.raises(ValidationError):
        populated.query(**kwargs)


def test_subject_history(populated: AuditLog) -> None:
    assert [r.kind for r in populated.history("apr_2")] == ["approval.requested", "approval.denied"]
    assert populated.history("nobody") == []


# ------------------------------------------------------------------------------------ backends


def test_in_memory_default_backend(log: AuditLog) -> None:
    assert isinstance(log.backend, InMemoryAuditBackend)
    assert isinstance(log.backend, AuditBackend)
    assert log.backend.last() is None
    record = log.append("run.started", "run_1")
    assert log.backend.last() == record == log.last() == log.get(0)


def test_jsonl_backend_satisfies_protocol(tmp_path) -> None:
    with JsonlAuditBackend(tmp_path / "a.jsonl") as backend:
        assert isinstance(backend, AuditBackend)
        assert len(backend) == 0 and backend.last() is None
        assert list(backend.iter()) == []


def test_missing_position_raises(tmp_path, log: AuditLog) -> None:
    _fill(log, 2)
    for seq in (2, 100, -1):
        with pytest.raises(NotFoundError):
            log.get(seq)
    with JsonlAuditBackend(tmp_path / "a.jsonl") as backend:
        with pytest.raises(NotFoundError):
            backend.get(0)


def test_custom_backend_is_pluggable(clock: ManualClock) -> None:
    class ListBackend:
        def __init__(self) -> None:
            self.items: list[AuditRecord] = []

        def append(self, record: AuditRecord) -> None:
            self.items.append(record)

        def __len__(self) -> int:
            return len(self.items)

        def get(self, seq: int) -> AuditRecord:
            if not 0 <= seq < len(self.items):
                raise NotFoundError(seq)
            return self.items[seq]

        def iter(self, start: int = 0):
            yield from self.items[start:]

        def last(self) -> AuditRecord | None:
            return self.items[-1] if self.items else None

    backend = ListBackend()
    assert isinstance(backend, AuditBackend)
    log = AuditLog(backend, clock=clock)
    _fill(log, 3)
    assert len(backend.items) == 3 and log.verify().ok


# ------------------------------------------------------------------------------------ jsonl durability


def test_jsonl_reopen_continues_chain(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "logs" / "audit.jsonl"
    with JsonlAuditBackend(path, fsync=True) as backend:
        first = AuditLog(backend, clock=clock)
        _fill(first, 3)
        head = first.head_hash
    with JsonlAuditBackend(path) as backend:
        second = AuditLog(backend, clock=clock)
        assert len(second) == 3 and second.head_hash == head
        record = second.append("run.resumed", "run_1")
        assert record.seq == 3 and record.prev_hash == head
        assert second.verify() == VerificationReport(True, 4)
    assert len(path.read_text().splitlines()) == 4


def test_jsonl_get_and_iter_stream_from_file(tmp_path, clock: ManualClock) -> None:
    with JsonlAuditBackend(tmp_path / "a.jsonl") as backend:
        log = AuditLog(backend, clock=clock)
        records = _fill(log, 5)
        assert backend.get(3) == records[3]
        assert list(backend.iter(2)) == records[2:]
        assert list(backend.iter(10)) == []
        iterator = backend.iter(0)
        assert next(iterator) == records[0]
        log.append("run.started", "run_late")  # appended mid-iteration: not yielded
        assert list(iterator) == records[1:]
        assert not hasattr(backend, "_records")


def test_jsonl_indexes_rebuilt_on_reopen(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "a.jsonl"
    with JsonlAuditBackend(path) as backend:
        log = AuditLog(backend, clock=clock)
        log.append("approval.requested", "apr_1", data={"run_id": "run_a"})
        log.append("run.started", "run_b", data={"run_id": "run_b"})
        log.append("approval.granted", "apr_1", data={"run_id": "run_a"})
    with JsonlAuditBackend(path) as backend:
        log = AuditLog(backend, clock=clock)
        assert [r.seq for r in log.history("apr_1")] == [0, 2]
        assert [r.seq for r in log.query(correlation_id="run_a")] == [0, 2]
        assert [r.kind for r in log.query(kind="approval.*")] == ["approval.requested", "approval.granted"]


def _write_and_close(path, clock: ManualClock, n: int = 5) -> None:
    with JsonlAuditBackend(path) as backend:
        _fill(AuditLog(backend, clock=clock), n)


def test_jsonl_tampered_line_detected(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "a.jsonl"
    _write_and_close(path, clock)
    lines = path.read_text().splitlines(keepends=True)
    raw = json.loads(lines[2])
    raw["data"] = {"i": 12345, "approved": True}
    lines[2] = json.dumps(raw) + "\n"
    path.write_text("".join(lines))
    with JsonlAuditBackend(path) as backend:
        report = AuditLog(backend).verify()
    assert (report.ok, report.first_bad_seq) == (False, 2)


def test_jsonl_deleted_and_reordered_lines_detected(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "a.jsonl"
    _write_and_close(path, clock)
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:1] + lines[2:]))
    with JsonlAuditBackend(path) as backend:
        assert AuditLog(backend).verify().first_bad_seq == 1
    path.write_text("".join([lines[0], lines[1], lines[3], lines[2], lines[4]]))
    with JsonlAuditBackend(path) as backend:
        assert AuditLog(backend).verify().first_bad_seq == 2


def test_jsonl_torn_trailing_line(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "a.jsonl"
    _write_and_close(path, clock, 2)
    with open(path, "a") as fp:
        fp.write('{"seq": 2, "record')
    with pytest.raises(IntegrityError, match="torn"):
        JsonlAuditBackend(path)


def test_jsonl_unparseable_line(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "a.jsonl"
    _write_and_close(path, clock, 3)
    lines = path.read_text().splitlines(keepends=True)
    lines[1] = "not json at all\n"
    path.write_text("".join(lines))
    with JsonlAuditBackend(path) as backend:
        with pytest.raises(IntegrityError, match="position 1"):
            backend.get(1)
        with pytest.raises(IntegrityError):
            AuditLog(backend)  # index rebuild streams every record
    lines[2] = "{broken\n"
    path.write_text("".join(lines))
    with pytest.raises(IntegrityError):
        JsonlAuditBackend(path)  # the last record is parsed on open


def test_jsonl_verify_reports_unreadable_line(tmp_path, clock: ManualClock) -> None:
    path = tmp_path / "a.jsonl"
    with JsonlAuditBackend(path) as backend:
        log = AuditLog(backend, clock=clock)
        _fill(log, 4)
        lines = path.read_text().splitlines(keepends=True)
        lines[1] = lines[1].replace('"seq":1', '"seq":"x"')
        path.write_text("".join(lines))
        report = log.verify()
    assert (report.ok, report.first_bad_seq) == (False, 1)


def test_jsonl_closed_backend_rejects_writes(tmp_path) -> None:
    backend = JsonlAuditBackend(tmp_path / "a.jsonl")
    backend.close()
    backend.close()
    with pytest.raises(IntegrityError):
        AuditLog(backend).append("run.started", "run_1")


# ------------------------------------------------------------------------------------ export / import


def test_export_import_round_trip_path(tmp_path, populated: AuditLog) -> None:
    out = tmp_path / "export.jsonl"
    assert populated.export_jsonl(out) == 5
    imported = AuditLog.import_jsonl(out)
    assert list(imported) == list(populated)
    assert imported.head_hash == populated.head_hash
    assert [r.seq for r in imported.query(correlation_id="run_b")] == [3, 4]


def test_export_import_round_trip_stream(populated: AuditLog) -> None:
    buffer = io.StringIO()
    populated.export_jsonl(buffer)
    lines = buffer.getvalue().splitlines(keepends=True) + ["\n"]
    imported = AuditLog.import_jsonl(lines)
    assert imported.head_hash == populated.head_hash
    assert AuditLog.import_jsonl([line.encode() for line in lines]).head_hash == populated.head_hash
    record = imported.append("run.finished", "run_a")
    assert record.seq == 5 and imported.verify().ok


def test_import_rejects_tampered_data(populated: AuditLog) -> None:
    buffer = io.StringIO()
    populated.export_jsonl(buffer)
    lines = buffer.getvalue().splitlines()
    lines[1] = lines[1].replace('"actor":"alice"', '"actor":"mallory"')
    with pytest.raises(IntegrityError, match="seq 1"):
        AuditLog.import_jsonl(lines)


def test_import_without_verification(populated: AuditLog) -> None:
    buffer = io.StringIO()
    populated.export_jsonl(buffer)
    lines = buffer.getvalue().splitlines()
    del lines[2]
    imported = AuditLog.import_jsonl(lines, verify=False)
    assert len(imported) == 4
    assert imported.verify().first_bad_seq == 2


def test_import_rejects_unreadable_lines() -> None:
    with pytest.raises(IntegrityError, match="line 1"):
        AuditLog.import_jsonl(["{not json"])
    with pytest.raises(IntegrityError):
        AuditLog.import_jsonl(['{"seq": 0}'])
    with pytest.raises(ValidationError):
        AuditLog.import_jsonl(b"raw bytes")  # type: ignore[arg-type]
