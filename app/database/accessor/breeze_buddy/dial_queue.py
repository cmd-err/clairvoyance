"""dial_queue accessors.

Fail-open on purpose: until the dialler reads this table (behind a per-number
switch), a dial_queue problem must never break lead intake, cancel or call
end. Each accessor logs and returns a falsy value instead of raising.
"""

import asyncio
from datetime import datetime
from typing import Any, List

from app import database
from app.core.logger import logger
from app.database import db_connection
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.dial_queue import (
    claim_dialing_query,
    delete_waiting_dial_query,
    drop_ended_live_rows_query,
    enqueue_dial_query,
    feed_dial_queue_query,
    give_back_dial_query,
    lock_lead_query,
    mark_dial_live_query,
    stuck_dials_query,
)


async def enqueue_dial(lead_id: str, due_at: datetime) -> bool:
    """Give a dispatchable BACKLOG lead its waiting row (or move it earlier)."""
    try:
        query_text, values = enqueue_dial_query(lead_id, due_at)
        await run_parameterized_query(query_text, values)
        return True
    except Exception as e:
        logger.error(f"enqueue_dial failed for lead {lead_id}: {e}")
        return False


async def delete_waiting_dial(lead_id: str) -> bool:
    """Drop a lead's row while it is still waiting."""
    try:
        query_text, values = delete_waiting_dial_query(lead_id)
        await run_parameterized_query(query_text, values)
        return True
    except Exception as e:
        logger.error(f"delete_waiting_dial failed for lead {lead_id}: {e}")
        return False


async def feed_dial_queue(limit: int) -> int:
    """Insert rows for dispatchable BACKLOG leads that have none. Returns how
    many were added (0 on error)."""
    try:
        query_text, values = feed_dial_queue_query(limit)
        return len(await run_parameterized_query(query_text, values))
    except Exception as e:
        logger.error(f"feed_dial_queue failed: {e}")
        return 0


async def claim_dialing(lead_id: str, ticket: str) -> bool:
    """True while the task still owns its line (granted -> dialing). False
    on a lost ticket or an error: the task must not dial."""
    try:
        query_text, values = claim_dialing_query(lead_id, ticket)
        return bool(await run_parameterized_query(query_text, values))
    except Exception as e:
        logger.error(f"claim_dialing failed for lead {lead_id}: {e}")
        return False


async def mark_dial_live(lead_id: str, ticket: str) -> bool:
    """dialing -> live. A failure is healed by repair (dialing + PROCESSING)."""
    try:
        query_text, values = mark_dial_live_query(lead_id, ticket)
        return bool(await run_parameterized_query(query_text, values))
    except Exception as e:
        logger.error(f"mark_dial_live failed for lead {lead_id}: {e}")
        return False


async def give_back_dial(
    lead_id: str, ticket: str, may_have_rung: bool = False
) -> bool:
    """Return a granted/dialing line without a call. True if a line went back.
    A failure leaves the row in flight; repair returns it."""
    sql, values = give_back_dial_query(lead_id, ticket, may_have_rung)
    lock_sql, lock_values = lock_lead_query(lead_id)
    for attempt in range(3):  # a DB blip must not park the line for 10 minutes
        try:
            async with db_connection() as conn:
                async with conn.transaction():
                    await conn.execute(lock_sql, *lock_values)
                    rows = await conn.fetch(sql, *values)
            break
        except Exception as e:
            logger.error(f"give_back_dial attempt {attempt + 1} for {lead_id}: {e}")
            await asyncio.sleep(0.2 * (attempt + 1))
    else:
        return False  # still in flight: repair returns it
    try:
        if rows and rows[0]["cancelled"]:
            # Deferred: lead_call_tracker's accessor imports this module.
            from app.database.accessor.breeze_buddy.lead_call_tracker import (
                fire_finished_hooks,
            )

            await fire_finished_hooks(lead_id)
        return bool(rows)
    except Exception as e:
        logger.error(f"give_back_dial failed for lead {lead_id}: {e}")
        return False


async def drop_ended_live_rows() -> int:
    """Delete live rows whose lead is no longer in a call (see the query)."""
    try:
        query_text, values = drop_ended_live_rows_query()
        return len(await run_parameterized_query(query_text, values))
    except Exception as e:
        logger.error(f"drop_ended_live_rows failed: {e}")
        return 0


async def dial_queue_missing() -> bool:
    """True only when the DB answers and has no dial_queue table (migration
    086 not applied). No pool or an error: False, boot goes on as today."""
    if database.pool is None:
        return False  # not a second connect attempt at boot
    try:
        rows = await run_parameterized_query(
            "SELECT to_regclass('dial_queue') IS NULL AS missing", []
        )
        return bool(rows and rows[0]["missing"])
    except Exception as e:
        logger.warning(f"dial_queue check skipped: {e}")
        return False


async def stuck_dials() -> List[Any]:
    """In-flight rows whose pod died (see the query)."""
    try:
        query_text, values = stuck_dials_query()
        return list(await run_parameterized_query(query_text, values))
    except Exception as e:
        logger.error(f"stuck_dials failed: {e}")
        return []
