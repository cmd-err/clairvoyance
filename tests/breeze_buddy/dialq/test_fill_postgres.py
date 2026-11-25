"""fill_number and the tick query on real Postgres (DIALQ_TEST_DSN; skipped in
CI). The real accessor runs, pointed at a scratch pool, so the transaction
under test is the shipped one."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.dialq import pick_leads
from app.database.accessor.breeze_buddy import dial_queue as dq
from app.database.queries.breeze_buddy.dial_queue import (
    due_templates_query,
    give_back_dial_query,
    lock_lead_query,
)
from app.database.queries.breeze_buddy.lead_call_tracker import (
    abort_lead_by_id_query,
    update_lead_call_completion_details_query,
)
from app.database.queries.breeze_buddy.telephony_number import (
    increment_telephony_number_channels_query,
)
from app.schemas import LeadCallStatus

DSN = os.environ.get("DIALQ_TEST_DSN")
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "app/database/migrations/086_create_dial_queue.sql"
)
NOW = datetime.now(timezone.utc)

pytestmark = pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN")


@pytest.fixture
async def db(monkeypatch):
    import asyncpg

    pool = await asyncpg.create_pool(DSN, min_size=2, max_size=12)
    async with pool.acquire() as conn:
        await conn.execute("""
            DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;
            CREATE TABLE lead_call_tracker (
                id text PRIMARY KEY, status text, outcome text, meta_data jsonb,
                call_end_time timestamptz, updated_at timestamptz,
                next_attempt_at timestamptz, call_direction text DEFAULT 'OUTBOUND',
                execution_mode text DEFAULT 'TELEPHONY', telephony_number_id text);
            CREATE TABLE telephony_numbers (
                id text PRIMARY KEY, status text, channels int,
                maximum_channels int, updated_at timestamptz,
                provider text DEFAULT 'PLIVO');
            """)
        await conn.execute(MIGRATION.read_text())

    @asynccontextmanager
    async def connection():
        async with pool.acquire() as conn:
            yield conn

    monkeypatch.setattr(dq, "db_connection", connection)
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;"
        )
    await pool.close()


async def number(
    pool: Any, nid: str = "N1", lines: int = 3, used: int = 0, status="AVAILABLE"
):
    await pool.execute(
        "INSERT INTO telephony_numbers VALUES ($1, $2, $3, $4, now())",
        nid,
        status,
        used,
        lines,
    )


async def waiting(
    pool: Any, lid: str, tpl: str, merchant: str, minutes_ago: int
) -> None:
    due = NOW - timedelta(minutes=minutes_ago)
    await pool.execute(
        "INSERT INTO lead_call_tracker (id, status, next_attempt_at) VALUES ($1,'BACKLOG',$2)",
        lid,
        due,
    )
    await pool.execute(
        "INSERT INTO dial_queue (lead_id, merchant_id, template_id, due_at)"
        " VALUES ($1, $2, $3::uuid, $4)",
        lid,
        merchant,
        tpl,
        due,
    )


async def channels(pool: Any, nid: str = "N1") -> int:
    return await pool.fetchval(
        "SELECT channels FROM telephony_numbers WHERE id=$1", nid
    )


async def test_due_templates_one_row_per_template(db):
    tpls = [str(uuid.uuid4()) for _ in range(4)]
    for i in range(13):
        await waiting(db, f"L{i}", tpls[i % 4], "m", 60 - i)
    future = str(uuid.uuid4())
    await waiting(db, "F", future, "m", -30)

    sql, values = due_templates_query()
    rows = {r["template_id"]: r["due_at"] for r in await db.fetch(sql, *values)}

    assert set(rows) == set(tpls) | {future}
    assert rows[tpls[0]] == NOW - timedelta(minutes=60)


async def test_the_tick_query_reads_one_index_entry_per_template(db):
    """200k waiting rows over 300 templates: an index-only skip-scan, a
    buffer count in the hundreds, nowhere near the 200k rows."""
    await db.execute(
        "INSERT INTO dial_queue (lead_id, merchant_id, template_id, due_at)"
        " SELECT 'L' || g, 'm', t.id, now() - (g % 997) * interval '1 second'"
        " FROM generate_series(1, 200000) g"
        " JOIN (SELECT row_number() OVER () - 1 AS i, gen_random_uuid() AS id"
        "       FROM generate_series(1, 300)) t ON t.i = g % 300"
    )
    await db.execute("VACUUM ANALYZE dial_queue")
    sql, values = due_templates_query()
    plan = "\n".join(
        r[0] for r in await db.fetch("EXPLAIN (ANALYZE, BUFFERS) " + sql, *values)
    )

    assert "Index Only Scan using dial_queue_waiting_ix" in plan
    assert "Seq Scan" not in plan
    # The top-level Buffers line is the whole statement's total.
    top = next(
        line for line in plan.splitlines() if line.strip().startswith("Buffers:")
    )
    hits = int(top.split("hit=")[1].split()[0])
    assert hits < 2000, plan
    assert len(await db.fetch(sql, *values)) == 300


async def abort(pool: Any, lid: str) -> List[Any]:
    """As the accessor: the lead lock first, then the abort."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(*_args(lock_lead_query(lid)))
            return await conn.fetch(*_args(abort_lead_by_id_query(lid, "x")))


