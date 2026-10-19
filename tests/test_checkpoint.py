"""Tests for hitl.checkpoint (pause & resume with validated context)."""

from __future__ import annotations

import asyncio
import json

import pytest

from hitl.checkpoint import (
    MISSING,
    Checkpoint,
    CheckpointPage,
    CheckpointStatus,
    CheckpointStore,
    FieldSpec,
    InMemoryCheckpointStore,
    InputSchema,
    PauseResumeManager,
    ResumedContext,
    SchemaValidationError,
)
from hitl.core import (
    Event,
    ExpiredError,
    IntegrityError,
    ManualClock,
    NotFoundError,
    StateError,
    ValidationError,
    fingerprint,
)

# ----------------------------------------------------------------------------------------------- fixtures


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(100.0)


@pytest.fixture
def events() -> Recorder:
    return Recorder()


@pytest.fixture
def store() -> InMemoryCheckpointStore:
    return InMemoryCheckpointStore()


@pytest.fixture
def mgr(store: InMemoryCheckpointStore, clock: ManualClock, events: Recorder) -> PauseResumeManager:
    return PauseResumeManager(store=store, clock=clock, event_sink=events)


def decision_schema() -> InputSchema:
    return InputSchema(
        {
            "decision": FieldSpec(str, choices=["approve", "reject"], description="final call"),
            "amount": FieldSpec(int, min=1, max=100),
            "note": FieldSpec(str, required=False, max=20),
            "priority": FieldSpec(str, default="low"),
        }
    )


# ---------------------------------------------------------------------------------------------------- pause


async def test_pause_creates_active_checkpoint(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("run-1", "review", {"amount": 10})
    assert cp.id.startswith("ckpt_")
    assert cp.status is CheckpointStatus.ACTIVE
    assert cp.version == 1
    assert cp.created_at == 100.0
    assert cp.expires_at is None
    assert cp.state_fingerprint == fingerprint({"amount": 10})
    fetched = await mgr.get(cp.id)
    assert fetched.to_dict() == cp.to_dict()


@pytest.mark.parametrize(("run_id", "step"), [("", "review"), ("run", ""), ("   ", "review"), (None, "x")])
async def test_pause_rejects_empty_run_or_step(
    mgr: PauseResumeManager, store: InMemoryCheckpointStore, run_id: object, step: object
) -> None:
    with pytest.raises(ValidationError):
        await mgr.pause(run_id, step, {})  # type: ignore[arg-type]
    assert len(store) == 0


async def test_pause_rejects_non_mapping_state(mgr: PauseResumeManager) -> None:
    with pytest.raises(ValidationError):
        await mgr.pause("run", "step", [1, 2])  # type: ignore[arg-type]


async def test_pause_publishes_event(mgr: PauseResumeManager, events: Recorder) -> None:
    cp = await mgr.pause("run-1", "review", {"a": 1}, input_schema=decision_schema(), ttl=5)
    (ev,) = events.of("checkpoint.paused")
    assert ev.subject_id == cp.id
    assert ev.timestamp == 100.0
    assert ev.data["run_id"] == "run-1"
    assert ev.data["step"] == "review"
    assert ev.data["state_fingerprint"] == cp.state_fingerprint
    assert ev.data["expires_at"] == 105.0
    assert ev.data["input_fields"] == ["decision", "amount", "note", "priority"]


async def test_explicit_ttl_sets_expiry(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {}, ttl=60)
    assert cp.expires_at == 160.0


async def test_default_ttl_applies() -> None:
    clock = ManualClock(0)
    mgr = PauseResumeManager(clock=clock, default_ttl=30)
    cp = await mgr.pause("r", "s", {})
    assert cp.expires_at == 30.0
    cp2 = await mgr.pause("r", "s", {}, ttl=5)
    assert cp2.expires_at == 5.0


async def test_no_ttl_never_expires(mgr: PauseResumeManager, clock: ManualClock) -> None:
    cp = await mgr.pause("r", "s", {})
    assert cp.expires_at is None
    clock.advance(10**9)
    ctx = await mgr.resume(cp.id, actor="alice")
    assert ctx.resumed_at == clock.now()


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), "10", True])
async def test_invalid_ttl_rejected(mgr: PauseResumeManager, ttl: object) -> None:
    with pytest.raises(ValidationError):
        await mgr.pause("r", "s", {}, ttl=ttl)  # type: ignore[arg-type]


def test_invalid_default_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        PauseResumeManager(default_ttl=0)


