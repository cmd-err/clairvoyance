"""
Inbound channel accounting — the single place that takes the channel an
inbound call holds. It goes back in the finishing claim's own statement
(update_lead_call_completion_details_query: inbound on Plivo/Vobiz).

Why this is its own module rather than part of ``managers.calls``: that module
imports from the ``dispatch`` package, and importing any ``dispatch`` submodule
executes ``dispatch/__init__.py``, which imports ``dispatch.worker``, which
imports ``managers.calls`` straight back. Anything that wants to import
``managers.calls`` is therefore hostage to whichever module the process happens
to load first. This file imports only the data layer and schemas, so every
caller — the call-end handlers here in ``managers`` and the IVR
deferred-policy block in ``ivr/selection.py`` — can import it normally at
module scope, with no cycle and no deferred import inside a function body.

The gate here, the finishing claim's release and the reconciler's in-flight
count (``count_processing_by_telephony_number_query``) must name the same
providers: any disagreement silently miscounts free capacity.
"""

from app.database.accessor import (
    increment_telephony_number_channels,
)


async def admit_inbound_call(telephony_number_id: str) -> bool:
    """Take one channel for an inbound call on a gated number (Plivo, Vobiz).

    Same gate outbound uses in ``_acquire_number``: the atomic
    ``channels = channels + 1 WHERE channels < maximum_channels`` update, which
    returns no row when the number is already at its ceiling. Both directions
    therefore share one counter, so an inbound call genuinely reduces what
    outbound can dial and vice versa.

    Returns False when at capacity — and also when the UPDATE itself failed,
    because the accessor collapses both into None. That makes inbound
    admission fail *closed* on a DB outage, matching outbound (where the
    worker simply defers the lead). The visible difference is that an inbound
    caller hears the busy message instead of waiting invisibly in a queue.

    Not idempotent per call, and deliberately so. A provider that retries a
    slow answer webhook is mostly absorbed upstream: ``resolve_call_templates``
    looks the call up by ``call_id`` first, finds the lead the earlier attempt
    created, and routes the retry down the outbound branch, which never reaches
    this gate. The residual window is a retry that lands before that first
    insert commits -- both attempts see no lead, both increment, and one
    channel is held by a lead nobody will ever close until
    ``reconcile_stuck_processing_leads`` sweeps it. Closing that properly needs
    a uniqueness guarantee on inbound ``call_id`` (the column has a plain,
    non-unique index today), which is a bigger change than this gate.
    """
    return await increment_telephony_number_channels(telephony_number_id) is not None
