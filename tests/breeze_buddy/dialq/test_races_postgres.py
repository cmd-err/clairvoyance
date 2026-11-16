"""PR4 SQL under real concurrency (DIALQ_TEST_DSN; skipped in CI).

Give-back, the finishing claim, cancel and a fill-style grant racing each
other on real Postgres, each repeated with concurrent connections. The
shipped migration and the real query builders are used.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pytest

from app.database.queries.breeze_buddy.dial_queue import (
    claim_dialing_query,
    drop_ended_live_rows_query,
    give_back_dial_query,
    lock_dial_row_query,
    lock_lead_query,
    mark_dial_live_query,
    stuck_dials_query,
)
from app.database.queries.breeze_buddy.lead_call_tracker import (
    abort_lead_by_id_query,
    adopt_unknown_dial_query,
    update_lead_call_completion_details_query,
)
from app.schemas import LeadCallStatus

DSN = os.environ.get("DIALQ_TEST_DSN")
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "app/database/migrations/086_create_dial_queue.sql"
)
TPL = str(uuid.uuid4())

pytestmark = pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN")


@pytest.fixture
async def db():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    await conn.execute("""
        DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;
        CREATE TABLE lead_call_tracker (
            id text PRIMARY KEY, status text, outcome text, meta_data jsonb,
            call_end_time timestamptz, updated_at timestamptz,
            next_attempt_at timestamptz, call_id text,
            call_initiated_time timestamptz, telephony_number_id text DEFAULT 'N1',
            call_direction text DEFAULT 'OUTBOUND',
            execution_mode text DEFAULT 'TELEPHONY');
        CREATE TABLE telephony_numbers (
            id text PRIMARY KEY, channels int, maximum_channels int,
            updated_at timestamptz, status text DEFAULT 'AVAILABLE',
            provider text DEFAULT 'PLIVO');
        """)
    await conn.execute(MIGRATION.read_text())
    conns = [await asyncpg.connect(DSN) for _ in range(8)]
    yield conn, conns
    for c in conns:
        await c.close()
    await conn.execute(
        "DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;"
    )
    await conn.close()


async def reset(conn: Any, channels: int = 5) -> None:
    await conn.execute(
        "TRUNCATE dial_queue, lead_call_tracker, telephony_numbers;"
        f"INSERT INTO telephony_numbers VALUES ('N1', {channels}, 10, now());"
    )


async def lead(conn: Any, lid: str, status: str = "BACKLOG", nxt: Any = None) -> None:
    await conn.execute(
        "INSERT INTO lead_call_tracker (id, status, next_attempt_at) VALUES ($1,$2,$3)",
        lid,
        status,
        nxt or datetime.now(timezone.utc),
    )


async def row(
    conn: Any, lid: str, state: str, ticket: Any = None, age_min: int = 0
) -> str:
    ticket = ticket or str(uuid.uuid4())
    number = None if state == "waiting" else "N1"
    await conn.execute(
        "INSERT INTO dial_queue (lead_id, merchant_id, template_id, due_at, state,"
        " number_id, ticket, state_at)"
        " VALUES ($1, 'm1', $2::uuid, now(), $3, $4, $5::uuid,"
        " now() - make_interval(mins => $6))",
        lid,
        TPL,
        state,
        number,
        None if state == "waiting" else ticket,
        age_min,
    )
    return ticket


async def q(conn: Any, built: tuple) -> List[Any]:
    sql, values = built
    return await conn.fetch(sql, *values)


async def channels(conn: Any) -> int:
    return await conn.fetchval("SELECT channels FROM telephony_numbers WHERE id='N1'")


def finish(lid: str) -> tuple:
    return update_lead_call_completion_details_query(
        lid,
        LeadCallStatus.FINISHED,
        "NO_ANSWER",
        {},
        None,
        LeadCallStatus.PROCESSING,
    )


async def abort(conn: Any, lid: str, reason: str) -> List[Any]:
    """The accessor's transaction: the lead, then the row, then the abort."""
    async with conn.transaction():
        await q(conn, lock_lead_query(lid))
        await q(conn, lock_dial_row_query(lid))
        return await q(conn, abort_lead_by_id_query(lid, reason))


async def test_give_back_requeues_backlog_and_deletes_finished(db):
    conn, _ = db
    await reset(conn)
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    await lead(conn, "B", "BACKLOG", later)
    await lead(conn, "F", "FINISHED")
    tb = await row(conn, "B", "granted")
    tf = await row(conn, "F", "dialing")

    assert await q(conn, give_back_dial_query("B", tb))
    assert await q(conn, give_back_dial_query("F", tf))

    b = await conn.fetchrow("SELECT state, number_id, ticket, due_at FROM dial_queue")
    assert b["state"] == "waiting" and b["number_id"] is None and b["ticket"] is None
    assert b["due_at"] >= later
    assert await channels(conn) == 3