async def test_walkthrough_fairness(db):
    """The walkthrough (steps 6, 11, 12, 20) on real rows."""
    await number(db, lines=3)
    hot, slow = str(uuid.uuid4()), str(uuid.uuid4())
    for i in range(1, 5):
        await waiting(db, f"H{i}", hot, "hot", 30 - i)
    for i in range(1, 3):
        await waiting(db, f"S{i}", slow, "slow", 15 - i)

    first = await dq.fill_number("N1", [hot, slow], 3, pick_leads)
    assert sorted(lid for lid, _ in first) == ["H1", "H2", "S1"]
    assert await channels(db) == 3

    tickets = dict(first)
    # H2's merchant pre-check says "later": give-back, the line returns.
    await db.execute(
        "UPDATE lead_call_tracker SET next_attempt_at = now() + interval '1 hour' WHERE id='H2'"
    )
    await db.fetch(*_args(give_back_dial_query("H2", tickets["H2"])))
    assert await channels(db) == 2

    # Hot 1 live, Slow 1 live: a tie, so the older lead (H3) gets it.
    assert [
        lid for lid, _ in await dq.fill_number("N1", [hot, slow], 1, pick_leads)
    ] == ["H3"]

    # S1's call ends: Hot holds 2, Slow 0, so S2 gets the line although H4 is older.
    await db.execute("UPDATE lead_call_tracker SET status='PROCESSING' WHERE id='S1'")
    await db.execute("UPDATE dial_queue SET state='live' WHERE lead_id='S1'")
    await db.fetch(*_args(_finish("S1")))
    assert await channels(db) == 2
    assert [
        lid for lid, _ in await dq.fill_number("N1", [hot, slow], 1, pick_leads)
    ] == ["S2"]


def _args(built: tuple) -> tuple:
    sql, values = built
    return (sql, *values)


def _finish(lid: str) -> tuple:
    return update_lead_call_completion_details_query(
        lid, LeadCallStatus.FINISHED, "NO_ANSWER", {}, None, LeadCallStatus.PROCESSING
    )


async def test_six_pods_fill_one_number(db):
    """Never more than the free lines, no lead twice, channels = granted."""
    tpl = str(uuid.uuid4())
    for _ in range(40):
        await db.execute("TRUNCATE dial_queue, lead_call_tracker, telephony_numbers")
        await number(db, lines=4)
        for i in range(12):
            await waiting(db, f"L{i}", tpl, f"m{i % 3}", i)

        results = await asyncio.gather(
            *(dq.fill_number("N1", [tpl], 4, pick_leads) for _ in range(6))
        )
        granted: List[str] = [lid for r in results for lid, _ in r]
        assert len(granted) <= 4 and len(set(granted)) == len(granted)
        assert await channels(db) == len(granted)
        rows = await db.fetchval(
            "SELECT count(*) FROM dial_queue WHERE state='granted'"
        )
        assert rows == len(granted)


