"""A telephony line goes back exactly once, whichever call-end path wins.

Four paths finish a call: the agent's completion, the unanswered webhook,
the 30 s completed-reconcile and the 30-minute stuck-call reaper. Each one
claims PROCESSING -> FINISHED (an UPDATE carrying ``expected_status``) and
only the winner returns the line and schedules the retry.

Releases are counted at the lowest level, so the real
``_release_call_resources`` and the real inbound guard run.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.managers", so isort
# keeps this line above the next one on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import (
    calls as calls_mod,
)
from app.schemas import CallDirection, CallProvider, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import (
    LeadCallTracker,
    TelephonyNumber,
    TelephonyNumberStatus,
)

CALL_ID = "call-1"
CONFIG = object()  # any truthy config; _retry_call is faked


def make_lead(
    status: LeadCallStatus = LeadCallStatus.PROCESSING,
    direction: CallDirection = CallDirection.OUTBOUND,
    outcome: Optional[str] = None,
    template: str = "welcome",
) -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-1",
        telephony_number_id="num-1",
        reseller_id="res-1",
        template=template,
        template_id="tmpl-1",
        merchant_id="merchant-1",
        status=status,
        outcome=outcome,
        call_id=CALL_ID,
        call_direction=direction,
        execution_mode=ExecutionMode.TELEPHONY,
    )


class World:
    """One lead row with the real claim rule, plus counters for side effects."""

    def __init__(self, lead: LeadCallTracker, config: Any = CONFIG) -> None:
        self.row = lead
        self.config = config
        self.updates: List[Dict[str, Any]] = []
        self.transitions = 0
        self.released: List[str] = []
        self.inbound_released: List[str] = []
        self.tokens: List[str] = []
        self.retries: List[Optional[str]] = []
        self.unlocked: List[str] = []
        self.fail_claim = False

    def snapshot(self) -> LeadCallTracker:
        return self.row.model_copy()

    async def update(
        self,
        id: str,
        status: Optional[LeadCallStatus] = None,
        outcome: Optional[str] = None,
        meta_data: Optional[Dict[str, Any]] = None,
        call_end_time: Any = None,
        expected_status: Optional[LeadCallStatus] = None,
    ) -> Optional[LeadCallTracker]:
        self.updates.append(
            {
                "status": status,
                "outcome": outcome,
                "meta_data": meta_data,
                "expected_status": expected_status,
            }
        )
        await asyncio.sleep(0)  # let racing enders interleave here
        if self.fail_claim and expected_status is not None:
            return None  # a DB error looks like a lost claim
        if expected_status is not None and self.row.status != expected_status:
            return None
        if status == LeadCallStatus.FINISHED and expected_status is not None:
            # The claim returns the line itself (num-1 is a Plivo number, so
            # both directions hold one); the SQL rule is checked on Postgres.
            inbound = self.row.call_direction == CallDirection.INBOUND
            (self.inbound_released if inbound else self.released).append("num-1")
        changes: Dict[str, Any] = {}
        if status is not None:
            changes["status"] = status
            if status == LeadCallStatus.FINISHED:
                self.transitions += 1
        if outcome is not None:
            changes["outcome"] = outcome
        if meta_data is not None:
            changes["metaData"] = meta_data
        if call_end_time is not None:
            changes["call_end_time"] = call_end_time
        self.row = self.row.model_copy(update=changes)
        return self.snapshot()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "World":
        number = TelephonyNumber(
            id="num-1",
            number="+15551230000",
            provider=CallProvider.PLIVO,
            status=TelephonyNumberStatus.AVAILABLE,
            channels=1,
            maximum_channels=5,
        )
        real_sleep = asyncio.sleep

        async def get_lead(_call_id: str) -> LeadCallTracker:
            await real_sleep(0)
            return self.snapshot()

        async def acquire(lead_id: str, expected_status=None, force=False):
            await real_sleep(0)
            if expected_status is not None and self.row.status != expected_status:
                return None
            return self.snapshot()

        async def unlock(lead_id: str, *_a: Any, **_k: Any) -> None:
            self.unlocked.append(lead_id)

        async def stale(*_a: Any, **_k: Any) -> List[LeadCallTracker]:
            if self.row.status == LeadCallStatus.PROCESSING:
                return [self.snapshot()]
            return []

        async def get_config(_lead: LeadCallTracker) -> Any:
            return self.config

        async def retry(lead: LeadCallTracker, config: Any, outcome=None) -> None:
            self.retries.append(outcome)

        async def get_number(_number_id: str) -> TelephonyNumber:
            return number

        async def release_number(number_id: str, _provider: CallProvider) -> None:
            self.released.append(number_id)

        async def release_token(number_id: str, _token=None) -> bool:
            self.tokens.append(number_id)
            return True

        async def decrement(number_id: str) -> None:
            self.inbound_released.append(number_id)

        async def noop(*_a: Any, **_k: Any) -> None:
            return None

        async def no_wait(_seconds: float) -> None:
            await real_sleep(0)

        class _Redis:
            async def delete(self, *_a: Any) -> None:
                return None

        async def redis() -> _Redis:
            return _Redis()

        monkeypatch.setattr(calls_mod, "get_lead_by_call_id", get_lead)
        monkeypatch.setattr(
            calls_mod, "update_lead_call_completion_details", self.update
        )
        monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", acquire)
        monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", unlock)
        monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", stale)
        monkeypatch.setattr(calls_mod, "_get_lead_config", get_config)
        monkeypatch.setattr(calls_mod, "_retry_call", retry)
        monkeypatch.setattr(calls_mod, "safe_release_pod", noop)
        monkeypatch.setattr(calls_mod, "get_redis_service", redis)
        monkeypatch.setattr(calls_mod, "raise_long_running_call", noop)
        monkeypatch.setattr(calls_mod, "raise_orphan_webhook", noop)
        monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", get_number)
        monkeypatch.setattr(calls_mod, "_release_number", release_number)
        monkeypatch.setattr(calls_mod, "release_channel_token", release_token)
        monkeypatch.setattr(asyncio, "sleep", no_wait)
        return self


# --- handle_call_completion ---------------------------------------------------


@pytest.mark.asyncio
async def test_completion_winner_releases_once_and_retries(monkeypatch):
    w = World(make_lead()).install(monkeypatch)

    result = await calls_mod.handle_call_completion(CALL_ID, outcome="NO_ANSWER")

    assert w.released == ["num-1"] and w.tokens == ["num-1"]
    assert w.retries == ["NO_ANSWER"]
    assert w.updates[0]["status"] == LeadCallStatus.FINISHED
    assert w.updates[0]["expected_status"] == LeadCallStatus.PROCESSING
    assert result is not None and result.status == LeadCallStatus.FINISHED


@pytest.mark.asyncio
async def test_completion_loser_keeps_agent_data_and_releases_nothing(monkeypatch):
    w = World(make_lead(status=LeadCallStatus.FINISHED, outcome="UNKNOWN"))
    w.install(monkeypatch)

    result = await calls_mod.handle_call_completion(
        CALL_ID, outcome="INTERESTED", meta_data={"t": 1}
    )

    assert w.released == [] and w.tokens == [] and w.retries == []
    assert w.row.outcome == "INTERESTED" and w.row.metaData == {"t": 1}
    assert result is not None and result.outcome == "INTERESTED"


@pytest.mark.asyncio
async def test_completion_loser_does_not_refire_the_finished_hook(monkeypatch):
    w = World(make_lead(status=LeadCallStatus.FINISHED, outcome="UNKNOWN"))
    w.install(monkeypatch)

    await calls_mod.handle_call_completion(CALL_ID, outcome="INTERESTED")

    # The CRM mirror fires only on a write carrying status=FINISHED that
    # returns a row; the loser's data write carries neither.
    assert w.updates[1]["status"] is None
    assert w.updates[1]["expected_status"] is None
    assert w.transitions == 0


@pytest.mark.asyncio
async def test_inbound_completion_releases_with_the_pre_claim_snapshot(monkeypatch):
    w = World(make_lead(direction=CallDirection.INBOUND)).install(monkeypatch)

    await calls_mod.handle_call_completion(CALL_ID, outcome="RESOLVED")

    assert w.inbound_released == ["num-1"], "the inbound channel must go back"
    assert w.tokens == [], "inbound never takes a Redis token"
    assert w.retries == []


@pytest.mark.asyncio
async def test_duplicate_completion_releases_once(monkeypatch):
    w = World(make_lead()).install(monkeypatch)

    await calls_mod.handle_call_completion(CALL_ID, outcome="BUSY")
    await calls_mod.handle_call_completion(CALL_ID, outcome="BUSY")

    assert w.released == ["num-1"] and w.retries == ["BUSY"]


@pytest.mark.asyncio
async def test_completion_without_config_still_finishes_and_releases(monkeypatch):
    """No call config (e.g. an inbound support template): finish at once and
    free the line; only the retry needs a config."""
    w = World(make_lead(direction=CallDirection.INBOUND), config=None)
    w.install(monkeypatch)

    result = await calls_mod.handle_call_completion(CALL_ID, outcome="RESOLVED")

    assert w.row.status == LeadCallStatus.FINISHED
    assert w.row.outcome == "RESOLVED", "the agent's outcome must be saved"
    assert w.inbound_released == ["num-1"]
    assert w.retries == []
    assert result is not None


@pytest.mark.asyncio
async def test_transfer_outcome_override_reaches_the_claim(monkeypatch):
    w = World(make_lead()).install(monkeypatch)

    await calls_mod.handle_call_completion(
        CALL_ID, outcome="INTERESTED", meta_data={"transfer": {"status": "success"}}
    )

    assert w.updates[0]["outcome"] == "TRANSFERRED"
    assert w.retries == []


@pytest.mark.asyncio
async def test_completion_claim_error_neither_releases_nor_retries(monkeypatch):
    """A DB error on the claim looks like a lost claim: the reaper closes the
    lead later and releases once, instead of now and again later."""
    w = World(make_lead()).install(monkeypatch)
    w.fail_claim = True

    await calls_mod.handle_call_completion(CALL_ID, outcome="NO_ANSWER")

    assert w.released == [] and w.retries == []


# --- handle_unanswered_calls --------------------------------------------------


@pytest.mark.asyncio
async def test_unanswered_winner_releases_once_and_retries(monkeypatch):
    w = World(make_lead()).install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_ID)

    assert w.released == ["num-1"] and w.retries == ["NO_ANSWER"]
    assert w.row.status == LeadCallStatus.FINISHED
    assert w.row.outcome == "NO_ANSWER"


@pytest.mark.asyncio
async def test_duplicate_unanswered_webhook_releases_nothing_more(monkeypatch):
    """B1: today the line is released before the FINISHED guard, so a repeated
    status webhook returned it twice."""
    w = World(make_lead()).install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_ID)
    await calls_mod.handle_unanswered_calls(CALL_ID)

    assert w.released == ["num-1"] and w.retries == ["NO_ANSWER"]


@pytest.mark.asyncio
async def test_unanswered_after_completion_is_a_noop(monkeypatch):
    w = World(make_lead(status=LeadCallStatus.FINISHED, outcome="INTERESTED"))
    w.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_ID)

    assert w.released == [] and w.retries == []
    assert w.row.outcome == "INTERESTED"


@pytest.mark.asyncio
async def test_unanswered_without_config_finishes_and_releases(monkeypatch):
    w = World(make_lead(), config=None).install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_ID)

    assert w.row.status == LeadCallStatus.FINISHED
    assert w.released == ["num-1"] and w.retries == []


@pytest.mark.asyncio
async def test_unanswered_claim_error_neither_releases_nor_retries(monkeypatch):
    w = World(make_lead()).install(monkeypatch)
    w.fail_claim = True

    await calls_mod.handle_unanswered_calls(CALL_ID)

    assert w.released == [] and w.retries == []


# --- reaper and 30 s reconcile -----------------------------------------------


@pytest.mark.asyncio
async def test_reaper_winner_releases_and_retries_once(monkeypatch):
    w = World(make_lead()).install(monkeypatch)

    await calls_mod.reconcile_stuck_processing_leads()

    assert w.released == ["num-1"] and w.retries == [None]
    assert w.updates[0]["expected_status"] == LeadCallStatus.PROCESSING
    assert w.unlocked == ["lead-1"]


@pytest.mark.asyncio
async def test_reaper_lost_claim_skips_release_and_retry(monkeypatch):
    w = World(make_lead()).install(monkeypatch)
    w.fail_claim = True

    await calls_mod.reconcile_stuck_processing_leads()

    assert w.released == [] and w.retries == []
    assert w.unlocked == ["lead-1"], "the lock must still be released"


@pytest.mark.asyncio
async def test_completed_reconcile_lost_claim_skips_release_and_retry(monkeypatch):
    w = World(make_lead()).install(monkeypatch)
    w.fail_claim = True

    await calls_mod.reconcile_completed_call(CALL_ID)

    assert w.released == [] and w.retries == []
    assert w.unlocked == ["lead-1"]


@pytest.mark.asyncio
async def test_four_enders_racing_release_exactly_once(monkeypatch):
    w = World(make_lead()).install(monkeypatch)

    await asyncio.gather(
        calls_mod.handle_call_completion(CALL_ID, outcome="NO_ANSWER"),
        calls_mod.handle_unanswered_calls(CALL_ID),
        calls_mod.reconcile_completed_call(CALL_ID),
        calls_mod.reconcile_stuck_processing_leads(),
    )

    assert w.transitions == 1, "exactly one FINISHED transition"
    assert w.released == ["num-1"], "the line goes back exactly once"
    assert len(w.retries) <= 1


# --- the claim on real Postgres ------------------------------------------------

DSN = os.environ.get("DIALQ_TEST_DSN")
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app/database/migrations/086_create_dial_queue.sql"
)


@pytest.mark.asyncio
@pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN to a scratch Postgres")
async def test_claim_is_atomic_on_postgres():
    """20 connections race the real claim (which also returns the line) on one lead."""
    import asyncpg

    from app.database.queries.breeze_buddy.lead_call_tracker import (
        update_lead_call_completion_details_query,
    )

    admin = await asyncpg.connect(DSN)
    await admin.execute("""
        DROP TABLE IF EXISTS lead_call_tracker, telephony_numbers, dial_queue;
        CREATE TABLE lead_call_tracker (
            id text PRIMARY KEY, status text, outcome text, meta_data jsonb,
            call_end_time timestamptz, updated_at timestamptz,
            call_direction text, execution_mode text, telephony_number_id text);
        CREATE TABLE telephony_numbers (
            id text PRIMARY KEY, channels int, maximum_channels int,
            status text, updated_at timestamptz, provider text);
        """)
    # The finishing claim also touches dial_queue. No row here (an old-path
    # call), so the caller's own release runs, as this test does.
    await admin.execute(MIGRATION.read_text())
    conns = [await asyncpg.connect(DSN) for _ in range(20)]
    try:
        for _ in range(30):
            await admin.execute(
                "TRUNCATE lead_call_tracker, telephony_numbers;"
                "INSERT INTO lead_call_tracker (id, status, call_direction, execution_mode,"
                " telephony_number_id)"
                " VALUES ('l1', 'PROCESSING', 'OUTBOUND', 'TELEPHONY', 'n1');"
                "INSERT INTO telephony_numbers"
                " VALUES ('n1', 5, 5, 'AVAILABLE', now(), 'PLIVO');"
            )
            claim_sql, claim_args = update_lead_call_completion_details_query(
                "l1",
                LeadCallStatus.FINISHED,
                "NO_ANSWER",
                {},
                None,
                LeadCallStatus.PROCESSING,
            )

            async def ender(conn) -> int:
                return 1 if await conn.fetch(claim_sql, *claim_args) else 0

            winners = sum(await asyncio.gather(*(ender(c) for c in conns)))
            channels = await admin.fetchval(
                "SELECT channels FROM telephony_numbers WHERE id = 'n1'"
            )
            assert winners == 1
            assert channels == 4
    finally:
        for c in conns:
            await c.close()
        await admin.execute(
            "DROP TABLE IF EXISTS lead_call_tracker, telephony_numbers, dial_queue;"
        )
        await admin.close()


# (direction, mode, provider, number status) -> (channels after, status after)
RULE = [
    ("OUTBOUND", "TELEPHONY", "PLIVO", "AVAILABLE", 4, "AVAILABLE"),
    ("OUTBOUND", "TELEPHONY_TEST", "EXOTEL", "AVAILABLE", 4, "AVAILABLE"),
    ("OUTBOUND", "TELEPHONY", "VOBIZ", "AVAILABLE", 4, "AVAILABLE"),
    ("OUTBOUND", "DAILY", "PLIVO", "AVAILABLE", 5, "AVAILABLE"),  # no line held
    ("OUTBOUND", "HOLD_TRANSFER", "PLIVO", "AVAILABLE", 5, "AVAILABLE"),
    ("OUTBOUND", "TELEPHONY", "TWILIO", "IN_USE", 5, "AVAILABLE"),
    ("OUTBOUND", "TELEPHONY", "TWILIO", "DISABLED", 5, "DISABLED"),  # not re-enabled
    ("INBOUND", "TELEPHONY", "PLIVO", "AVAILABLE", 4, "AVAILABLE"),
    ("INBOUND", "TELEPHONY", "VOBIZ", "AVAILABLE", 4, "AVAILABLE"),
    ("INBOUND", "TELEPHONY", "EXOTEL", "AVAILABLE", 5, "AVAILABLE"),  # not gated
    ("INBOUND", "TELEPHONY", "TWILIO", "IN_USE", 5, "IN_USE"),
]


@pytest.mark.asyncio
@pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN to a scratch Postgres")
async def test_the_claim_returns_the_line_by_todays_rule_on_postgres():
    """The finishing claim reads the number itself and applies today's rule;
    a second claim returns nothing more."""
    import asyncpg

    from app.database.queries.breeze_buddy.lead_call_tracker import (
        update_lead_call_completion_details_query,
    )

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("""
            DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;
            CREATE TABLE dial_queue (lead_id text PRIMARY KEY, state text,
                number_id text);  -- the claim also drops the lead's row
            CREATE TABLE lead_call_tracker (
                id text PRIMARY KEY, status text, outcome text, meta_data jsonb,
                call_end_time timestamptz, updated_at timestamptz,
                call_direction text, execution_mode text, telephony_number_id text);
            CREATE TABLE telephony_numbers (
                id text PRIMARY KEY, channels int, status text,
                updated_at timestamptz, provider text);
            """)
        claim = update_lead_call_completion_details_query(
            "l1", LeadCallStatus.FINISHED, "X", {}, None, LeadCallStatus.PROCESSING
        )
        for direction, mode, provider, nstatus, channels, status_after in RULE:
            await conn.execute("TRUNCATE lead_call_tracker, telephony_numbers")
            await conn.execute(
                "INSERT INTO telephony_numbers VALUES ('n1', 5, $1, now(), $2)",
                nstatus,
                provider,
            )
            await conn.execute(
                "INSERT INTO lead_call_tracker (id, status, call_direction,"
                " execution_mode, telephony_number_id)"
                " VALUES ('l1', 'PROCESSING', $1, $2, 'n1')",
                direction,
                mode,
            )
            assert await conn.fetch(claim[0], *claim[1])
            assert (
                await conn.fetch(claim[0], *claim[1]) == []
            ), "a second claim wins nothing"
            got = await conn.fetchrow("SELECT channels, status FROM telephony_numbers")
            case = (direction, mode, provider, nstatus)
            assert (got["channels"], got["status"]) == (channels, status_after), case
    finally:
        await conn.execute(
            "DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;"
        )
        await conn.close()
