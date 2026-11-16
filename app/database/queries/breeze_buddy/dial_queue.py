"""SQL for dial_queue: the waiting / in-flight list the dialler fills from."""

from datetime import datetime
from typing import Any, List, Tuple

from app.schemas import CallDirection, ExecutionMode, LeadCallStatus

DIAL_QUEUE_TABLE = "dial_queue"
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"

# The execution modes the dialler places outbound calls for (mirrors
# dispatch/queue.py DISPATCHABLE_EXECUTION_MODES; the data layer cannot
# import app.ai).
DIALLABLE_MODES = [ExecutionMode.TELEPHONY.value, ExecutionMode.TELEPHONY_TEST.value]


def enqueue_dial_query(lead_id: str, due_at: datetime) -> Tuple[str, List[Any]]:
    """
    Give a dispatchable BACKLOG lead its waiting row, or move an existing
    waiting row EARLIER. Never moves a time later (the old path re-schedules
    on every defer; give-back writes later times) and never touches a row
    that holds a line.
    """
    text = f"""
        INSERT INTO "{DIAL_QUEUE_TABLE}" (lead_id, merchant_id, template_id, due_at)
        SELECT l.id, COALESCE(l.merchant_id, l.reseller_id, ''), l.template_id::uuid, $2
        FROM "{LEAD_CALL_TRACKER_TABLE}" l
        WHERE l.id = $1 AND l.status = $3 AND l.call_direction = $4
          AND l.execution_mode = ANY($5::text[]) AND l.template_id IS NOT NULL
        ON CONFLICT (lead_id) DO UPDATE SET due_at = EXCLUDED.due_at
          WHERE {DIAL_QUEUE_TABLE}.state = 'waiting'
            AND EXCLUDED.due_at < {DIAL_QUEUE_TABLE}.due_at;
    """
    values = [
        lead_id,
        due_at,
        LeadCallStatus.BACKLOG.value,
        CallDirection.OUTBOUND.value,
        DIALLABLE_MODES,
    ]
    return text, values


def delete_waiting_dial_query(lead_id: str) -> Tuple[str, List[Any]]:
    """Drop a lead's row while it is still waiting (cancel, finish)."""
    text = f"""
        DELETE FROM "{DIAL_QUEUE_TABLE}" WHERE lead_id = $1 AND state = 'waiting';
    """
    return text, [lead_id]


def feed_dial_queue_query(limit: int) -> Tuple[str, List[Any]]:
    """
    Safety net: give every unlocked, dispatchable BACKLOG lead due within 10
    minutes that has no row one (later leads get theirs when they come due).
    The anti-join runs BEFORE the LIMIT, so a hot merchant's queued backlog
    cannot hide a cold merchant's missing lead.
    """
    text = f"""
        INSERT INTO "{DIAL_QUEUE_TABLE}" (lead_id, merchant_id, template_id, due_at)
        SELECT l.id, COALESCE(l.merchant_id, l.reseller_id, ''), l.template_id::uuid,
               l.next_attempt_at
        FROM "{LEAD_CALL_TRACKER_TABLE}" l
        WHERE l.status = $2 AND l.is_locked = FALSE
          AND l.execution_mode = ANY($3::text[]) AND l.call_direction = $4
          AND l.template_id IS NOT NULL
          AND l.next_attempt_at <= now() + interval '10 minutes'
          AND NOT EXISTS (SELECT 1 FROM "{DIAL_QUEUE_TABLE}" q WHERE q.lead_id = l.id)
        ORDER BY l.next_attempt_at
        LIMIT $1
        ON CONFLICT (lead_id) DO NOTHING
        RETURNING lead_id;
    """
    values = [
        limit,
        LeadCallStatus.BACKLOG.value,
        DIALLABLE_MODES,
        CallDirection.OUTBOUND.value,
    ]
    return text, values