async def test_give_back_twice_returns_once_and_never_touches_live(db):
    conn, _ = db
    await reset(conn)
    await lead(conn, "L")
    await lead(conn, "V")
    t = await row(conn, "L", "granted")
    tv = await row(conn, "V", "live")

    assert await q(conn, give_back_dial_query("L", t))
    assert await q(conn, give_back_dial_query("L", t)) == []
    assert await q(conn, give_back_dial_query("V", tv)) == []
    assert await channels(conn) == 4


async def test_give_back_vs_end(db):
    """Adopted/placed call: two give-backs and two finishing claims at once.
    The line goes back exactly once."""
    conn, conns = db
    for _ in range(40):
        await reset(conn, channels=6)  # 5 other calls + this one
        await lead(conn, "L", "PROCESSING")
        t = await row(conn, "L", "dialing")
        await asyncio.gather(
            q(conns[0], give_back_dial_query("L", t)),
            q(conns[1], give_back_dial_query("L", t)),
            q(conns[2], finish("L")),
            q(conns[3], finish("L")),
        )
        assert await channels(conn) == 5
        assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


async def test_double_finish_releases_the_line_once(db):
    conn, conns = db
    for _ in range(30):
        await reset(conn)
        await lead(conn, "L", "PROCESSING")
        await row(conn, "L", "live")
        results = await asyncio.gather(*(q(c, finish("L")) for c in conns))
        won = [r for r in results if r]
        assert len(won) == 1
        assert await channels(conn) == 4


async def test_finish_of_an_old_path_lead_returns_its_line_itself(db):
    conn, _ = db
    await reset(conn)
    await lead(conn, "L", "PROCESSING")
    await row(conn, "L", "waiting")

    [won] = await q(conn, finish("L"))

    assert won["status"] == "FINISHED"
    assert await channels(conn) == 4, "the line passed in, in the same statement"
    assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


async def test_cancel_matrix(db):
    conn, _ = db
    await reset(conn)
    for lid, state in [("W", "waiting"), ("G", "granted"), ("N", None)]:
        await lead(conn, lid)
        if state:
            await row(conn, lid, state)

    assert await abort(conn, "W", "paid"), "cancelled"
    assert await abort(conn, "G", "paid") == [], "too late"
    assert await abort(conn, "N", "paid"), "no row: cancelled"
    assert await abort(conn, "W", "paid") == [], "only once"

    states = dict(await conn.fetch("SELECT id, status FROM lead_call_tracker"))
    assert states == {"W": "FINISHED", "G": "BACKLOG", "N": "FINISHED"}
    assert [
        r["lead_id"] for r in await conn.fetch("SELECT lead_id FROM dial_queue")
    ] == ["G"]


async def test_cancel_vs_grant(db):
    """A fill-style grant (number row locked, rows granted, channels + count)
    racing five cancels: each lead is granted XOR cancelled, and channels
    equals the rows granted."""
    conn, conns = db
    ids = [f"L{i}" for i in range(5)]
    for _ in range(30):
        await reset(conn, channels=0)
        for lid in ids:
            await lead(conn, lid)
            await row(conn, lid, "waiting")

        async def grant() -> int:
            async with conns[0].transaction():
                await conns[0].execute(
                    "SELECT 1 FROM telephony_numbers WHERE id='N1' FOR UPDATE"
                )
                got = await conns[0].fetch(
                    "UPDATE dial_queue SET state='granted', number_id='N1',"
                    " ticket=gen_random_uuid(), state_at=now()"
                    " WHERE lead_id = ANY($1) AND state='waiting' RETURNING lead_id",
                    ids,
                )
                await conns[0].execute(
                    "UPDATE telephony_numbers SET channels = channels + $1 WHERE id='N1'",
                    len(got),
                )
                return len(got)

        results = await asyncio.gather(
            grant(),
            *(abort(conns[i + 1], lid, "x") for i, lid in enumerate(ids)),
        )
        granted = results[0]
        cancelled = sum(1 for r in results[1:] if r)
        assert granted + cancelled == 5
        assert await channels(conn) == granted
        uncancelled = await conn.fetchval(
            "SELECT count(*) FROM dial_queue WHERE cancel_reason IS NULL"
        )
        assert uncancelled == 0, "a granted lead's cancel was lost"
        assert (
            await conn.fetchval("SELECT count(*) FROM dial_queue WHERE state='granted'")
            == granted
        )


