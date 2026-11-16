"""dial_queue accessors.

Fail-open on purpose: until the dialler reads this table (behind a per-number
switch), a dial_queue problem must never break lead intake, cancel or call
end. Each accessor logs and returns a falsy value instead of raising.
"""

from datetime import datetime

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.dial_queue import (
    delete_waiting_dial_query,
    enqueue_dial_query,
    feed_dial_queue_query,
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
