"""dial_queue SQL on real Postgres (DIALQ_TEST_DSN; skipped in CI).

The shipped migration file is executed, so the DDL under test is the real
one. lead_call_tracker is a minimal stand-in with the columns the SQL reads.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.database.queries.breeze_buddy.dial_queue import (
    delete_waiting_dial_query,
    enqueue_dial_query,
    feed_dial_queue_query,
)

DSN = os.environ.get("DIALQ_TEST_DSN")
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "app/database/migrations/086_create_dial_queue.sql"
)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TPL = str(uuid.uuid4())

pytestmark = pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN")


@pytest.fixture
async def db():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    await conn.execute("""
        DROP TABLE IF EXISTS dial_queue, lead_call_tracker;
        CREATE TABLE lead_call_tracker (
            id varchar(255) PRIMARY KEY, merchant_id varchar(100),
            reseller_id varchar(255), template_id uuid, status varchar(50),
            call_direction text, execution_mode text, is_locked boolean DEFAULT false,
            next_attempt_at timestamptz);
        """)
    await conn.execute(MIGRATION.read_text())
    yield conn
    await conn.execute("DROP TABLE IF EXISTS dial_queue, lead_call_tracker;")
    await conn.close()


async def lead(conn: Any, lid: str, **over: Any) -> None:
    row = {
        "merchant_id": "m1",
        "reseller_id": "r1",
        "template_id": TPL,
        "status": "BACKLOG",
        "call_direction": "OUTBOUND",
        "execution_mode": "TELEPHONY",
        "next_attempt_at": NOW,
        **over,
    }
    await conn.execute(
        "INSERT INTO lead_call_tracker (id, merchant_id, reseller_id, template_id,"
        " status, call_direction, execution_mode, next_attempt_at)"
        " VALUES ($1,$2,$3,$4::uuid,$5,$6,$7,$8)",
        lid,
        *row.values(),
    )


async def run(conn: Any, built: tuple) -> Any:
    sql, values = built
    return await conn.fetch(sql, *values)


async def test_migration_creates_the_table_and_both_indexes(db):
    names = {
        r["indexname"]
        for r in await db.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'dial_queue'"
        )
    }
    assert {"dial_queue_pkey", "dial_queue_waiting_ix", "dial_queue_busy_ix"} <= names


async def test_enqueue_inserts_only_dispatchable_backlog(db):
    await lead(db, "ok")
    await lead(db, "done", status="FINISHED")
    await lead(db, "web", execution_mode="DAILY")
    await lead(db, "consult", execution_mode="HOLD_TRANSFER")
    await lead(db, "inbound", call_direction="INBOUND")
    await lead(db, "notpl", template_id=None)
    for lid in ["ok", "done", "web", "consult", "inbound", "notpl"]:
        await run(db, enqueue_dial_query(lid, NOW))

    rows = await db.fetch("SELECT lead_id, merchant_id, state FROM dial_queue")
    assert [(r["lead_id"], r["merchant_id"], r["state"]) for r in rows] == [
        ("ok", "m1", "waiting")
    ]


async def test_no_merchant_falls_back_to_reseller(db):
    await lead(db, "L1", merchant_id=None)
    await run(db, enqueue_dial_query("L1", NOW))
    assert await db.fetchval("SELECT merchant_id FROM dial_queue") == "r1"


async def test_enqueue_moves_earlier_never_later(db):
    await lead(db, "L1")
    await run(db, enqueue_dial_query("L1", NOW))
    await run(db, enqueue_dial_query("L1", NOW + timedelta(minutes=5)))
    assert await db.fetchval("SELECT due_at FROM dial_queue") == NOW
    await run(db, enqueue_dial_query("L1", NOW - timedelta(minutes=5)))
    assert await db.fetchval("SELECT due_at FROM dial_queue") == NOW - timedelta(
        minutes=5
    )


async def test_enqueue_never_touches_an_in_flight_row(db):
    await lead(db, "L1")
    await run(db, enqueue_dial_query("L1", NOW))
    await db.execute(
        "UPDATE dial_queue SET state = 'granted', number_id = 'N1', ticket = gen_random_uuid()"
    )
    await run(db, enqueue_dial_query("L1", NOW - timedelta(hours=1)))
    row = await db.fetchrow("SELECT state, due_at FROM dial_queue")
    assert row["state"] == "granted" and row["due_at"] == NOW


async def test_feeder_adds_only_missing_rows_and_is_idempotent(db):
    for i in range(5):
        await lead(db, f"L{i}")
    await lead(db, "locked")
    await db.execute(
        "UPDATE lead_call_tracker SET is_locked = true WHERE id = 'locked'"
    )
    await run(db, enqueue_dial_query("L0", NOW))

    added = await run(db, feed_dial_queue_query(1000))
    again = await run(db, feed_dial_queue_query(1000))

    assert len(added) == 4 and again == []
    assert await db.fetchval("SELECT count(*) FROM dial_queue") == 5


async def test_feeder_reaches_a_cold_merchant_behind_a_hot_backlog(db):
    """3,000 hot leads already queued and older; the cold one is still fed."""
    await db.execute(
        "INSERT INTO lead_call_tracker (id, merchant_id, template_id, status,"
        " call_direction, execution_mode, next_attempt_at)"
        " SELECT 'hot' || g, 'hot', $1::uuid, 'BACKLOG', 'OUTBOUND', 'TELEPHONY',"
        " $2::timestamptz - interval '1 hour' FROM generate_series(1, 3000) g",
        TPL,
        NOW,
    )
    await db.execute(
        "INSERT INTO dial_queue (lead_id, merchant_id, template_id, due_at)"
        " SELECT id, merchant_id, template_id, next_attempt_at FROM lead_call_tracker"
    )
    await lead(db, "cold", merchant_id="cold")

    added = await run(db, feed_dial_queue_query(1000))

    assert [r["lead_id"] for r in added] == ["cold"]


async def test_delete_waiting_leaves_in_flight_rows(db):
    await lead(db, "W")
    await lead(db, "G")
    await run(db, enqueue_dial_query("W", NOW))
    await run(db, enqueue_dial_query("G", NOW))
    await db.execute(
        "UPDATE dial_queue SET state = 'granted', number_id = 'N1' WHERE lead_id = 'G'"
    )
    await run(db, delete_waiting_dial_query("W"))
    await run(db, delete_waiting_dial_query("G"))
    assert [r["lead_id"] for r in await db.fetch("SELECT lead_id FROM dial_queue")] == [
        "G"
    ]
