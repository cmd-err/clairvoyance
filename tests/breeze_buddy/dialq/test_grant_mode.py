"""Grant mode: a lead whose line fill already granted from dial_queue.

The task takes no Redis token and no ``channels`` increment, claims
``dialing`` with its ticket before any "customer was called" record, marks
the row live after the PROCESSING write, and every exit returns the line
exactly once through ``dispatch_granted``'s give-back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest

# dispatch must import first (managers.calls <-> dispatch.worker cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.dispatch import reconcilers, worker as worker_mod
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.core import TelephonyNumberStatus
from tests.breeze_buddy.dispatch.conftest import (  # noqa: F401  (fixtures)
    CallRecorder,
    fake_redis,
    harness,
    make_lead,
)

TICKET = "00000000-0000-0000-0000-0000000000aa"


class Grant:
    """Records the dial_queue calls a granted task makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, harness: Any) -> None:
        self.events: List[str] = []
        self.claim_ok = True
        self.acquired = 0
        self.harness = harness

        async def claim(lead_id: str, ticket: str) -> bool:
            self.events.append("claim")
            return self.claim_ok

        async def live(lead_id: str, ticket: str) -> bool:
            self.events.append("live")
            return True

        async def give_back(lead_id: str, ticket: str, may_have_rung=False) -> bool:
            self.events.append("give_back")
            self.may_have_rung = may_have_rung
            return True

        async def number(number_id: str) -> Any:
            return harness.number

        real_acquire = worker_mod._acquire_number

        async def acquire(number: Any) -> Any:
            self.acquired += 1
            return await real_acquire(number)

        monkeypatch.setattr(worker_mod, "claim_dialing", claim)
        monkeypatch.setattr(worker_mod, "mark_dial_live", live)
        monkeypatch.setattr(worker_mod, "give_back_dial", give_back)
        monkeypatch.setattr(worker_mod, "get_telephony_number_by_id", number)
        monkeypatch.setattr(worker_mod, "_acquire_number", acquire)

        real_make = harness.call_recorder.make_call

        def make_call(to: str, from_number: str, **kw: Any) -> Any:
            self.events.append("dial")
            return real_make(to, from_number, **kw)

        harness.call_recorder.make_call = make_call

    async def run(self, lead_id: str) -> None:
        worker = worker_mod.Worker(worker_uuid="w-grant")
        await worker.dispatch_granted(lead_id, self.harness.number.id, TICKET, None)


async def test_grant_happy_path(harness, fake_redis, monkeypatch):
    lead = make_lead("lead-g")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    g = Grant(monkeypatch, harness)

    await g.run(lead.id)

    assert g.events == ["claim", "dial", "live", "give_back"]
    assert g.acquired == 0, "fill already took the line"
    assert await channel_tokens_available(harness.number.id) == 1, "no Redis token"
    assert lead.status == LeadCallStatus.PROCESSING


def _blacklisted(h: Any, lead: Any) -> None:
    h.is_blacklisted = True


def _precheck_abort(h: Any, lead: Any) -> None:
    h.pre_check_result = False


def _rate_limited(h: Any, lead: Any) -> None:
    h.rate_limit_ok = False
    h.rate_limit_defer_seconds = 60


def _no_sid(h: Any, lead: Any) -> None:
    h.call_recorder = CallRecorder(sid=None)


def _dial_raises(h: Any, lead: Any) -> None:
    h.call_recorder = CallRecorder(raise_exc=RuntimeError("provider down"))


def _write_fails(h: Any, lead: Any) -> None:
    h.cas_succeeds = False


def _not_backlog(h: Any, lead: Any) -> None:
    lead.status = LeadCallStatus.FINISHED


def _invalid_phone(h: Any, lead: Any) -> None:
    lead.payload = {}


@pytest.mark.parametrize(
    "exit_path",
    [
        _blacklisted,
        _precheck_abort,
        _rate_limited,
        _no_sid,
        _dial_raises,
        _write_fails,
        _not_backlog,
        _invalid_phone,
    ],
)
async def test_grant_every_exit_gives_back_once(
    harness, fake_redis, monkeypatch, exit_path
):
    lead = make_lead("lead-x")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    exit_path(harness, lead)
    g = Grant(monkeypatch, harness)

    await g.run(lead.id)

    assert g.events.count("give_back") == 1
    assert g.acquired == 0
    assert harness.released_numbers == [], "the old release never runs in grant mode"
    assert await channel_tokens_available(harness.number.id) == 1


async def test_grant_disabled_number_gives_back(harness, fake_redis, monkeypatch):
    lead = make_lead("lead-d")
    harness.add_lead(lead)
    g = Grant(monkeypatch, harness)
    disabled = harness.number.model_copy(
        update={"status": TelephonyNumberStatus.DISABLED}
    )

    async def number(_number_id: str) -> Any:
        return disabled

    monkeypatch.setattr(worker_mod, "get_telephony_number_by_id", number)

    await g.run(lead.id)

    assert g.events == ["give_back"], "no claim, no dial"
    assert lead.id in harness.released_locks