@pytest.mark.parametrize(
    ("state", "path"),
    [
        ({"tags": {"a", "b"}}, "state.tags"),
        ({"obj": object()}, "state.obj"),
        ({"nested": {1: "x"}}, "state.nested"),
        ({"items": [1, float("nan")]}, "state.items[1]"),
    ],
)
async def test_non_serializable_state_rejected(
    mgr: PauseResumeManager, store: InMemoryCheckpointStore, state: dict, path: str
) -> None:
    with pytest.raises(ValidationError, match=r"^" + path.replace("[", r"\[").replace("]", r"\]")):
        await mgr.pause("r", "s", state)
    assert len(store) == 0


async def test_non_serializable_metadata_rejected(mgr: PauseResumeManager) -> None:
    with pytest.raises(ValidationError):
        await mgr.pause("r", "s", {}, metadata={"x": object()})


async def test_tuples_are_normalized_to_lists(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {"pair": (1, 2)})
    assert cp.state == {"pair": [1, 2]}


async def test_caller_mutation_does_not_affect_checkpoint(mgr: PauseResumeManager) -> None:
    state = {"amount": 10, "items": [1, 2]}
    cp = await mgr.pause("r", "s", state)
    state["amount"] = 999
    state["items"].append(3)
    cp.state["amount"] = 555
    cp.state["items"].clear()
    stored = await mgr.get(cp.id)
    assert stored.state == {"amount": 10, "items": [1, 2]}
    ctx = await mgr.resume(cp.id, actor="alice")
    assert ctx.state == {"amount": 10, "items": [1, 2]}


# --------------------------------------------------------------------------------------------------- schema


def test_schema_correct_types_pass() -> None:
    schema = InputSchema({"amount": FieldSpec(int), "note": FieldSpec(str)})
    assert schema.validate({"amount": 5, "note": "ok"}) == {"amount": 5, "note": "ok"}


@pytest.mark.parametrize("ftype", [int, float])
def test_bool_is_not_a_number(ftype: type) -> None:
    schema = InputSchema({"amount": FieldSpec(ftype)})
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"amount": True})
    assert info.value.errors == [f"amount: expected {ftype.__name__}, got bool"]


def test_bool_accepted_when_declared() -> None:
    schema = InputSchema({"flag": FieldSpec((int, bool))})
    assert schema.validate({"flag": True}) == {"flag": True}


def test_int_satisfies_float() -> None:
    assert InputSchema({"ratio": FieldSpec(float)}).validate({"ratio": 1}) == {"ratio": 1}


def test_none_requires_nonetype() -> None:
    with pytest.raises(SchemaValidationError):
        InputSchema({"x": FieldSpec(str)}).validate({"x": None})
    assert InputSchema({"x": FieldSpec((str, type(None)))}).validate({"x": None}) == {"x": None}


def test_missing_required_field() -> None:
    with pytest.raises(SchemaValidationError) as info:
        InputSchema({"decision": FieldSpec(str)}).validate({})
    assert info.value.errors == ["decision: required"]
    assert isinstance(info.value, ValidationError)


def test_default_applied_as_copy() -> None:
    default = ["a"]
    schema = InputSchema(
        {"priority": FieldSpec(str, default="low"), "tags": FieldSpec(list, default=default)}
    )
    cleaned = schema.validate({})
    assert cleaned == {"priority": "low", "tags": ["a"]}
    cleaned["tags"].append("b")
    assert schema.validate({})["tags"] == ["a"]


def test_optional_field_omitted() -> None:
    assert InputSchema({"note": FieldSpec(str, required=False)}).validate({}) == {}


def test_choices() -> None:
    schema = InputSchema({"decision": FieldSpec(str, choices=["approve", "reject"])})
    assert schema.validate({"decision": "approve"}) == {"decision": "approve"}
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"decision": "maybe"})
    assert info.value.errors[0].startswith("decision: must be one of")


def test_choices_do_not_conflate_bool_and_int() -> None:
    schema = InputSchema({"level": FieldSpec((int, bool), choices=[1, 2])})
    with pytest.raises(SchemaValidationError):
        schema.validate({"level": True})


@pytest.mark.parametrize(("value", "ok"), [(0, False), (1, True), (100, True), (101, False), (50.5, True)])
def test_numeric_range(value: float, ok: bool) -> None:
    schema = InputSchema({"amount": FieldSpec((int, float), min=1, max=100)})
    if ok:
        assert schema.validate({"amount": value}) == {"amount": value}
    else:
        with pytest.raises(SchemaValidationError):
            schema.validate({"amount": value})


