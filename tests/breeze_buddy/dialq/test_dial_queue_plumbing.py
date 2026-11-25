"""dial_queue plumbing: every dialler lead gets a waiting row, and loses it
when cancelled or finished. Nothing reads the table yet."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

# dispatch must import first (managers.calls <-> dispatch.worker cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.dispatch import queue, reconcilers
from app.database.accessor.breeze_buddy import (
    dial_queue as dq_acc,
    lead_call_tracker as lct,
)
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker

WHEN = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


class _Redis:
    def __init__(self) -> None:
        self.zadds: List[Any] = []
        self.zrems: List[Any] = []

    async def get_client(self) -> "_Redis":
        return self

    async def zadd(self, key: str, mapping: Dict[str, int]) -> None:
        self.zadds.append(mapping)

    async def zrem(self, key: str, member: str) -> None:
        self.zrems.append(member)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _Redis:
    fake = _Redis()

    async def get() -> _Redis:
        return fake

    monkeypatch.setattr(queue, "get_redis_service", get)
    return fake


async def test_schedule_lead_enqueues_with_the_unjittered_time(monkeypatch, redis):
    seen: List[Any] = []

    async def enqueue(lead_id: str, due_at: datetime) -> bool:
        seen.append((lead_id, due_at))
        return True

    monkeypatch.setattr(queue, "enqueue_dial", enqueue)

    assert await queue.schedule_lead("L1", WHEN, jitter_ms=500) is True
    assert seen == [("L1", WHEN)]
    assert len(redis.zadds) == 1, "the old schedule still gets the lead"


async def test_schedule_lead_survives_a_dial_queue_failure(monkeypatch, redis):
    """The real (fail-open) accessor, with its database failing."""

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(queue, "enqueue_dial", dq_acc.enqueue_dial)
    monkeypatch.setattr(dq_acc, "run_parameterized_query", boom)

    assert await queue.schedule_lead("L1", WHEN) is True
    assert len(redis.zadds) == 1


async def test_cancel_deletes_the_waiting_row(monkeypatch, redis):
    deleted: List[str] = []

    async def delete(lead_id: str) -> bool:
        deleted.append(lead_id)
        return True

    monkeypatch.setattr(queue, "delete_waiting_dial", delete)

    await queue.cancel_scheduled_lead("L1")

    assert deleted == ["L1"] and redis.zrems == ["L1"]


def _lead(status: LeadCallStatus) -> LeadCallTracker:
    return LeadCallTracker(
        id="L1",
        reseller_id="r1",
        template="t",
        status=status,
        call_direction=CallDirection.OUTBOUND,
        execution_mode=ExecutionMode.TELEPHONY,
    )


@pytest.mark.parametrize(
    "status,row,expect_delete",
    [
        (LeadCallStatus.FINISHED, True, True),
        (None, True, False),  # a mid-call outcome write
        (LeadCallStatus.FINISHED, False, False),  # lost the claim
    ],
)
async def test_finishing_write_deletes_the_waiting_row(
    monkeypatch, status, row, expect_delete
):
    deleted: List[str] = []

    async def run(*_a: Any, **_k: Any) -> List[Any]:
        return [{"id": "L1"}] if row else []

    async def delete(lead_id: str) -> bool:
        deleted.append(lead_id)
        return True

    monkeypatch.setattr(lct, "run_parameterized_query", run)
    monkeypatch.setattr(
        lct, "decode_lead_call_tracker", lambda _r: _lead(LeadCallStatus.FINISHED)
    )
    monkeypatch.setattr(lct, "delete_waiting_dial", delete)
    monkeypatch.setattr(lct, "_fire_hooks", lambda *_a: None)

    await lct.update_lead_call_completion_details(
        id="L1", status=status, outcome="X", expected_status=LeadCallStatus.PROCESSING
    )

    assert deleted == (["L1"] if expect_delete else [])


async def test_feeder_task_asks_for_five_times_the_reconcile_limit(monkeypatch):
    asked: List[int] = []

    async def limit() -> int:
        return 1000

    async def feed(n: int) -> int:
        asked.append(n)
        return 3

    monkeypatch.setattr(reconcilers.dyn_cfg, "BB_RECONCILE_BACKLOG_LIMIT", limit)
    monkeypatch.setattr(reconcilers, "feed_dial_queue", feed)

    await reconcilers.feed_dial_queue_task()

    assert asked == [5000]
