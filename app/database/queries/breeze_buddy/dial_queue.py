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


def claim_dialing_query(lead_id: str, ticket: str) -> Tuple[str, List[Any]]:
    """The task may dial: granted -> dialing, only with its ticket, only if no
    cancel arrived and the lead is still BACKLOG (a webhook may have attached
    a call to it since the task read it)."""
    text = f"""
        UPDATE "{DIAL_QUEUE_TABLE}" q SET state = 'dialing', state_at = now()
        WHERE q.lead_id = $1 AND q.ticket = $2::uuid AND q.state = 'granted'
          AND q.cancel_reason IS NULL
          AND EXISTS (SELECT 1 FROM "{LEAD_CALL_TRACKER_TABLE}" l
                       WHERE l.id = q.lead_id AND l.status = $3)
        RETURNING q.lead_id;
    """
    return text, [lead_id, ticket, LeadCallStatus.BACKLOG.value]


def mark_dial_live_query(lead_id: str, ticket: str) -> Tuple[str, List[Any]]:
    """The call is placed and recorded: dialing -> live."""
    text = f"""
        UPDATE "{DIAL_QUEUE_TABLE}" SET state = 'live', state_at = now()
        WHERE lead_id = $1 AND ticket = $2::uuid AND state = 'dialing'
        RETURNING lead_id;
    """
    return text, [lead_id, ticket]


def lock_lead_query(lead_id: str) -> Tuple[str, List[Any]]:
    """Give-back's first step: the lead before its row, the order abort, adopt
    and the finishing write use, so none of them can deadlock with it."""
    return f'SELECT 1 FROM "{LEAD_CALL_TRACKER_TABLE}" WHERE id = $1 FOR UPDATE;', [
        lead_id
    ]


def lock_dial_row_query(lead_id: str) -> Tuple[str, List[Any]]:
    """Abort's second step, after the lead: the row too, so the abort sees a
    grant committed while it waited (fill takes rows SKIP LOCKED, so it never
    waits for this lock)."""
    return f'SELECT 1 FROM "{DIAL_QUEUE_TABLE}" WHERE lead_id = $1 FOR UPDATE;', [
        lead_id
    ]


def give_back_dial_query(
    lead_id: str, ticket: str, may_have_rung: bool = False
) -> Tuple[str, List[Any]]:
    """
    Return a granted or dialing line without a call — the ONLY way one is
    returned before the call ends. Lead still BACKLOG: the row goes back to
    waiting, not before the lead's next_attempt_at nor 30 s from now. A
    dialing row whose lead is PROCESSING is this row's call (our PROCESSING
    write or the webhook's adopt; only our task dials after claim_dialing):
    the row becomes live and keeps its line for the call's end to return.
    A cancel recorded on the row (cancel_reason) and a lead still BACKLOG:
    the lead is finished as ABORT instead of re-queued (one final event; the
    note says when the dial may have rung). Anything else: the row is
    dropped. The number's channels go down by one in the same statement. A
    second call, a stale ticket or a live row match nothing.
    Locks: the accessor locks the lead first (lock_lead_query), then this
    takes the row, then the number: lead -> row -> number, as abort, adopt
    and the finishing write. fill takes the number (SKIP LOCKED) and then
    only waiting rows, which no give-back holds, so nothing waits in a cycle.
    """
    text = f"""
        WITH mine AS (
            SELECT q.lead_id, q.number_id, q.state, q.cancel_reason
              FROM "{DIAL_QUEUE_TABLE}" q
             WHERE q.lead_id = $1 AND q.ticket = $2::uuid
               AND q.state IN ('granted', 'dialing')
               FOR UPDATE
        ), lead AS (
            SELECT l.status, l.next_attempt_at FROM "{LEAD_CALL_TRACKER_TABLE}" l, mine
             WHERE l.id = mine.lead_id
        ), live AS (
            UPDATE "{DIAL_QUEUE_TABLE}" q SET state = 'live', state_at = now()
              FROM mine, lead
             WHERE q.lead_id = mine.lead_id AND mine.state = 'dialing'
               AND lead.status = $4
            RETURNING q.lead_id
        ), requeued AS (
            UPDATE "{DIAL_QUEUE_TABLE}" q
               SET state = 'waiting', number_id = NULL, ticket = NULL, state_at = now(),
                   due_at = GREATEST(COALESCE(lead.next_attempt_at, now()),
                                     now() + interval '30 seconds')
              FROM mine, lead WHERE q.lead_id = mine.lead_id AND lead.status = $3
               AND mine.cancel_reason IS NULL
        ), dropped AS (
            DELETE FROM "{DIAL_QUEUE_TABLE}" q USING mine
             WHERE q.lead_id = mine.lead_id
               AND NOT (mine.cancel_reason IS NULL
                        AND EXISTS (SELECT 1 FROM lead WHERE lead.status = $3))
               AND NOT EXISTS (SELECT 1 FROM live)
        ), cancelled AS (
            UPDATE "{LEAD_CALL_TRACKER_TABLE}" l
               SET status = $6, outcome = 'ABORT', updated_at = now(),
                   meta_data = COALESCE(l.meta_data, '{{}}')::jsonb || jsonb_build_object(
                       'aborted_at', now(),
                       'outcome', jsonb_strip_nulls(jsonb_build_object(
                           'abort_reason', mine.cancel_reason, 'abort_note', $5::text)))
              FROM mine
             WHERE l.id = mine.lead_id AND l.status = $3
               AND mine.cancel_reason IS NOT NULL
            RETURNING l.id
        )
        UPDATE telephony_numbers t
           SET channels = GREATEST(0, COALESCE(t.channels, 0) - 1), updated_at = now()
          FROM mine WHERE t.id = mine.number_id AND NOT EXISTS (SELECT 1 FROM live)
        RETURNING t.id, EXISTS (SELECT 1 FROM cancelled) AS cancelled;
    """
    return text, [
        lead_id,
        ticket,
        LeadCallStatus.BACKLOG.value,
        LeadCallStatus.PROCESSING.value,
        "cancel_after_unknown_dial" if may_have_rung else None,
        LeadCallStatus.FINISHED.value,
    ]


def drop_ended_live_rows_query() -> Tuple[str, List[Any]]:
    """live rows whose lead is no longer in a call: it was finished by a
    writer that skipped the finishing statement (a pod on an older image, a
    manual fix), which released the line its own way. Bounded by the lines in
    use. Never touches channels."""
    text = f"""
        DELETE FROM "{DIAL_QUEUE_TABLE}" q
         WHERE q.state = 'live'
           AND NOT EXISTS (SELECT 1 FROM "{LEAD_CALL_TRACKER_TABLE}" l
                            WHERE l.id = q.lead_id AND l.status = $1)
        RETURNING q.lead_id;
    """
    return text, [LeadCallStatus.PROCESSING.value]


def stuck_dials_query() -> Tuple[str, List[Any]]:
    """In-flight rows past any task's lifetime: their pod died. Reads only
    in-flight rows (at most the lines in use)."""
    text = f"""
        SELECT q.lead_id, q.ticket::text AS ticket, q.state, l.status AS lead_status
        FROM "{DIAL_QUEUE_TABLE}" q
        LEFT JOIN "{LEAD_CALL_TRACKER_TABLE}" l ON l.id = q.lead_id
        WHERE (q.state = 'granted' AND q.state_at < now() - interval '10 minutes')
           OR (q.state = 'dialing' AND q.state_at < now() - interval '5 minutes')
        LIMIT 100;
    """
    return text, []
