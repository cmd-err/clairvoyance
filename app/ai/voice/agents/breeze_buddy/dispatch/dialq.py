"""
Per-number dialler (dial_queue): a free line pulls the next lead.

Every dialler pod runs one tick per second (no leader). A tick finds the
templates with due waiting leads (one row per template), maps each to the
number today's routing rule would dial it from, and, for each number on the
``BB_DIALQ_NUMBERS`` list with a free line, gives those lines out in one
short transaction (``fill_number``). Each granted lead is dialled by its own
task through ``Worker.dispatch_granted`` (today's checks, same order).

Fairness on a number: each line goes to the merchant with the fewest live
calls on it; ties go to the older lead; within a merchant, oldest first.
Numbers not on the list stay on the old path untouched.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set

import aiohttp

from app.ai.voice.agents.breeze_buddy.dispatch import worker as worker_mod
from app.ai.voice.agents.breeze_buddy.dispatch.alerts import (
    raise_dialq_drift,
    raise_dialq_no_route,
    raise_dialq_tick_failing,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    PROMOTER_PAUSED,
    reseller_paused_key,
)
from app.ai.voice.agents.breeze_buddy.managers.calls import _is_within_calling_hours
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import BB_WORKER_BLPOP_TIMEOUT_S, BB_WORKER_COUNT
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import get_all_telephony_numbers
from app.database.accessor.breeze_buddy.dial_queue import (
    due_templates,
    fill_number,
    free_numbers,
    in_flight_by_number,
    route_numbers,
    route_templates,
)
from app.database.accessor.breeze_buddy.dispatch import (
    count_processing_by_telephony_number,
)
from app.database.accessor.breeze_buddy.merchants import get_merchant_call_caps
from app.services.redis import get_redis_service

ROUTE_TTL_S = 10.0
DRIFT_KEY_TTL_S = 900

# --- pure rules -----------------------------------------------------------------


def pick_number(
    pinned_id: Optional[str],
    merchant_id: Optional[str],
    provider: Optional[str],
    numbers: List[Any],
) -> Optional[str]:
    """Same rule as managers.calls._get_available_number. ``numbers`` are
    newest first. A pin wins and never falls back; else the merchant's newest
    AVAILABLE number on the template's provider; else the newest AVAILABLE
    pool number (no reseller, no merchant) on that provider."""
    if pinned_id:
        pin = next((n for n in numbers if n["id"] == pinned_id), None)
        return pin["id"] if pin is not None and pin["status"] == "AVAILABLE" else None
    usable = [
        n for n in numbers if n["status"] == "AVAILABLE" and n["provider"] == provider
    ]
    owned = [
        n
        for n in usable
        if n["merchant_id"] is not None and n["merchant_id"] == merchant_id
    ]
    pool = [n for n in usable if n["reseller_id"] is None and n["merchant_id"] is None]
    chosen = owned or pool
    return chosen[0]["id"] if chosen else None


def pick_leads(
    k: int,
    live: Dict[str, int],
    candidates: List[Any],
    room: Optional[Dict[str, int]] = None,
) -> List[str]:
    """Hand out k lines one at a time: to the merchant with the fewest live
    calls on this number; ties to the merchant whose next lead is older;
    within a merchant oldest first. ``room[m]`` caps how many more calls a
    capped merchant may get (merchants not in it are uncapped)."""
    room = dict(room or {})
    queues: Dict[str, List[Any]] = {}
    for c in sorted(candidates, key=lambda c: c["due_at"]):
        if room.get(c["merchant_id"], 1) > 0:
            queues.setdefault(c["merchant_id"], []).append(c)
    held = dict(live)
    chosen: List[str] = []
    while len(chosen) < k and queues:
        merchant = min(queues, key=lambda m: (held.get(m, 0), queues[m][0]["due_at"]))
        chosen.append(queues[merchant].pop(0)["lead_id"])
        held[merchant] = held.get(merchant, 0) + 1
        if merchant in room:
            room[merchant] -= 1
        if not queues[merchant] or room.get(merchant, 1) <= 0:
            del queues[merchant]
    return chosen


# --- route cache (per pod) ---------------------------------------------------------

_routes: Dict[str, Dict[str, Any]] = {}  # template_id -> route + gates
_routes_at: List[float] = [-1e9]
# A Twilio line is a status flip (AVAILABLE/IN_USE), not a channel count, so a
# Twilio number is never served here even if listed.
_twilio: Set[str] = set()
_caps: Dict[str, Any] = {"at": -1e9, "caps": {}}  # merchant concurrent-call caps


async def _merchant_caps() -> Dict[str, int]:
    """Merchant caps, re-read at most every ROUTE_TTL_S. A failed read keeps
    the last good caps (soft cap: never stops dialling, never drops caps)."""
    now = time.monotonic()
    if now - _caps["at"] > ROUTE_TTL_S:
        _caps["at"] = now
        try:
            _caps["caps"] = await get_merchant_call_caps()
        except Exception as e:  # noqa: BLE001
            logger.error(f"merchant caps read failed, keeping the last: {e}")
    return _caps["caps"]


def _route(r: Any, numbers: List[Any]) -> Dict[str, Any]:
    number_id = pick_number(
        r["pinned_id"], r["merchant_id"], r["calling_provider"], numbers
    )
    return {
        "number_id": number_id,
        # The number the template belongs to even while it cannot dial
        # (a pinned number that is disabled): decides which path owns it.
        "home": number_id or r["pinned_id"],
        "reseller_id": r["reseller_id"],
        "enable_calling": r["enable_calling"],
        "hours": SimpleNamespace(
            call_start_time=r["call_start_time"], call_end_time=r["call_end_time"]
        ),
    }


async def _refresh_routes() -> None:
    """Re-read every template's route at most every ROUTE_TTL_S, in two narrow
    reads. Every template, not only those with waiting leads: which path owns
    a lead must not depend on whether this pod's tick has seen its row yet. A
    template with no call config gets no route (the old path keeps it)."""
    now = time.monotonic()
    if now - _routes_at[0] <= ROUTE_TTL_S:
        return
    numbers = await route_numbers()
    _twilio.clear()
    _twilio.update(n["id"] for n in numbers if n["provider"] == "TWILIO")
    fresh = {r["template_id"]: _route(r, numbers) for r in await route_templates()}
    _routes.clear()
    _routes.update(fresh)
    _routes_at[0] = now


# --- the tick ---------------------------------------------------------------------


TICK_FAILS_ALERT = 30  # consecutive failed ticks (~30 s) before paging


class Tick:
    def __init__(self) -> None:
        self._worker = worker_mod.Worker(worker_uuid="dialq")
        self._tasks: Set[asyncio.Task] = set()
        self._stopping = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        # The session outlives the loop: dials still running at stop() use it
        # for pre-checks and webhooks.
        self._session = create_aiohttp_session()
        # One round first, so the hand-off set exists before workers pop.
        try:
            await self.run_once(self._session)
        except Exception as e:  # noqa: BLE001
            logger.error(f"dialq first tick failed: {e}", exc_info=True)
        self._task = asyncio.create_task(self._loop(), name="dialq-tick")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._tasks:
            # Dials in flight finish; a task cut off still gives its line back.
            _, pending = await asyncio.wait(
                self._tasks, timeout=BB_WORKER_BLPOP_TIMEOUT_S + 5
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self._session:
            await self._session.close()

    async def _loop(self) -> None:
        fails = 0
        while not self._stopping.is_set():
            try:
                await self.run_once(self._session)
                fails = 0
            except Exception as e:  # noqa: BLE001
                fails += 1
                logger.error(
                    f"dialq tick failed ({fails} in a row): {e}", exc_info=True
                )
                if fails == TICK_FAILS_ALERT:
                    await raise_dialq_tick_failing(fails, str(e))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _paused(self, key: str) -> bool:
        try:
            redis = await get_redis_service()
            return bool(await redis.exists(key))
        except Exception:  # noqa: BLE001
            return False  # fail open, as the old path does

    async def run_once(self, session: Optional[aiohttp.ClientSession]) -> None:
        listed = set(await dyn_cfg.BB_DIALQ_NUMBERS())
        if not listed:
            worker_mod.DIALQ_TEMPLATES.clear()
            return
        # Ownership first, before any pause: the old worker must keep handing
        # these leads over while the dialler is paused.
        await _refresh_routes()
        listed -= _twilio
        worker_mod.DIALQ_TEMPLATES.clear()
        worker_mod.DIALQ_TEMPLATES.update(
            t for t, r in _routes.items() if r["home"] in listed
        )
        if await self._worker._dispatch_globally_disabled():
            return
        if await self._paused(PROMOTER_PAUSED):
            return

        rows = await due_templates()
        now = datetime.now(timezone.utc)
        paused: Dict[str, bool] = {}  # reseller -> paused, read once per tick
        by_number: Dict[str, List[str]] = {}
        for r in rows:
            if r["due_at"] > now:
                continue
            route = _routes.get(r["template_id"], {})
            if route.get("home") not in listed:
                continue  # the old path owns it (including "no number at all")
            number_id = route["number_id"]
            if number_id is None:
                # Pinned to a listed number that cannot dial now: leads WAIT
                # (the old path would finish them NUMBER_UNAVAILABLE).
                await raise_dialq_no_route(r["template_id"])
                continue
            if not route["enable_calling"]:
                continue
            if not _is_within_calling_hours(route["hours"]):
                continue
            reseller = route["reseller_id"]
            if reseller:
                if reseller not in paused:
                    paused[reseller] = await self._paused(reseller_paused_key(reseller))
                if paused[reseller]:
                    continue
            by_number.setdefault(number_id, []).append(r["template_id"])
        if not by_number:
            return

        free = await free_numbers(list(by_number))
        caps = await _merchant_caps()
        share = max(1, BB_WORKER_COUNT // 2)
        for number_id in random.sample(list(free), len(free)):
            slots = BB_WORKER_COUNT - len(self._tasks)
            if slots <= 0:
                break
            k = min(free[number_id], slots, share)
            granted = await fill_number(
                number_id, by_number[number_id], k, pick_leads, caps
            )
            for lead_id, ticket in granted:
                task = asyncio.create_task(
                    self._worker.dispatch_granted(lead_id, number_id, ticket, session),
                    name=f"dialq-dial:{lead_id}",
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)


_tick: Optional[Tick] = None


async def start_tick() -> None:
    """Start this pod's tick. Idempotent. Does nothing while the list is empty."""
    global _tick
    if _tick is None:
        _tick = Tick()
        await _tick.start()


async def stop_tick() -> None:
    global _tick
    if _tick is not None:
        await _tick.stop()
        _tick = None


# --- drift check (alert only) -------------------------------------------------------


async def monitor_dialq_drift() -> None:
    """For listed numbers, compare channels with the calls holding lines
    (PROCESSING leads, inbound included, plus rows granted/dialing). Alert when
    the same gap shows on two checks in a row; never changes the counter."""
    listed = await dyn_cfg.BB_DIALQ_NUMBERS()
    if not listed:
        return
    processing = await count_processing_by_telephony_number()
    in_flight = await in_flight_by_number(listed)
    redis = await get_redis_service()
    for number in await get_all_telephony_numbers():
        if number.id not in listed:
            continue
        channels = number.channels or 0
        expected = processing.get(number.id, 0) + in_flight.get(number.id, 0)
        gap = channels - expected
        key = f"bb:dialq:drift:{number.id}"
        previous = await redis.get(key)
        if gap and previous is not None and int(previous) == gap:
            await raise_dialq_drift(number.id, channels, expected)
        if gap:
            await redis.set(key, str(gap), ex=DRIFT_KEY_TTL_S)
        else:
            await redis.delete(key)