@pytest.mark.parametrize(
    ("ftype", "value"), [(str, "abcd"), (list, [1, 2, 3, 4]), (dict, dict.fromkeys("abcd"))]
)
def test_length_range(ftype: type, value: object) -> None:
    schema = InputSchema({"x": FieldSpec(ftype, min=1, max=3)})
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"x": value})
    assert info.value.errors == ["x: length must be <= 3"]
    with pytest.raises(SchemaValidationError, match="length must be >= 1"):
        schema.validate({"x": ftype()})


def test_pattern_fullmatch() -> None:
    schema = InputSchema({"code": FieldSpec(str, pattern=r"[A-Z]{3}")})
    assert schema.validate({"code": "ABC"}) == {"code": "ABC"}
    for bad in ("abcd", "ABCD", "AB"):
        with pytest.raises(SchemaValidationError, match="code: does not match pattern"):
            schema.validate({"code": bad})


def test_custom_validator_message() -> None:
    schema = InputSchema({"n": FieldSpec(int, validator=lambda v: None if v % 2 == 0 else "must be even")})
    assert schema.validate({"n": 4}) == {"n": 4}
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"n": 3})
    assert info.value.errors == ["n: must be even"]


def test_custom_validator_false_or_raises() -> None:
    def boom(_: object) -> bool:
        raise RuntimeError("kaboom")

    schema = InputSchema(
        {"a": FieldSpec(int, validator=lambda v: v > 0), "b": FieldSpec(int, validator=boom)}
    )
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"a": -1, "b": 1})
    assert info.value.errors == ["a: rejected by validator", "b: validator raised RuntimeError"]
    assert InputSchema({"a": FieldSpec(int, validator=lambda v: True)}).validate({"a": 1}) == {"a": 1}


def test_unknown_key_rejected_or_allowed() -> None:
    fields = {"a": FieldSpec(int)}
    with pytest.raises(SchemaValidationError) as info:
        InputSchema(fields).validate({"a": 1, "extra": 2})
    assert info.value.errors == ["extra: unexpected field"]
    assert InputSchema(fields, allow_extra=True).validate({"a": 1, "extra": [2]}) == {"a": 1, "extra": [2]}


def test_all_errors_reported_at_once() -> None:
    schema = InputSchema({"amount": FieldSpec(int), "decision": FieldSpec(str)})
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"amount": "ten", "bogus": 1})
    assert info.value.errors == [
        "amount: expected int, got str",
        "decision: required",
        "bogus: unexpected field",
    ]
    assert "decision: required" in str(info.value)


def test_multiple_constraint_errors_for_one_field() -> None:
    schema = InputSchema({"s": FieldSpec(str, max=2, pattern=r"\d+")})
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"s": "abc"})
    assert len(info.value.errors) == 2


@pytest.mark.parametrize("data", [["a"], "text", 5])
def test_non_mapping_input_rejected(data: object) -> None:
    with pytest.raises(SchemaValidationError):
        InputSchema({}).validate(data)


