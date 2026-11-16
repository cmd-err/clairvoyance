"""PR5b: a merchant's concurrent-call cap on the per-number dialler."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

# dispatch must import first (managers.calls <-> dispatch.worker cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.dispatch.dialq import pick_leads
from app.database.accessor.breeze_buddy import (
    dial_queue as dq,
    merchants as merchants_acc,
)

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _c(lead: str, merchant: str, minute: int) -> Dict[str, Any]:
    return {
        "lead_id": lead,
        "merchant_id": merchant,
        "due_at": T0 + timedelta(minutes=minute),
    }


def test_pick_leads_respects_room():
    cands = [_c(f"H{i}", "hot", i) for i in range(5)]
    assert pick_leads(5, {}, cands, room={"hot": 1}) == ["H0"]
    assert pick_leads(5, {}, cands, room={"hot": 0}) == []


def test_a_capped_merchant_does_not_block_others():
    cands = [
        _c("H1", "hot", 0),
        _c("H2", "hot", 1),
        _c("S1", "slow", 5),
        _c("S2", "slow", 6),
    ]
    assert pick_leads(4, {}, cands, room={"hot": 0}) == ["S1", "S2"]


class _Conn:
    """Records the SQL a fill runs; answers like a 3-line number."""

    def __init__(self, merchant: str) -> None:
        self.sql: List[str] = []
        self.merchant = merchant

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql: str, *a: Any) -> Dict[str, int]:
        self.sql.append(sql)
        return {"free": 3}

    async def fetch(self, sql: str, *a: Any) -> List[Dict[str, Any]]:
        self.sql.append(sql)
        if "RETURNING lead_id" in sql:
            return [{"lead_id": lid, "ticket": "t"} for lid in a[1]]
        if "LATERAL" in sql:
            return [_c("L1", self.merchant, 0)]
        return []

    async def execute(self, sql: str, *a: Any) -> None:
        self.sql.append(sql)


@pytest.mark.parametrize(
    "caps,expect_quota_read", [({}, False), ({"m2": 1}, False), ({"m1": 2}, True)]
)
async def test_the_quota_is_read_only_when_a_candidate_is_capped(
    monkeypatch, caps, expect_quota_read
):
    conn = _Conn("m1")

    @asynccontextmanager
    async def connection():
        yield conn

    monkeypatch.setattr(dq, "db_connection", connection)

    await dq.fill_number("N1", [str(uuid.uuid4())], 3, pick_leads, caps)

    assert any("merchant_id = ANY" in s for s in conn.sql) is expect_quota_read


# --- real Postgres ------------------------------------------------------------------

DSN = os.environ.get("DIALQ_TEST_DSN")
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "app/database/migrations/086_create_dial_queue.sql"
)
CAPS_MIGRATION = MIGRATION.with_name("087_merchants_max_concurrent_calls.sql")


@pytest.fixture
async def db(monkeypatch):
    import asyncpg

    pool = await asyncpg.create_pool(DSN, min_size=2, max_size=6)
    async with pool.acquire() as conn:
        await conn.execute("""
            DROP TABLE IF EXISTS dial_queue, telephony_numbers;
            CREATE TABLE telephony_numbers (
                id text PRIMARY KEY, status text, channels int,
                maximum_channels int, updated_at timestamptz);
            INSERT INTO telephony_numbers VALUES
                ('N1', 'AVAILABLE', 0, 5, now()), ('N2', 'AVAILABLE', 0, 5, now());
            """)
        await conn.execute(MIGRATION.read_text())

    @asynccontextmanager
    async def connection():
        async with pool.acquire() as conn:
            yield conn

    monkeypatch.setattr(dq, "db_connection", connection)
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS dial_queue, telephony_numbers;")
    await pool.close()


async def _leads(pool: Any, tpl: str, n: int) -> None:
    await pool.execute(
        "INSERT INTO dial_queue (lead_id, merchant_id, template_id, due_at)"
        " SELECT 'L' || g, 'm1', $1::uuid, now() - g * interval '1 second'"
        " FROM generate_series(1, $2) g",
        tpl,
        n,
    )


@pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN")
async def test_the_cap_holds_across_two_numbers(db):
    tpl = str(uuid.uuid4())
    await _leads(db, tpl, 10)

    first = await dq.fill_number("N1", [tpl], 5, pick_leads, {"m1": 3})
    second = await dq.fill_number("N2", [tpl], 5, pick_leads, {"m1": 3})

    assert len(first) == 3 and second == []


@pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN")
async def test_the_soft_cap_overshoot_is_bounded(db):
    """Two numbers filling in the same instant may each see the merchant
    under its cap: the overshoot is at most one fill's worth."""
    tpl = str(uuid.uuid4())
    for _ in range(20):
        await db.execute(
            "TRUNCATE dial_queue; UPDATE telephony_numbers SET channels = 0"
        )
        await _leads(db, tpl, 10)
        a, b = await asyncio.gather(
            dq.fill_number("N1", [tpl], 5, pick_leads, {"m1": 3}),
            dq.fill_number("N2", [tpl], 5, pick_leads, {"m1": 3}),
        )
        assert 3 <= len(a) + len(b) <= 6


@pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN")
async def test_the_caps_read_runs_on_the_real_columns(db, monkeypatch):
    """The real query against merchants as migrations 022 (merchant_id) and
    087 (max_concurrent_calls) leave it: a wrong column name fails here."""
    async with db.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS merchants; CREATE TABLE merchants (merchant_id text);"
        )
        await conn.execute(CAPS_MIGRATION.read_text())
        await conn.execute("INSERT INTO merchants VALUES ('m1', 3), ('m2', NULL)")

    async def run(query_text: str, values: Any, timeout: Any = None) -> Any:
        async with db.acquire() as conn:
            return await conn.fetch(query_text, *values)

    monkeypatch.setattr(merchants_acc, "run_parameterized_query", run)
    try:
        assert await merchants_acc.get_merchant_call_caps() == {"m1": 3}
    finally:
        async with db.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS merchants;")
