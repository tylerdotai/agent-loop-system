from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop.message_board import (
    FactConflictError,
    FactOwnershipError,
    MessageBoard,
    MessageValidationError,
)
from agent_loop.persistence import SQLiteStore


@pytest.fixture
def board(tmp_path: Path) -> MessageBoard:
    return MessageBoard(SQLiteStore(tmp_path / "control.db"))


def test_publish_is_append_only_and_uses_server_identity(board: MessageBoard) -> None:
    first = board.publish(
        mission_id="mission-1",
        topic="mission.mission-1.research",
        kind="fact_observation",
        actor_id="researcher-1",
        body="Found the primary source.",
        data={"url": "https://example.test/source"},
    )
    second = board.publish(
        mission_id="mission-1",
        topic="mission.mission-1.research",
        kind="question",
        actor_id="reviewer-1",
        body="Was the date independently verified?",
        reply_to=first.message_id,
    )

    messages = board.list_messages("mission-1", topic_prefix="mission.mission-1.research")

    assert [message.message_id for message in messages] == [first.message_id, second.message_id]
    assert messages[0].actor_id == "researcher-1"
    assert messages[0].data == {"url": "https://example.test/source"}
    assert messages[1].reply_to == first.message_id
    assert first.sequence < second.sequence


def test_publish_rejects_unknown_kind_and_cross_mission_topic(board: MessageBoard) -> None:
    with pytest.raises(MessageValidationError, match="message kind"):
        board.publish("mission-1", "mission.mission-1.general", "command", "agent-1", "do it")

    with pytest.raises(MessageValidationError, match="topic must be scoped"):
        board.publish("mission-1", "mission.other.general", "question", "agent-1", "why")


def test_dedupe_key_returns_original_message_without_duplicate_event(board: MessageBoard) -> None:
    original = board.publish(
        "mission-1",
        "mission.mission-1.general",
        "checkpoint",
        "agent-1",
        "halfway",
        data={"coordinates": (1, 2)},
        dedupe_key="run-1:checkpoint:1",
    )
    duplicate = board.publish(
        "mission-1",
        "mission.mission-1.general",
        "checkpoint",
        "agent-1",
        "halfway",
        data={"coordinates": (1, 2)},
        dedupe_key="run-1:checkpoint:1",
    )

    assert duplicate == original
    assert len(board.list_messages("mission-1")) == 1
    assert [event.kind for event in board.store.list_events()] == ["message.published"]

    with pytest.raises(MessageValidationError, match="idempotency"):
        board.publish(
            "mission-1",
            "mission.mission-1.general",
            "checkpoint",
            "agent-1",
            "different retry payload",
            data={"coordinates": (1, 2)},
            dedupe_key="run-1:checkpoint:1",
        )


def test_subscription_cursor_survives_reopen_and_ack_is_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    board = MessageBoard(SQLiteStore(path))
    subscription = board.subscribe("mission-1", "worker-1", "mission.mission-1.research")
    first = board.publish("mission-1", "mission.mission-1.research.a", "question", "planner", "A?")
    second = board.publish("mission-1", "mission.mission-1.research.b", "question", "planner", "B?")

    unread = board.read_subscription(subscription.subscription_id)
    assert [message.message_id for message in unread] == [first.message_id, second.message_id]

    board.ack(subscription.subscription_id, first.message_id)
    reopened = MessageBoard(SQLiteStore(path))
    assert [message.message_id for message in reopened.read_subscription(subscription.subscription_id)] == [
        second.message_id
    ]

    reopened.ack(subscription.subscription_id, first.message_id)
    assert reopened.get_subscription(subscription.subscription_id).cursor_sequence == first.sequence


def test_ack_rejects_message_outside_subscription_scope(board: MessageBoard) -> None:
    subscription = board.subscribe("mission-1", "worker-1", "mission.mission-1.research")
    unrelated = board.publish("mission-1", "mission.mission-1.build", "question", "planner", "Build?")

    with pytest.raises(MessageValidationError, match="subscription scope"):
        board.ack(subscription.subscription_id, unrelated.message_id)


def test_fact_compare_and_swap_detects_stale_writer(board: MessageBoard) -> None:
    created = board.put_fact(
        mission_id="mission-1",
        fact_key="contract/export-schema",
        value={"version": 1},
        actor_id="architect",
        expected_version=0,
    )
    updated = board.put_fact(
        mission_id="mission-1",
        fact_key="contract/export-schema",
        value={"version": 2},
        actor_id="architect",
        expected_version=created.version,
    )

    assert created.version == 1
    assert updated.version == 2
    assert board.get_fact("mission-1", "contract/export-schema") == updated

    with pytest.raises(FactConflictError, match="expected version 1"):
        board.put_fact(
            "mission-1",
            "contract/export-schema",
            {"version": 3},
            "architect",
            expected_version=1,
        )


def test_fact_namespace_owner_cannot_be_overwritten_by_peer(board: MessageBoard) -> None:
    board.put_fact("mission-1", "decision/runtime", {"runner": "script"}, "planner", expected_version=0)

    with pytest.raises(FactOwnershipError, match="owned by planner"):
        board.put_fact(
            "mission-1",
            "decision/runtime",
            {"runner": "other"},
            "worker-1",
            expected_version=1,
        )


def test_event_payload_records_provenance_without_mutating_message(board: MessageBoard) -> None:
    message = board.publish(
        "mission-1",
        "mission.mission-1.general",
        "alert",
        "watchdog",
        "lease expired",
        correlation_id="correlation-1",
    )

    events = board.store.list_events(kind="message.published")

    assert len(events) == 1
    assert events[0].actor_id == "watchdog"
    assert events[0].mission_id == "mission-1"
    assert events[0].correlation_id == "correlation-1"
    assert events[0].payload["message_id"] == message.message_id
