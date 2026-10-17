"""Tamper-evident, redacting and queryable audit trail of domain events.

Every :class:`AuditRecord` carries a contiguous sequence number, the hash of its predecessor and its own
SHA-256 hash over the canonical JSON of all its other fields, so edits, deletions and reordering of stored
records are detectable by :meth:`AuditLog.verify`. Storage is pluggable through :class:`AuditBackend`; an
in-memory backend is the default and :class:`JsonlAuditBackend` persists to an append-only JSON Lines file.

An :class:`AuditLog` is a ``hitl.core.EventSink``, so it can be subscribed to a ``hitl.core.EventBus``.
A log assumes it is the single writer of its backend.
"""

from __future__ import annotations

import json
import os
import threading
import types
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

from .core import (
    Clock,
    Event,
    IntegrityError,
    NotFoundError,
    SystemClock,
    ValidationError,
    canonical_json,
    fingerprint,
    new_id,
)

__all__ = [
    "DEFAULT_REDACT_KEYS",
    "GENESIS_HASH",
    "AuditBackend",
    "AuditLog",
    "AuditRecord",
    "InMemoryAuditBackend",
    "JsonlAuditBackend",
    "VerificationReport",
]

GENESIS_HASH = "0" * 64
DEFAULT_REDACT_KEYS: frozenset[str] = frozenset({"password", "secret", "token", "api_key", "ssn"})
DEFAULT_REDACTION = "[REDACTED]"

_REQUIRED_FIELDS = (
    "seq",
    "record_id",
    "kind",
    "subject_id",
    "actor",
    "data",
    "timestamp",
    "prev_hash",
    "hash",
)


def _normalize(value: Any) -> Any:
    """Return ``value`` as plain JSON types, exactly as it will look after a JSON round-trip."""
    return json.loads(canonical_json(value))


def _freeze(data: Mapping[str, Any]) -> Mapping[str, Any]:
    return types.MappingProxyType(_normalize(dict(data)))


