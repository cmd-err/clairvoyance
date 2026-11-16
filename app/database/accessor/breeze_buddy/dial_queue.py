"""dial_queue accessors.

The writes on the lead path (enqueue, cancel, call end, give-back) are
fail-open: a dial_queue problem must never break lead intake, cancel or call
end, so they log and return a falsy value. The tick's reads and fill raise:
the tick logs and skips that one round.
"""

import asyncio
from datetime import datetime
from typing import Any, Callable, Dict, List, Tuple

from app import database
from app.core.logger import logger
from app.database import db_connection
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.dial_queue import (
    FILL_STALL_GUARD,
    claim_dialing_query,
    delete_waiting_dial_query,
    drop_ended_live_rows_query,
    due_templates_query,
    enqueue_dial_query,
    feed_dial_queue_query,
    fill_candidates_query,
    free_numbers_query,
    give_back_dial_query,
    grant_query,
    in_flight_by_number_query,
    live_by_merchant_query,
    lock_lead_query,
    lock_number_query,
    mark_dial_live_query,
    route_numbers_query,
    route_templates_query,
    stuck_dials_query,
    take_lines_query,
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


# --- reads and fill for the per-number tick (these raise: the tick logs and
# --- skips one round; nothing on the call path depends on them) -------------


async def due_templates() -> List[Any]:
    """(template_id, earliest due_at) for every template with waiting leads."""
    query_text, values = due_templates_query()
    return list(await run_parameterized_query(query_text, values))


async def free_numbers(number_ids: List[str]) -> Dict[str, int]:
    """Free lines per AVAILABLE number that has room."""
    query_text, values = free_numbers_query(number_ids)
    rows = await run_parameterized_query(query_text, values)
    return {r["id"]: int(r["free"]) for r in rows}


async def route_numbers() -> List[Any]:
    query_text, values = route_numbers_query()
    return list(await run_parameterized_query(query_text, values))


async def route_templates() -> List[Any]:
    query_text, values = route_templates_query()
    return list(await run_parameterized_query(query_text, values))


async def in_flight_by_number(number_ids: List[str]) -> Dict[str, int]:
    query_text, values = in_flight_by_number_query(number_ids)
    rows = await run_parameterized_query(query_text, values)
    return {r["number_id"]: int(r["n"]) for r in rows}


async def fill_number(
    number_id: str,
    template_ids: List[str],
    k: int,
    pick: Callable[[int, Dict[str, int], List[Any]], List[str]],
) -> List[Tuple[str, str]]:
    """
    Give up to k free lines of one number to due leads of the templates routed
    to it, in ONE transaction holding the number's row (no network call
    inside). ``pick(k, live, candidates)`` chooses the leads (fairness).
    Returns (lead_id, ticket) for each line actually granted; channels go up
    by exactly that many. Another pod filling the same number: nothing.
    """
    async with db_connection() as conn:
        async with conn.transaction():
            await conn.execute(FILL_STALL_GUARD)
            sql, values = lock_number_query(number_id)
            row = await conn.fetchrow(sql, *values)
            if row is None:
                return []
            k = min(k, int(row["free"]))
            if k <= 0:
                return []
            sql, values = live_by_merchant_query(number_id)
            live = {
                r["merchant_id"]: int(r["live"]) for r in await conn.fetch(sql, *values)
            }
            sql, values = fill_candidates_query(template_ids, k)
            candidates = list(await conn.fetch(sql, *values))
            chosen = pick(k, live, candidates)
            if not chosen:
                return []
            sql, values = grant_query(number_id, chosen)
            granted = [
                (r["lead_id"], r["ticket"]) for r in await conn.fetch(sql, *values)
            ]
            if granted:
                sql, values = take_lines_query(number_id, len(granted))
                await conn.execute(sql, *values)
            return granted