async def test_fill_vs_cancel(db):
    tpl = str(uuid.uuid4())
    ids = [f"L{i}" for i in range(5)]
    for _ in range(40):
        await db.execute("TRUNCATE dial_queue, lead_call_tracker, telephony_numbers")
        await number(db, lines=5)
        for i, lid in enumerate(ids):
            await waiting(db, lid, tpl, "m", i)

        results = await asyncio.gather(
            dq.fill_number("N1", [tpl], 5, pick_leads),
            *(abort(db, lid) for lid in ids),
        )
        granted = {lid for lid, _ in results[0]}
        cancelled = {ids[i] for i, r in enumerate(results[1:]) if r}
        assert granted.isdisjoint(cancelled) and granted | cancelled == set(ids)
        assert await channels(db) == len(granted)


async def test_fill_vs_inbound_never_exceeds_the_lines(db):
    tpl = str(uuid.uuid4())
    for _ in range(40):
        await db.execute("TRUNCATE dial_queue, lead_call_tracker, telephony_numbers")
        await number(db, lines=3)
        for i in range(6):
            await waiting(db, f"L{i}", tpl, "m", i)
        inbound = increment_telephony_number_channels_query("N1")

        results = await asyncio.gather(
            dq.fill_number("N1", [tpl], 3, pick_leads),
            dq.fill_number("N1", [tpl], 3, pick_leads),
            *(db.fetch(*_args(inbound)) for _ in range(3)),
        )
        admitted = sum(1 for r in results[2:] if r)
        granted = sum(len(r) for r in results[:2])
        assert granted + admitted <= 3
        assert await channels(db) == granted + admitted


async def test_fill_skips_an_unavailable_number(db):
    tpl = str(uuid.uuid4())
    await number(db, lines=3, status="DISABLED")
    await waiting(db, "L1", tpl, "m", 1)

    assert await dq.fill_number("N1", [tpl], 3, pick_leads) == []
    assert await channels(db) == 0


async def test_fill_with_300_templates_on_one_number(db):
    """The shared pool number: 300 templates, 200k waiting rows."""
    await number(db, lines=1000)
    await db.execute(
        "INSERT INTO dial_queue (lead_id, merchant_id, template_id, due_at)"
        " SELECT 'L' || g, 'm' || (g % 50), t.id, now() - (g % 997) * interval '1 second'"
        " FROM generate_series(1, 200000) g"
        " JOIN (SELECT row_number() OVER () - 1 AS i, gen_random_uuid() AS id"
        "       FROM generate_series(1, 300)) t ON t.i = g % 300"
    )
    await db.execute("VACUUM ANALYZE dial_queue")
    tpls = [
        r["t"]
        for r in await db.fetch(
            "SELECT DISTINCT template_id::text AS t FROM dial_queue"
        )
    ]

    started = time.perf_counter()
    granted = await dq.fill_number("N1", tpls, 10, pick_leads)
    elapsed = time.perf_counter() - started

    assert len(granted) == 10
    assert elapsed < 0.5, f"fill took {elapsed:.3f}s"


async def test_a_call_end_and_a_fill_on_one_number_never_deadlock(db):
    """An old-path call ends on a just-listed number while its lead still has
    a waiting row: the call end holds that row and wants the number, fill
    holds the number. Fill skips the row instead of waiting: no deadlock, and
    the counter equals the rows granted."""
    tpl = str(uuid.uuid4())
    finish = update_lead_call_completion_details_query(
        "OLD", LeadCallStatus.FINISHED, "X", {}, None, LeadCallStatus.PROCESSING
    )
    for _ in range(40):
        await db.execute("TRUNCATE dial_queue, lead_call_tracker, telephony_numbers")
        await number(db, lines=4, used=1)  # the old-path call holds one
        await waiting(db, "OLD", tpl, "m", 30)  # oldest, so fill picks it
        await db.execute(
            "UPDATE lead_call_tracker SET status = 'PROCESSING',"
            " telephony_number_id = 'N1' WHERE id = 'OLD'"
        )
        for i in range(3):
            await waiting(db, f"L{i}", tpl, "m", i)

        granted, _ = await asyncio.gather(
            dq.fill_number("N1", [tpl], 3, pick_leads), db.fetch(*_args(finish))
        )

        rows = await db.fetchval(
            "SELECT count(*) FROM dial_queue WHERE state = 'granted'"
        )
        assert rows == len(granted)
        assert await channels(db) == rows, "old call's line back, grants counted"