def test_error_messages_never_echo_values() -> None:
    schema = InputSchema(
        {
            "a": FieldSpec(str, max=3, pattern=r"x+", choices=["xx"]),
            "b": FieldSpec(int),
        }
    )
    with pytest.raises(SchemaValidationError) as info:
        schema.validate({"a": "SECRETVALUE", "b": "SECRET2"})
    assert "SECRET" not in " ".join(info.value.errors)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": int, "min": 5, "max": 1},
        {"type": str, "pattern": "("},
        {"type": int, "default": "x"},
        {"type": "int"},
        {"type": ()},
        {"type": str, "choices": "abc"},
        {"type": int, "validator": 3},
        {"type": int, "min": "1"},
        {"type": int, "choices": [1, 2], "default": 3},
    ],
)
def test_invalid_field_definitions(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        FieldSpec(**kwargs)


def test_invalid_schema_definitions() -> None:
    with pytest.raises(ValidationError):
        InputSchema({"a": int})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        InputSchema({"": FieldSpec(int)})
    with pytest.raises(ValidationError):
        InputSchema([("a", FieldSpec(int))])  # type: ignore[arg-type]


def test_schema_description() -> None:
    schema = decision_schema()
    desc = schema.to_dict()
    json.dumps(desc)
    assert desc["allow_extra"] is False
    assert desc["fields"]["decision"] == {
        "type": ["str"],
        "required": True,
        "description": "final call",
        "has_validator": False,
        "choices": ["approve", "reject"],
    }
    assert desc["fields"]["amount"]["min"] == 1 and desc["fields"]["amount"]["max"] == 100
    assert desc["fields"]["priority"] == {
        "type": ["str"],
        "required": False,
        "description": "",
        "has_validator": False,
        "default": "low",
    }
    assert desc["fields"]["note"]["required"] is False
    pattern_desc = FieldSpec(str, pattern="a+", validator=bool).to_dict()
    assert pattern_desc["pattern"] == "a+" and pattern_desc["has_validator"] is True
    assert FieldSpec(str, choices={"b", "a"}).to_dict()["choices"] == ["a", "b"]


def test_missing_sentinel() -> None:
    assert repr(MISSING) == "MISSING"
    assert not MISSING
    assert FieldSpec(int).default is MISSING


# --------------------------------------------------------------------------------------------------- resume


async def test_successful_resume(mgr: PauseResumeManager, clock: ManualClock) -> None:
    cp = await mgr.pause("run-1", "review", {"amount": 10, "note": "agent"})
    clock.advance(5)
    ctx = await mgr.resume(cp.id, {"note": "human", "approved": True}, actor="alice")
    assert isinstance(ctx, ResumedContext)
    assert ctx.merged == {"amount": 10, "note": "human", "approved": True}
    assert ctx.state == {"amount": 10, "note": "agent"}
    assert ctx.human_input == {"note": "human", "approved": True}
    assert ctx.resumed_by == "alice"
    assert ctx.resumed_at == 105.0
    assert ctx.checkpoint_id == cp.id and ctx.run_id == "run-1" and ctx.step == "review"
    assert ctx.state_fingerprint == cp.state_fingerprint
    assert ctx.input_fingerprint == fingerprint({"note": "human", "approved": True})
    json.dumps(ctx.to_dict())
    stored = await mgr.get(cp.id)
    assert stored.status is CheckpointStatus.RESUMED
    assert stored.version == 2
    assert stored.resumed_by == "alice" and stored.resumed_at == 105.0 and stored.closed_at == 105.0


async def test_resume_with_schema_applies_defaults(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {"amount": 3}, input_schema=decision_schema())
    ctx = await mgr.resume(cp.id, {"decision": "approve", "amount": 7}, actor="alice")
    assert ctx.human_input == {"decision": "approve", "amount": 7, "priority": "low"}
    assert ctx.merged == {"decision": "approve", "amount": 7, "priority": "low"}


async def test_resume_without_schema_and_input(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {"x": 1})
    ctx = await mgr.resume(cp.id, actor="alice")
    assert ctx.human_input == {}
    assert ctx.merged == {"x": 1}


@pytest.mark.parametrize("bad", [{"x": object()}, {"x": {1, 2}}, ["not", "a", "mapping"]])
async def test_resume_without_schema_rejects_non_json(mgr: PauseResumeManager, bad: object) -> None:
    cp = await mgr.pause("r", "s", {})
    with pytest.raises(SchemaValidationError):
        await mgr.resume(cp.id, bad, actor="alice")  # type: ignore[arg-type]
    assert (await mgr.get(cp.id)).status is CheckpointStatus.ACTIVE


async def test_schema_allowed_non_json_value_is_rejected(mgr: PauseResumeManager) -> None:
    schema = InputSchema({"tags": FieldSpec(set)})
    cp = await mgr.pause("r", "s", {}, input_schema=schema)
    with pytest.raises(SchemaValidationError, match="not JSON-serializable"):
        await mgr.resume(cp.id, {"tags": {"a"}}, actor="alice")
    assert (await mgr.get(cp.id)).is_active


@pytest.mark.parametrize("actor", ["", "  ", None])
async def test_resume_requires_actor(mgr: PauseResumeManager, actor: object) -> None:
    cp = await mgr.pause("r", "s", {})
    with pytest.raises(ValidationError):
        await mgr.resume(cp.id, actor=actor)  # type: ignore[arg-type]
    assert (await mgr.get(cp.id)).status is CheckpointStatus.ACTIVE


async def test_resume_mutation_of_context_is_isolated(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {"items": [1]})
    ctx = await mgr.resume(cp.id, {"more": [2]}, actor="alice")
    ctx.merged["items"].append(99)
    ctx.merged["more"].append(99)
    assert ctx.state == {"items": [1]}
    assert ctx.human_input == {"more": [2]}
    assert (await mgr.get(cp.id)).state == {"items": [1]}


# --------------------------------------------------------------------------------------------------- events


async def test_resumed_event_has_fingerprint_and_keys_only(mgr: PauseResumeManager, events: Recorder) -> None:
    cp = await mgr.pause("r", "s", {"a": 1})
    await mgr.resume(cp.id, {"secret": "s3cr3t-value", "b": 2}, actor="alice")
    (ev,) = events.of("checkpoint.resumed")
    assert ev.actor == "alice"
    assert ev.subject_id == cp.id
    assert ev.data["input_keys"] == ["b", "secret"]
    assert ev.data["input_fingerprint"] == fingerprint({"secret": "s3cr3t-value", "b": 2})
    assert ev.data["state_fingerprint"] == cp.state_fingerprint
    assert "s3cr3t-value" not in json.dumps([e.to_dict() for e in events.events])


async def test_rejected_event_for_invalid_input(mgr: PauseResumeManager, events: Recorder) -> None:
    cp = await mgr.pause("r", "s", {}, input_schema=decision_schema())
    with pytest.raises(SchemaValidationError) as info:
        await mgr.resume(cp.id, {"decision": "TOPSECRET", "amount": 1000, "zzz": "HIDDEN"}, actor="bob")
    (ev,) = events.of("checkpoint.resume_rejected")
    assert ev.data["reason"] == "validation"
    assert ev.data["errors"] == info.value.errors
    assert len(ev.data["errors"]) == 3
    dumped = json.dumps(ev.to_dict())
    assert "TOPSECRET" not in dumped and "HIDDEN" not in dumped and "1000" not in dumped


async def test_failing_sink_is_isolated(clock: ManualClock) -> None:
    def broken(_: Event) -> None:
        raise RuntimeError("sink down")

    mgr = PauseResumeManager(clock=clock, event_sink=broken)
    cp = await mgr.pause("r", "s", {"a": 1})
    ctx = await mgr.resume(cp.id, {"b": 2}, actor="alice")
    assert ctx.merged == {"a": 1, "b": 2}


async def test_default_sink_and_clock() -> None:
    mgr = PauseResumeManager()
    cp = await mgr.pause("r", "s", {})
    assert cp.created_at > 0
    assert isinstance(mgr.store, CheckpointStore)


# ------------------------------------------------------------------------------------- retry / single use


async def test_invalid_input_leaves_checkpoint_resumable(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {}, input_schema=decision_schema())
    with pytest.raises(SchemaValidationError):
        await mgr.resume(cp.id, {"decision": "maybe", "amount": 5}, actor="alice")
    stored = await mgr.get(cp.id)
    assert stored.status is CheckpointStatus.ACTIVE and stored.version == 1
    ctx = await mgr.resume(cp.id, {"decision": "reject", "amount": 5}, actor="alice")
    assert ctx.human_input["decision"] == "reject"


async def test_second_resume_rejected(mgr: PauseResumeManager, events: Recorder, clock: ManualClock) -> None:
    cp = await mgr.pause("r", "s", {})
    await mgr.resume(cp.id, {"v": 1}, actor="alice")
    clock.advance(1)
    with pytest.raises(StateError) as info:
        await mgr.resume(cp.id, {"v": 2}, actor="mallory")
    assert not isinstance(info.value, ExpiredError)
    stored = await mgr.get(cp.id)
    assert stored.resumed_by == "alice" and stored.resumed_at == 100.0 and stored.version == 2
    (ev,) = events.of("checkpoint.resume_rejected")
    assert ev.data["reason"] == "state" and ev.actor == "mallory"


async def test_resume_after_discard_rejected(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {})
    await mgr.discard(cp.id, actor="bob")
    with pytest.raises(StateError):
        await mgr.resume(cp.id, actor="alice")


async def test_concurrent_resumes_exactly_one_wins(mgr: PauseResumeManager, events: Recorder) -> None:
    cp = await mgr.pause("r", "s", {"a": 1})
    results = await asyncio.gather(
        *(mgr.resume(cp.id, {"n": i}, actor=f"user{i}") for i in range(10)), return_exceptions=True
    )
    wins = [r for r in results if isinstance(r, ResumedContext)]
    losses = [r for r in results if isinstance(r, StateError)]
    assert len(wins) == 1 and len(losses) == 9
    stored = await mgr.get(cp.id)
    assert stored.version == 2 and stored.resumed_by == wins[0].resumed_by
    assert len(events.of("checkpoint.resumed")) == 1


class YieldingStore(InMemoryCheckpointStore):
    """Yields to the event loop after every read so two managers interleave read-then-write."""

    async def get(self, checkpoint_id: str) -> Checkpoint | None:
        cp = await super().get(checkpoint_id)
        await asyncio.sleep(0)
        return cp


async def test_two_managers_sharing_a_store_use_optimistic_versioning(
    clock: ManualClock, events: Recorder
) -> None:
    store = YieldingStore()
    a = PauseResumeManager(store=store, clock=clock, event_sink=events)
    b = PauseResumeManager(store=store, clock=clock, event_sink=events)
    cp = await a.pause("r", "s", {})
    results = await asyncio.gather(
        a.resume(cp.id, actor="alice"), b.resume(cp.id, actor="bob"), return_exceptions=True
    )
    assert sum(isinstance(r, ResumedContext) for r in results) == 1
    (err,) = [r for r in results if isinstance(r, StateError)]
    assert "version conflict" in str(err)
    assert (await a.get(cp.id)).version == 2
    assert [e.data["reason"] for e in events.of("checkpoint.resume_rejected")] == ["state"]


async def test_concurrent_resumes_of_different_checkpoints(mgr: PauseResumeManager) -> None:
    cps = [await mgr.pause("r", "s", {"i": i}) for i in range(20)]
    results = await asyncio.gather(*(mgr.resume(cp.id, actor="alice") for cp in cps))
    assert [r.state["i"] for r in results] == list(range(20))


# --------------------------------------------------------------------------------------------------- expiry


async def test_resume_after_expiry(mgr: PauseResumeManager, clock: ManualClock, events: Recorder) -> None:
    cp = await mgr.pause("r", "s", {}, ttl=10)
    clock.advance(10)
    with pytest.raises(ExpiredError):
        await mgr.resume(cp.id, actor="alice")
    stored = await mgr.get(cp.id)
    assert stored.status is CheckpointStatus.EXPIRED and stored.version == 2 and stored.closed_at == 110.0
    assert events.kinds()[-2:] == ["checkpoint.expired", "checkpoint.resume_rejected"]
    assert events.of("checkpoint.resume_rejected")[0].data["reason"] == "expired"
    assert events.of("checkpoint.expired")[0].data["expires_at"] == 110.0


async def test_resume_before_expiry(mgr: PauseResumeManager, clock: ManualClock) -> None:
    cp = await mgr.pause("r", "s", {}, ttl=10)
    clock.advance(9)
    ctx = await mgr.resume(cp.id, actor="alice")
    assert ctx.resumed_at == 109.0


async def test_resume_already_expired(mgr: PauseResumeManager, clock: ManualClock, events: Recorder) -> None:
    cp = await mgr.pause("r", "s", {}, ttl=1)
    clock.advance(2)
    assert [c.id for c in await mgr.expire_due()] == [cp.id]
    with pytest.raises(ExpiredError):
        await mgr.resume(cp.id, actor="alice")
    assert events.of("checkpoint.resume_rejected")[-1].data["reason"] == "expired"
    assert (await mgr.get(cp.id)).version == 2


# ------------------------------------------------------------------------------------------------ integrity


async def test_tampered_state_detected(
    mgr: PauseResumeManager, store: InMemoryCheckpointStore, events: Recorder
) -> None:
    cp = await mgr.pause("r", "s", {"amount": 10})
    store._items[cp.id].state["amount"] = 1_000_000  # simulate tampering at rest
    with pytest.raises(IntegrityError):
        await mgr.resume(cp.id, actor="alice")
    stored = await mgr.get(cp.id)
    assert stored.status is CheckpointStatus.ACTIVE and stored.version == 1
    assert events.of("checkpoint.resume_rejected")[0].data["reason"] == "integrity"


async def test_tampered_fingerprint_detected(mgr: PauseResumeManager, store: InMemoryCheckpointStore) -> None:
    cp = await mgr.pause("r", "s", {"amount": 10})
    store._items[cp.id].state_fingerprint = "0" * 64
    with pytest.raises(IntegrityError):
        await mgr.resume(cp.id, actor="alice")


async def test_expected_fingerprint_mismatch(mgr: PauseResumeManager, events: Recorder) -> None:
    cp = await mgr.pause("r", "s", {"amount": 10})
    with pytest.raises(IntegrityError):
        await mgr.resume(cp.id, actor="alice", expected_fingerprint=fingerprint({"amount": 11}))
    assert (await mgr.get(cp.id)).is_active
    assert events.of("checkpoint.resume_rejected")[0].data["reason"] == "integrity"


async def test_expected_fingerprint_match(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {"amount": 10})
    ctx = await mgr.resume(cp.id, actor="alice", expected_fingerprint=cp.state_fingerprint)
    assert ctx.state_fingerprint == cp.state_fingerprint


# ------------------------------------------------------------------------------------------- not found


async def test_unknown_id(mgr: PauseResumeManager) -> None:
    with pytest.raises(NotFoundError):
        await mgr.get("ckpt_missing")
    with pytest.raises(NotFoundError):
        await mgr.resume("ckpt_missing", actor="alice")
    with pytest.raises(NotFoundError):
        await mgr.discard("ckpt_missing", actor="alice")
    with pytest.raises(NotFoundError):
        await mgr.get("")


# ---------------------------------------------------------------------------------------------- discard


async def test_discard_active(mgr: PauseResumeManager, events: Recorder, clock: ManualClock) -> None:
    cp = await mgr.pause("run-9", "s", {})
    clock.advance(3)
    out = await mgr.discard(cp.id, actor="bob", reason="obsolete")
    assert out.status is CheckpointStatus.DISCARDED
    assert out.discarded_by == "bob" and out.discard_reason == "obsolete"
    assert out.version == 2 and out.closed_at == 103.0
    (ev,) = events.of("checkpoint.discarded")
    assert ev.actor == "bob" and ev.data == {"run_id": "run-9", "step": "s", "reason": "obsolete"}
    assert ev.timestamp == 103.0


async def test_discard_resumed_rejected(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {})
    await mgr.resume(cp.id, actor="alice")
    with pytest.raises(StateError):
        await mgr.discard(cp.id, actor="bob")


async def test_discard_validates_arguments(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {})
    with pytest.raises(ValidationError):
        await mgr.discard(cp.id, actor="")
    with pytest.raises(ValidationError):
        await mgr.discard(cp.id, actor="bob", reason=None)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------- expire_due


async def test_expire_due(mgr: PauseResumeManager, clock: ManualClock, events: Recorder) -> None:
    short = await mgr.pause("r", "s", {}, ttl=5)
    long = await mgr.pause("r", "s", {}, ttl=50)
    forever = await mgr.pause("r", "s", {})
    clock.advance(10)
    expired = await mgr.expire_due()
    assert [c.id for c in expired] == [short.id]
    assert expired[0].status is CheckpointStatus.EXPIRED
    assert (await mgr.get(long.id)).is_active and (await mgr.get(forever.id)).is_active
    (ev,) = events.of("checkpoint.expired")
    assert ev.subject_id == short.id and ev.actor == "system" and ev.data["expires_at"] == 105.0
    assert await mgr.expire_due() == []


async def test_expire_due_spans_pages(mgr: PauseResumeManager, clock: ManualClock) -> None:
    cps = [await mgr.pause("r", "s", {}, ttl=1) for _ in range(7)]
    clock.advance(1)
    expired = await mgr.expire_due(page_size=2)
    assert sorted(c.id for c in expired) == sorted(c.id for c in cps)


# ---------------------------------------------------------------------------------------------- listing


async def test_active_for_run(mgr: PauseResumeManager, clock: ManualClock) -> None:
    active = await mgr.pause("run-a", "s1", {})
    resumed = await mgr.pause("run-a", "s2", {})
    await mgr.resume(resumed.id, actor="alice")
    await mgr.pause("run-a", "s3", {}, ttl=1)
    await mgr.pause("run-b", "s1", {})
    clock.advance(1)
    assert [c.id for c in await mgr.active_for_run("run-a")] == [active.id]
    assert await mgr.active_for_run("nope") == []


async def test_paginated_listing(mgr: PauseResumeManager, store: InMemoryCheckpointStore) -> None:
    cps = [await mgr.pause("r", f"s{i}", {}) for i in range(5)]
    seen: list[str] = []
    sizes: list[int] = []
    cursor = None
    while True:
        page = await store.list_active(limit=2, cursor=cursor)
        assert isinstance(page, CheckpointPage)
        sizes.append(len(page.items))
        seen.extend(c.id for c in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sizes == [2, 2, 1]
    assert seen == [c.id for c in cps]


async def test_cursor_stability(mgr: PauseResumeManager, store: InMemoryCheckpointStore) -> None:
    cps = [await mgr.pause("r", f"s{i}", {}) for i in range(5)]
    first = await store.list_active(limit=2)
    await mgr.resume(first.items[0].id, actor="alice")
    second = await store.list_active(limit=2, cursor=first.next_cursor)
    third = await store.list_active(limit=2, cursor=second.next_cursor)
    assert [c.id for c in second.items + third.items] == [c.id for c in cps[2:]]
    assert third.next_cursor is None


async def test_listing_excludes_non_active(mgr: PauseResumeManager, store: InMemoryCheckpointStore) -> None:
    a = await mgr.pause("r", "s", {})
    b = await mgr.pause("r", "s", {})
    await mgr.discard(a.id, actor="bob")
    page = await store.list_active()
    assert [c.id for c in page.items] == [b.id] and page.next_cursor is None


@pytest.mark.parametrize(
    ("limit", "cursor"), [(0, None), (1001, None), (True, None), (10, "abc"), (10, "-1"), (10, "")]
)
async def test_invalid_page_parameters(store: InMemoryCheckpointStore, limit: object, cursor: object) -> None:
    with pytest.raises(ValidationError):
        await store.list_active(limit=limit, cursor=cursor)  # type: ignore[arg-type]


async def test_list_by_run_in_creation_order(mgr: PauseResumeManager, store: InMemoryCheckpointStore) -> None:
    cps = [await mgr.pause("run-x", f"s{i}", {}) for i in range(4)]
    await mgr.pause("run-y", "s", {})
    assert [c.id for c in await store.list_by_run("run-x")] == [c.id for c in cps]


# ------------------------------------------------------------------------------------------------ store


def _make_cp(cid: str = "ckpt_test", version: int = 1) -> Checkpoint:
    return Checkpoint(
        id=cid, run_id="r", step="s", state={"a": [1]}, state_fingerprint=fingerprint({"a": [1]}),
        created_at=0.0, version=version,
    )  # fmt: skip


async def test_store_stale_update_rejected(store: InMemoryCheckpointStore) -> None:
    await store.put(_make_cp())
    updated = _make_cp(version=2)
    updated.status = CheckpointStatus.RESUMED
    with pytest.raises(StateError):
        await store.put(updated, expected_version=5)
    assert (await store.get("ckpt_test")).version == 1  # type: ignore[union-attr]
    await store.put(updated, expected_version=1)
    assert (await store.get("ckpt_test")).status is CheckpointStatus.RESUMED  # type: ignore[union-attr]


async def test_store_duplicate_create_rejected(store: InMemoryCheckpointStore) -> None:
    await store.put(_make_cp())
    with pytest.raises(StateError):
        await store.put(_make_cp())


async def test_store_update_missing_rejected(store: InMemoryCheckpointStore) -> None:
    with pytest.raises(NotFoundError):
        await store.put(_make_cp(), expected_version=1)
    assert await store.get("ckpt_test") is None


async def test_store_returns_isolated_copies(store: InMemoryCheckpointStore) -> None:
    cp = _make_cp()
    await store.put(cp)
    cp.state["a"].append(2)
    got = await store.get("ckpt_test")
    assert got is not None and got.state == {"a": [1]}
    got.state["a"].append(3)
    got.metadata["x"] = 1
    again = await store.get("ckpt_test")
    assert again is not None and again.state == {"a": [1]} and again.metadata == {}


def test_in_memory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryCheckpointStore(), CheckpointStore)


# -------------------------------------------------------------------------------------------- serialize


async def test_checkpoint_to_dict(mgr: PauseResumeManager) -> None:
    cp = await mgr.pause("r", "s", {"a": 1}, input_schema=decision_schema(), ttl=5, metadata={"k": "v"})
    d = cp.to_dict()
    json.dumps(d)
    assert d["status"] == "active"
    assert d["id"] == cp.id and d["run_id"] == "r" and d["step"] == "s"
    assert d["state"] == {"a": 1} and d["state_fingerprint"] == cp.state_fingerprint
    assert d["input_schema"] == decision_schema().to_dict()
    assert d["created_at"] == 100.0 and d["expires_at"] == 105.0
    assert d["version"] == 1 and d["metadata"] == {"k": "v"}
    assert d["resumed_at"] is None and d["resumed_by"] is None
    plain = await mgr.pause("r", "s", {})
    assert plain.to_dict()["input_schema"] is None