async def test_crash_after_grant_is_repaired_and_the_old_ticket_is_dead(db):
    conn, _ = db
    await reset(conn, channels=1)
    await lead(conn, "L")
    old = await row(conn, "L", "granted", age_min=11)

    stuck = await q(conn, stuck_dials_query())
    assert [(r["lead_id"], r["state"]) for r in stuck] == [("L", "granted")]
    assert await q(conn, give_back_dial_query("L", stuck[0]["ticket"]))
    assert await channels(conn) == 0

    # The next fill grants it again with a new ticket.
    new = str(uuid.uuid4())
    await conn.execute(
        "UPDATE dial_queue SET state='granted', number_id='N1', ticket=$1::uuid,"
        " state_at=now() WHERE lead_id='L'",
        new,
    )
    await conn.execute("UPDATE telephony_numbers SET channels = 1")

    assert await q(conn, claim_dialing_query("L", old)) == [], "old ticket cannot dial"
    assert await q(conn, give_back_dial_query("L", old)) == [], "nor return the line"
    assert await q(conn, claim_dialing_query("L", new))
    assert await channels(conn) == 1


async def test_dialing_stuck_with_a_processing_lead_becomes_live(db):
    conn, _ = db
    await reset(conn)
    await lead(conn, "L", "PROCESSING")
    t = await row(conn, "L", "dialing", age_min=6)

    [stuck] = await q(conn, stuck_dials_query())
    assert stuck["lead_status"] == "PROCESSING"
    assert await q(conn, mark_dial_live_query("L", t))
    assert await conn.fetchval("SELECT state FROM dial_queue") == "live"
    assert await channels(conn) == 5


async def end_call(conn: Any, lid: str) -> None:
    """The enders: the finishing claim returns the call's line itself."""
    assert await q(conn, finish(lid))


async def test_lost_mark_live_keeps_the_line_for_the_call(db):
    """Dial placed, PROCESSING written, mark_dial_live lost: the finally's
    give-back makes the row live instead of returning the line mid-call."""
    conn, _ = db
    await reset(conn, channels=6)  # 5 other calls + this one
    await lead(conn, "L", "PROCESSING")
    t = await row(conn, "L", "dialing")

    assert await q(conn, give_back_dial_query("L", t)) == []
    assert await conn.fetchval("SELECT state FROM dial_queue") == "live"
    assert await channels(conn) == 6

    await end_call(conn, "L")
    assert await channels(conn) == 5


@pytest.mark.parametrize("give_back_first", [False, True])
async def test_adopt_of_a_dialing_row_counts_the_line_once(db, give_back_first):
    """No dial reply; Plivo's webhook adopts the call while the task still
    holds its dialing row. Fill already counted the line."""
    conn, _ = db
    await reset(conn, channels=6)
    await lead(conn, "L", "BACKLOG")
    t = await row(conn, "L", "dialing")

    assert await q(conn, adopt_unknown_dial_query("L", "sid", "N1"))
    assert await conn.fetchval("SELECT state FROM dial_queue") == "live"
    assert await channels(conn) == 6
    if give_back_first:
        assert await q(conn, give_back_dial_query("L", t)) == []
    await end_call(conn, "L")
    if not give_back_first:
        assert await q(conn, give_back_dial_query("L", t)) == []
    assert await channels(conn) == 5
    assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


@pytest.mark.parametrize("state", ["waiting", None])
async def test_adopt_without_a_held_row_counts_and_the_end_returns_it(db, state):
    conn, _ = db
    await reset(conn)
    await lead(conn, "L", "BACKLOG")
    if state:
        await row(conn, "L", state)

    assert await q(conn, adopt_unknown_dial_query("L", "sid", "N1"))
    assert await channels(conn) == 6
    await end_call(conn, "L")
    assert await channels(conn) == 5
    assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


async def test_adopt_vs_give_back(db):
    """The task gives its line back (no reply) while the webhook adopts: in
    either commit order the call ends with the counter back at baseline."""
    conn, conns = db
    for _ in range(40):
        await reset(conn, channels=6)
        await lead(conn, "L", "BACKLOG")
        t = await row(conn, "L", "dialing")
        await asyncio.gather(
            q(conns[0], adopt_unknown_dial_query("L", "sid", "N1")),
            q(conns[1], give_back_dial_query("L", t)),
        )
        await end_call(conn, "L")
        assert await channels(conn) == 5
        assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