async def test_grant_lost_line_stops_before_any_record(
    harness, fake_redis, monkeypatch
):
    lead = make_lead("lead-lost")
    harness.add_lead(lead)
    g = Grant(monkeypatch, harness)
    g.claim_ok = False
    recorded: List[Any] = []

    async def record(**kw: Any) -> Any:
        recorded.append(kw)
        return True, 0

    monkeypatch.setattr(worker_mod, "record_outbound_call_attempt", record)

    await g.run(lead.id)

    assert "dial" not in g.events
    assert recorded == [], "no 'customer was called' record"
    assert lead.id in harness.released_locks


async def test_grant_cancelled_mid_prewarm_still_gives_back(
    harness, fake_redis, monkeypatch
):
    lead = make_lead("lead-c")
    harness.add_lead(lead)
    g = Grant(monkeypatch, harness)
    entered = asyncio.Event()

    async def template(_template_id: Any) -> Any:
        return SimpleNamespace(configurations=None)

    async def prewarm(**_kw: Any) -> None:
        entered.set()
        await asyncio.Event().wait()  # hangs until cancelled

    monkeypatch.setattr(worker_mod, "get_template_by_id", template)
    monkeypatch.setattr(worker_mod, "_prewarm_initial_greeting_with_retry", prewarm)

    task = asyncio.create_task(g.run(lead.id))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert g.events[-1] == "give_back"
    assert "dial" not in g.events


async def test_grant_unexpected_error_is_logged_and_gives_back(
    harness, fake_redis, monkeypatch
):
    """A dial task has no caller: an error is logged, not left on the task."""
    lead = make_lead("lead-e")
    harness.add_lead(lead)
    g = Grant(monkeypatch, harness)
    errors: List[str] = []

    async def boom(_lead: Any) -> Any:
        raise RuntimeError("config read failed")

    monkeypatch.setattr(worker_mod, "_get_lead_config", boom)
    monkeypatch.setattr(worker_mod.logger, "error", lambda m, **_k: errors.append(m))

    await g.run(lead.id)  # does not raise

    assert g.events == ["give_back"]
    assert any("granted dial of lead lead-e failed" in e for e in errors)


async def test_boot_guard_only_trips_on_a_missing_table(monkeypatch):
    from app import database
    from app.database.accessor.breeze_buddy import dial_queue as dq

    async def answer(rows: Any) -> Any:
        return rows

    monkeypatch.setattr(database, "pool", None)
    assert await dq.dial_queue_missing() is False, "no DB: boot as today"

    monkeypatch.setattr(database, "pool", object())
    for rows, missing in [([{"missing": True}], True), ([{"missing": False}], False)]:
        monkeypatch.setattr(
            dq, "run_parameterized_query", lambda *_a, r=rows: answer(r)
        )
        assert await dq.dial_queue_missing() is missing

    async def down(*_a: Any) -> Any:
        raise OSError("db down")

    monkeypatch.setattr(dq, "run_parameterized_query", down)
    assert await dq.dial_queue_missing() is False


# --- repair ---------------------------------------------------------------------


async def test_repair_actions(monkeypatch):
    rows = [
        {"lead_id": "g", "ticket": "t1", "state": "granted", "lead_status": "BACKLOG"},
        {
            "lead_id": "p",
            "ticket": "t2",
            "state": "dialing",
            "lead_status": "PROCESSING",
        },
        {"lead_id": "b", "ticket": "t3", "state": "dialing", "lead_status": "BACKLOG"},
    ]
    done: List[Any] = []

    async def stuck() -> List[Any]:
        return rows

    def rec(name: str):
        async def f(*args: Any) -> bool:
            done.append((name, args))
            return True

        return f

    monkeypatch.setattr(reconcilers, "stuck_dials", stuck)
    monkeypatch.setattr(reconcilers, "give_back_dial", rec("give_back"))
    monkeypatch.setattr(reconcilers, "mark_dial_live", rec("live"))
    monkeypatch.setattr(reconcilers, "release_lock_on_lead_by_id", rec("unlock"))
    monkeypatch.setattr(reconcilers, "raise_dialq_stuck", rec("alert"))

    async def ended() -> int:
        done.append(("ended", ()))
        return 0

    monkeypatch.setattr(reconcilers, "drop_ended_live_rows", ended)

    await reconcilers.repair_dial_queue()

    assert done == [
        ("give_back", ("g", "t1", False)),
        ("unlock", ("g",)),
        ("live", ("p", "t2")),
        ("give_back", ("b", "t3", True)),  # a dialing row may have rung
        ("unlock", ("b",)),
        ("alert", (3,)),
        ("ended", ()),
    ]