@dataclass(frozen=True)
class AuditRecord:
    """One immutable, hash-chained entry of the audit trail.

    ``data`` has already been redacted and normalised to JSON types; ``hash`` is the SHA-256 hex digest of
    the canonical JSON of every other field (see :meth:`hash_payload`).
    """

    seq: int
    record_id: str
    kind: str
    subject_id: str
    actor: str
    data: Mapping[str, Any]
    timestamp: float
    prev_hash: str
    hash: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, Mapping):
            raise ValidationError("audit record data must be a mapping")
        object.__setattr__(self, "data", _freeze(self.data))

    def hash_payload(self) -> dict[str, Any]:
        """Every field except ``hash``: the exact value that is hashed."""
        return {
            "seq": self.seq,
            "record_id": self.record_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "actor": self.actor,
            "data": dict(self.data),
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "correlation_id": self.correlation_id,
        }

    def compute_hash(self) -> str:
        """Recompute the hash from the record's fields (ignores the stored ``hash``)."""
        return fingerprint(self.hash_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self.hash_payload()
        payload["data"] = _normalize(payload["data"])
        payload["hash"] = self.hash
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AuditRecord:
        if not isinstance(raw, Mapping):
            raise ValidationError("audit record must be a JSON object")
        missing = [name for name in _REQUIRED_FIELDS if name not in raw]
        if missing:
            raise ValidationError(f"audit record is missing fields: {', '.join(missing)}")
        seq = raw["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValidationError(f"audit record seq must be a non-negative integer, got {seq!r}")
        for name in ("record_id", "kind", "subject_id", "actor", "prev_hash", "hash"):
            if not isinstance(raw[name], str):
                raise ValidationError(f"audit record {name} must be a string")
        timestamp = raw["timestamp"]
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise ValidationError("audit record timestamp must be a number")
        correlation_id = raw.get("correlation_id")
        if correlation_id is not None and not isinstance(correlation_id, str):
            raise ValidationError("audit record correlation_id must be a string or null")
        return cls(
            seq=seq,
            record_id=raw["record_id"],
            kind=raw["kind"],
            subject_id=raw["subject_id"],
            actor=raw["actor"],
            data=raw["data"],
            timestamp=timestamp,
            prev_hash=raw["prev_hash"],
            hash=raw["hash"],
            correlation_id=correlation_id,
        )

    def to_json_line(self) -> str:
        """Canonical single-line JSON encoding, terminated by a newline."""
        return canonical_json(self.to_dict()) + "\n"


@dataclass(frozen=True)
class VerificationReport:
    """Outcome of a chain verification; truthy when ``ok``."""

    ok: bool
    checked: int
    first_bad_seq: int | None = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------------------------- backends


@runtime_checkable
class AuditBackend(Protocol):
    """Append-only record storage. Positions are 0-based and equal ``seq`` for an intact log.

    Backends store and return records verbatim; they never validate the chain (``AuditLog.verify`` does).
    """

    def append(self, record: AuditRecord) -> None: ...

    def __len__(self) -> int: ...

    def get(self, seq: int) -> AuditRecord: ...

    def iter(self, start: int = 0) -> Iterator[AuditRecord]: ...

    def last(self) -> AuditRecord | None: ...


class InMemoryAuditBackend:
    """Thread-safe list-backed storage; the default backend."""

    def __init__(self, records: Iterable[AuditRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = list(records)

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def get(self, seq: int) -> AuditRecord:
        if not isinstance(seq, int) or seq < 0:
            raise NotFoundError(f"no audit record at position {seq!r}")
        try:
            return self._records[seq]
        except IndexError:
            raise NotFoundError(f"no audit record at position {seq}") from None

    def iter(self, start: int = 0) -> Iterator[AuditRecord]:
        end = len(self._records)  # snapshot: records appended during iteration are not yielded
        for index in range(max(start, 0), end):
            yield self._records[index]

    def last(self) -> AuditRecord | None:
        records = self._records
        return records[-1] if records else None


class JsonlAuditBackend:
    """Append-only JSON Lines file; one canonical JSON record per line.

    Opening scans the file once to build an index of line byte offsets (records are not kept in memory)
    and to load the last record so the chain continues across restarts. ``get`` seeks to one offset;
    ``iter`` streams with its own file handle. A trailing line without a newline (a torn write) or an
    unparseable line raises :class:`IntegrityError`.
    """

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self._lock = threading.Lock()
        self._offsets: list[int] = []
        self._last: AuditRecord | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._size = self._scan()
        self._writer: IO[bytes] = open(self.path, "ab")  # long-lived; closed in close()
        self._reader: IO[bytes] = open(self.path, "rb")
        self._closed = False

    def _scan(self) -> int:
        position = 0
        last_line = b""
        with open(self.path, "rb") as fp:
            for line in fp:
                if not line.endswith(b"\n"):
                    raise IntegrityError(
                        f"{self.path}: torn trailing line at byte {position} (incomplete write?)"
                    )
                if line.strip():
                    self._offsets.append(position)
                    last_line = line
                position += len(line)
        if last_line:
            self._last = self._parse(last_line, len(self._offsets) - 1)
        return position

    def _parse(self, line: bytes, position: int) -> AuditRecord:
        try:
            return AuditRecord.from_dict(json.loads(line))
        except (ValueError, ValidationError) as exc:
            raise IntegrityError(
                f"{self.path}: unreadable audit record at position {position}: {exc}"
            ) from exc

    def append(self, record: AuditRecord) -> None:
        line = record.to_json_line().encode("utf-8")
        with self._lock:
            self._check_open()
            self._writer.write(line)
            self._writer.flush()
            if self.fsync:
                os.fsync(self._writer.fileno())
            self._offsets.append(self._size)
            self._size += len(line)
            self._last = record

    def __len__(self) -> int:
        return len(self._offsets)

    def get(self, seq: int) -> AuditRecord:
        if not isinstance(seq, int) or seq < 0 or seq >= len(self._offsets):
            raise NotFoundError(f"no audit record at position {seq!r}")
        with self._lock:
            self._check_open()
            self._reader.seek(self._offsets[seq])
            line = self._reader.readline()
        return self._parse(line, seq)

    def iter(self, start: int = 0) -> Iterator[AuditRecord]:
        start = max(start, 0)
        end = len(self._offsets)  # snapshot: later appends are not yielded
        if start >= end:
            return
        offset = self._offsets[start]
        with open(self.path, "rb") as fp:
            fp.seek(offset)
            position = start
            while position < end:
                line = fp.readline()
                if not line:
                    raise IntegrityError(f"{self.path}: file shrank while reading position {position}")
                if not line.strip():
                    continue
                yield self._parse(line, position)
                position += 1

    def last(self) -> AuditRecord | None:
        return self._last

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._writer.close()
                self._reader.close()

    def _check_open(self) -> None:
        if self._closed:
            raise IntegrityError(f"{self.path}: backend is closed")

    def __enter__(self) -> JsonlAuditBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------------------------- the log


def _redact(value: Any, keys: frozenset[str], marker: str) -> Any:
    if isinstance(value, Mapping):
        return {
            k: (marker if isinstance(k, str) and k.lower() in keys else _redact(v, keys, marker))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, keys, marker) for item in value]
    return value


def _kind_matches(kind: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return kind.startswith(pattern[:-1])
    return kind == pattern


class AuditLog:
    """Hash-chained audit trail over a pluggable backend; usable directly as an ``EventSink``."""

    def __init__(
        self,
        backend: AuditBackend | None = None,
        clock: Clock | None = None,
        redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS,
        redaction: str = DEFAULT_REDACTION,
    ) -> None:
        self._backend: AuditBackend = backend if backend is not None else InMemoryAuditBackend()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self.redact_keys: frozenset[str] = frozenset(k.lower() for k in redact_keys)
        self.redaction = redaction
        self._lock = threading.Lock()
        self._by_subject: dict[str, list[int]] = {}
        self._by_correlation: dict[str, list[int]] = {}
        last = self._backend.last()
        self._head_hash = last.hash if last is not None else GENESIS_HASH
        for position, record in enumerate(self._backend.iter(0)):
            self._index(record, position)

    # ----------------------------------------------------------------------------------------- writing

    def record(self, event: Event) -> AuditRecord:
        """Append ``event`` to the chain and return the stored record."""
        return self._append(event, None)

    __call__ = record

    def append(
        self,
        kind: str,
        subject_id: str,
        actor: str = "system",
        data: Mapping[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> AuditRecord:
        """Convenience: build an :class:`Event` (validating ``kind``) and record it."""
        event = Event(kind=kind, subject_id=subject_id, actor=actor, data=dict(data or {}))
        return self._append(event, correlation_id)

    def _append(self, event: Event, correlation_id: str | None) -> AuditRecord:
        data = _redact(dict(event.data), self.redact_keys, self.redaction)
        if correlation_id is None:
            for key in ("correlation_id", "run_id"):
                if data.get(key) is not None:
                    correlation_id = str(data[key])
                    break
        timestamp = float(event.timestamp) if event.timestamp > 0 else float(self._clock.now())
        with self._lock:
            seq = len(self._backend)
            unsigned = AuditRecord(
                seq=seq,
                record_id=new_id("aud"),
                kind=event.kind,
                subject_id=event.subject_id,
                actor=event.actor,
                data=data,
                timestamp=timestamp,
                prev_hash=self._head_hash,
                hash="",
                correlation_id=correlation_id,
            )
            record = replace(unsigned, hash=unsigned.compute_hash())
            self._backend.append(record)
            self._head_hash = record.hash
            self._index(record, seq)
        return record

    def _index(self, record: AuditRecord, position: int) -> None:
        self._by_subject.setdefault(record.subject_id, []).append(position)
        if record.correlation_id is not None:
            self._by_correlation.setdefault(record.correlation_id, []).append(position)

    # ----------------------------------------------------------------------------------------- reading

    @property
    def backend(self) -> AuditBackend:
        return self._backend

    @property
    def head_hash(self) -> str:
        """Hash of the newest record (``GENESIS_HASH`` when empty): an anchor to publish externally."""
        return self._head_hash

    def __len__(self) -> int:
        return len(self._backend)

    def __iter__(self) -> Iterator[AuditRecord]:
        return self._backend.iter(0)

    def get(self, seq: int) -> AuditRecord:
        return self._backend.get(seq)

    def last(self) -> AuditRecord | None:
        return self._backend.last()

    def query(
        self,
        *,
        subject_id: str | None = None,
        kind: str | None = None,
        actor: str | None = None,
        correlation_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditRecord]:
        """Records matching every given filter, in seq order, paginated.

        ``kind`` matches exactly, or as a prefix when it ends with ``*`` (``"approval.*"``).
        ``since`` is inclusive and ``until`` exclusive.
        """
        if not isinstance(limit, int) or limit < 0:
            raise ValidationError(f"limit must be a non-negative integer, got {limit!r}")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError(f"offset must be a non-negative integer, got {offset!r}")

        def matches(record: AuditRecord) -> bool:
            return (
                (subject_id is None or record.subject_id == subject_id)
                and (kind is None or _kind_matches(record.kind, kind))
                and (actor is None or record.actor == actor)
                and (correlation_id is None or record.correlation_id == correlation_id)
                and (since is None or record.timestamp >= since)
                and (until is None or record.timestamp < until)
            )

        results: list[AuditRecord] = []
        if limit == 0:
            return results
        skipped = 0
        for record in self._candidates(subject_id, correlation_id):
            if not matches(record):
                continue
            if skipped < offset:
                skipped += 1
                continue
            results.append(record)
            if len(results) >= limit:
                break
        return results

    def _candidates(self, subject_id: str | None, correlation_id: str | None) -> Iterator[AuditRecord]:
        with self._lock:
            indexed: list[list[int]] = []
            if subject_id is not None:
                indexed.append(list(self._by_subject.get(subject_id, ())))
            if correlation_id is not None:
                indexed.append(list(self._by_correlation.get(correlation_id, ())))
        if not indexed:
            yield from self._backend.iter(0)
            return
        positions = min(indexed, key=len)
        for position in positions:
            yield self._backend.get(position)

    def history(self, subject_id: str) -> list[AuditRecord]:
        """Every record of ``subject_id`` in seq order."""
        with self._lock:
            positions = list(self._by_subject.get(subject_id, ()))
        return [self._backend.get(position) for position in positions]

    # ------------------------------------------------------------------------------------ verification

    def verify(self, start: int = 0, end: int | None = None) -> VerificationReport:
        """Recompute hashes, chain links and seq contiguity for positions ``[start, end)``."""
        total = len(self._backend)
        if end is None or end > total:
            end = total
        if start < 0 or end < start:
            raise ValidationError(f"invalid verification range [{start}, {end})")
        checked = 0
        expected = start
        try:
            prev_hash = GENESIS_HASH if start == 0 else self._backend.get(start - 1).hash
            for record in self._backend.iter(start):
                if expected >= end:
                    break
                problem = None
                if record.seq != expected:
                    problem = f"sequence gap or reorder: expected seq {expected}, found {record.seq}"
                elif record.prev_hash != prev_hash:
                    problem = f"broken chain link at seq {expected}: prev_hash does not match previous record"
                elif record.compute_hash() != record.hash:
                    problem = f"hash mismatch at seq {expected}: record content was altered"
                if problem is not None:
                    return VerificationReport(False, checked, expected, problem)
                prev_hash = record.hash
                checked += 1
                expected += 1
        except (IntegrityError, NotFoundError) as exc:
            return VerificationReport(False, checked, expected, str(exc))
        if expected < end:
            return VerificationReport(False, checked, expected, f"record at position {expected} is missing")
        return VerificationReport(True, checked)

    def verify_or_raise(self, start: int = 0, end: int | None = None) -> VerificationReport:
        report = self.verify(start, end)
        if not report.ok:
            raise IntegrityError(f"audit chain invalid at seq {report.first_bad_seq}: {report.error}")
        return report

    def verify_against(self, anchor_hash: str, seq: int) -> VerificationReport:
        """Check that the chain up to ``seq`` is intact and the record at ``seq`` has ``anchor_hash``.

        Detects truncation and wholesale rewrites that plain :meth:`verify` cannot see.
        """
        if seq < 0:
            raise ValidationError(f"anchor seq must be non-negative, got {seq}")
        if seq >= len(self._backend):
            return VerificationReport(
                False, 0, len(self._backend), f"anchored record {seq} is missing (log truncated?)"
            )
        report = self.verify(0, seq + 1)
        if not report.ok:
            return report
        if self._backend.get(seq).hash != anchor_hash:
            return VerificationReport(
                False, report.checked, seq, f"record {seq} does not match the anchor hash"
            )
        return report

    # ---------------------------------------------------------------------------------- import / export

    def export_jsonl(self, fp_or_path: str | os.PathLike[str] | IO[str]) -> int:
        """Write every record as canonical JSON Lines; returns the number of records written."""
        if isinstance(fp_or_path, (str, os.PathLike)):
            with open(fp_or_path, "w", encoding="utf-8") as fp:
                return self._write_lines(fp)
        return self._write_lines(fp_or_path)

    def _write_lines(self, fp: IO[str]) -> int:
        count = 0
        for record in self._backend.iter(0):
            fp.write(record.to_json_line())
            count += 1
        return count

    @classmethod
    def import_jsonl(
        cls,
        path_or_lines: str | os.PathLike[str] | Iterable[str],
        verify: bool = True,
        *,
        clock: Clock | None = None,
        redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS,
        redaction: str = DEFAULT_REDACTION,
    ) -> AuditLog:
        """Load exported JSON Lines into a new in-memory log, raising IntegrityError if ``verify`` fails."""
        backend = InMemoryAuditBackend()
        if isinstance(path_or_lines, (str, os.PathLike)):
            with open(path_or_lines, encoding="utf-8") as fp:
                cls._load_lines(fp, backend)
        else:
            cls._load_lines(path_or_lines, backend)
        log = cls(backend, clock=clock, redact_keys=redact_keys, redaction=redaction)
        if verify:
            log.verify_or_raise()
        return log

    @staticmethod
    def _load_lines(lines: Iterable[str | bytes], backend: InMemoryAuditBackend) -> None:
        if isinstance(lines, (bytes, bytearray)):
            raise ValidationError("import_jsonl expects a path or an iterable of lines")
        for number, line in enumerate(lines, start=1):
            text = line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
            if not text.strip():
                continue
            try:
                backend.append(AuditRecord.from_dict(json.loads(text)))
            except (ValueError, ValidationError) as exc:
                raise IntegrityError(f"unreadable audit record on line {number}: {exc}") from exc