async def test_an_old_path_call_ending_leaves_a_granted_row_to_its_task(db):
    """Mixed mode: the old worker won the lead and dialled it (counting its own
    line) while fill had granted the row. The call ends before the grant's
    task gives back: the end must not take the grant's line as the call's."""
    conn, _ = db
    await reset(conn, channels=5 + 2)  # 5 others + old-path call + grant
    await lead(conn, "L", "PROCESSING")
    t = await row(conn, "L", "granted")

    await end_call(conn, "L")
    assert await channels(conn) == 6
    assert await q(conn, give_back_dial_query("L", t))
    assert await channels(conn) == 5
    assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


async def give_back(conn: Any, lid: str, ticket: str, rang: bool = False) -> List[Any]:
    """The accessor's transaction: the lead first, then the give-back."""
    async with conn.transaction():
        await q(conn, lock_lead_query(lid))
        return await q(conn, give_back_dial_query(lid, ticket, rang))


@pytest.mark.parametrize("rang", [False, True])
async def test_a_cancel_too_late_to_stop_the_grant_still_wins(db, rang):
    """Cancel while the line is granted: too late to answer "cancelled", but
    the lead is never dialled and ends as ABORT with the reason (and a note
    when the dial may have rung)."""
    conn, _ = db
    await reset(conn, channels=6)
    await lead(conn, "L")
    t = await row(conn, "L", "granted")

    assert await abort(conn, "L", "paid") == [], "too late"
    assert await q(conn, claim_dialing_query("L", t)) == [], "never dials"
    [freed] = await give_back(conn, "L", t, rang)

    assert freed["cancelled"] is True
    assert await channels(conn) == 5
    done = await conn.fetchrow(
        "SELECT status, outcome, meta_data FROM lead_call_tracker"
    )
    assert (done["status"], done["outcome"]) == ("FINISHED", "ABORT")
    outcome = json.loads(done["meta_data"])["outcome"]
    assert outcome["abort_reason"] == "paid"
    assert outcome.get("abort_note") == ("cancel_after_unknown_dial" if rang else None)
    assert await conn.fetchval("SELECT count(*) FROM dial_queue") == 0


async def test_a_cancel_after_the_call_is_placed_leaves_the_call_alone(db):
    conn, _ = db
    await reset(conn, channels=6)
    await lead(conn, "L")
    t = await row(conn, "L", "granted")
    assert await q(conn, claim_dialing_query("L", t))
    await conn.execute("UPDATE lead_call_tracker SET status='PROCESSING'")

    assert await abort(conn, "L", "paid") == [], "in a call"
    assert await give_back(conn, "L", t) == [], "the row becomes live"
    assert await conn.fetchval("SELECT status FROM lead_call_tracker") == "PROCESSING"
    assert await channels(conn) == 6


async def test_cancel_vs_give_back_never_deadlocks(db):
    """Abort (lead -> row) and give-back (lead -> row -> number) race on one
    lead: no deadlock, one final state, the line returned once."""
    conn, conns = db
    for _ in range(40):
        await reset(conn, channels=6)
        await lead(conn, "L")
        t = await row(conn, "L", "granted")
        await asyncio.gather(
            abort(conns[0], "L", "paid"),
            give_back(conns[1], "L", t),
        )
        assert await channels(conn) == 5
        status = await conn.fetchval("SELECT status FROM lead_call_tracker")
        rows = await conn.fetchval("SELECT count(*) FROM dial_queue")
        # abort first: the cancel is recorded and give-back finishes the
        # lead; give-back first: the row is waiting and abort cancels it.
        assert (status, rows) == ("FINISHED", 0)


async def test_claim_refuses_a_lead_that_is_no_longer_backlog(db):
    conn, _ = db
    await reset(conn)
    await lead(conn, "L", "PROCESSING")  # a webhook attached a call meanwhile
    t = await row(conn, "L", "granted")
    assert await q(conn, claim_dialing_query("L", t)) == []


async def test_repair_drops_live_rows_of_leads_no_longer_in_a_call(db):
    conn, _ = db
    await reset(conn)
    await lead(conn, "F", "FINISHED")
    await lead(conn, "P", "PROCESSING")
    await row(conn, "F", "live")
    await row(conn, "P", "live")

    gone = await q(conn, drop_ended_live_rows_query())

    assert [r["lead_id"] for r in gone] == ["F"]
    assert await channels(conn) == 5, "never touches the counter"
