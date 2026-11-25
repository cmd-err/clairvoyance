"""The per-number tick: routing equivalence, fairness, gates, hand-off."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast

import pytest

# dispatch must import first (managers.calls <-> dispatch.worker cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.dispatch import dialq, worker as worker_mod
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.schemas.breeze_buddy.core import (
    CallExecutionConfig,
    CallProvider,
    TelephonyNumber,
    TelephonyNumberStatus,
)
from tests.breeze_buddy.dispatch.conftest import (  # noqa: F401  (fixtures)
    fake_redis,
    harness,
    make_lead,
)

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


# --- pick_number == _get_available_number ----------------------------------------


def _num(
    i: str, status="AVAILABLE", provider="PLIVO", merchant=None, reseller=None, age=0
):
    return {
        "id": i,
        "status": status,
        "provider": provider,
        "merchant_id": merchant,
        "reseller_id": reseller,
        "created_at": T0 - timedelta(days=age),
    }


NUMBERS = [  # newest first, as route_numbers returns them
    _num("own-new", merchant="m1", age=1),
    _num("own-old", merchant="m1", age=5),
    _num("pool-new", age=2),
    _num("pool-old", age=9),
    _num("exo-pool", provider="EXOTEL", age=0),
    _num("off-pin", status="DISABLED", merchant="m9"),
    _num("other", merchant="m2", age=3),
]


@pytest.mark.parametrize(
    "pinned,merchant,provider",
    [
        ("other", "m1", "PLIVO"),  # pin wins, even cross-merchant
        ("off-pin", "m1", "PLIVO"),  # pinned but disabled -> nothing, no fallback
        ("missing", "m1", "PLIVO"),  # pin to a row that does not exist
        (None, "m1", "PLIVO"),  # own number, newest
        (None, "m3", "PLIVO"),  # no own number -> newest pool
        (None, "m3", "EXOTEL"),  # provider decides the pool
        (None, "m3", "VOBIZ"),  # nothing on that provider
    ],
)
async def test_pick_number_matches_get_available_number(
    monkeypatch, pinned, merchant, provider
):
    def model(n: Dict[str, Any]) -> TelephonyNumber:
        return TelephonyNumber(
            id=n["id"],
            number="+1",
            provider=CallProvider(n["provider"]),
            status=TelephonyNumberStatus(n["status"]),
            merchant_id=n["merchant_id"],
            reseller_id=n["reseller_id"],
        )

    async def by_id(number_id: str) -> Optional[TelephonyNumber]:
        found = next((n for n in NUMBERS if n["id"] == number_id), None)
        return model(found) if found else None

    async def by_status_provider(status: Any, prov: Any) -> List[TelephonyNumber]:
        return [
            model(n)
            for n in NUMBERS
            if n["status"] == status.value and n["provider"] == prov.value
        ]

    monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", by_id)
    monkeypatch.setattr(
        calls_mod,
        "get_telephony_number_based_on_status_and_provider",
        by_status_provider,
    )
    config = CallExecutionConfig(
        id="c",
        initial_offset=0,
        retry_offset=0,
        call_start_time=time(9),
        call_end_time=time(21),
        max_retry=1,
        calling_provider=CallProvider(provider),
        reseller_id="r1",
        template="t",
        merchant_id=merchant,
    )
    template = SimpleNamespace(telephony_number_id=pinned)

    today = await calls_mod._get_available_number(config, cast(Any, template))

    assert dialq.pick_number(pinned, merchant, provider, NUMBERS) == (
        today.id if today else None
    )


# --- pick_leads (fairness) ----------------------------------------------------------


def _c(lead: str, merchant: str, minute: int) -> Dict[str, Any]:
    return {
        "lead_id": lead,
        "merchant_id": merchant,
        "due_at": T0 + timedelta(minutes=minute),
    }


def test_pick_leads_story_2_the_small_merchant_gets_the_line():
    """Hot holds 2 calls, Slow 0: the free line goes to Slow even though Hot's
    lead is older."""
    picked = dialq.pick_leads(1, {"hot": 2}, [_c("H4", "hot", 0), _c("S2", "slow", 15)])
    assert picked == ["S2"]


def test_pick_leads_tie_goes_to_the_older_lead_then_alternates():
    candidates = [
        _c("H1", "hot", 0),
        _c("H2", "hot", 1),
        _c("S1", "slow", 15),
        _c("S2", "slow", 16),
    ]
    assert dialq.pick_leads(3, {}, candidates) == ["H1", "S1", "H2"]


def test_pick_leads_oldest_first_within_a_merchant():
    candidates = [_c("b", "m", 5), _c("a", "m", 1), _c("c", "m", 9)]
    assert dialq.pick_leads(3, {}, candidates) == ["a", "b", "c"]


def test_pick_leads_never_exceeds_k_or_candidates():
    rng = random.Random(7)
    for _ in range(300):
        cands = [
            _c(f"L{i}", f"m{rng.randint(1, 4)}", rng.randint(0, 60))
            for i in range(rng.randint(0, 12))
        ]
        k = rng.randint(0, 15)
        live = {f"m{m}": rng.randint(0, 5) for m in range(1, 5)}
        picked = dialq.pick_leads(k, live, cands)
        assert len(picked) == min(k, len(cands))
        assert len(set(picked)) == len(picked)


# --- the tick -----------------------------------------------------------------------


class Env:
    """Fakes every read the tick makes; records fills and alerts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.listed = ["N1"]
        self.enabled = True
        self.paused: set = set()
        self.rows = [{"template_id": "T1", "due_at": T0}]
        self.route = {
            "template_id": "T1",
            "pinned_id": None,
            "merchant_id": "m1",
            "reseller_id": "r1",
            "calling_provider": "PLIVO",
            "enable_calling": True,
            "call_start_time": time(0),
            "call_end_time": time(23, 59),
        }
        self.numbers = [_num("N1", merchant="m1")]
        self.free = {"N1": 3}
        self.fills: List[Any] = []
        self.alerts: List[str] = []
        self.reads: List[str] = []
        dialq._routes.clear()
        dialq._routes_at[0] = -1e9
        dialq._caps.update({"at": -1e9, "caps": {}})
        worker_mod.DIALQ_TEMPLATES.clear()

        async def no_caps() -> dict:
            return {}

        monkeypatch.setattr(dialq, "get_merchant_call_caps", no_caps)

        async def listed() -> list:
            return self.listed

        async def enabled() -> bool:
            return self.enabled

        async def due() -> list:
            self.reads.append("due")
            return self.rows

        async def numbers() -> list:
            return self.numbers

        async def templates() -> list:
            return [self.route] if self.route else []

        async def free(ids: list) -> dict:
            return {n: f for n, f in self.free.items() if n in ids}

        async def fill(
            number_id: str, template_ids: list, k: int, pick: Any, caps: Any = None
        ) -> list:
            self.fills.append((number_id, template_ids, k))
            return []

        async def no_route(template_id: str) -> None:
            self.alerts.append(template_id)

        async def paused(_self: Any, key: str) -> bool:
            return key in self.paused

        monkeypatch.setattr(dialq.dyn_cfg, "BB_DIALQ_NUMBERS", listed)
        monkeypatch.setattr(dialq.dyn_cfg, "BB_DISPATCH_ENABLED", enabled)
        monkeypatch.setattr(dialq, "due_templates", due)
        monkeypatch.setattr(dialq, "route_numbers", numbers)
        monkeypatch.setattr(dialq, "route_templates", templates)
        monkeypatch.setattr(dialq, "free_numbers", free)
        monkeypatch.setattr(dialq, "fill_number", fill)
        monkeypatch.setattr(dialq, "raise_dialq_no_route", no_route)
        monkeypatch.setattr(dialq.Tick, "_paused", paused)
        monkeypatch.setattr(dialq, "_is_within_calling_hours", lambda _cfg: True)
        monkeypatch.setattr(
            dialq,
            "datetime",
            SimpleNamespace(now=lambda tz=None: T0 + timedelta(seconds=1)),
        )


async def test_tick_is_a_noop_when_the_list_is_empty(monkeypatch):
    env = Env(monkeypatch)
    env.listed = []
    worker_mod.DIALQ_TEMPLATES.add("stale")

    await dialq.Tick().run_once(None)

    assert env.reads == [] and env.fills == []
    assert worker_mod.DIALQ_TEMPLATES == set()


async def test_tick_fills_a_listed_number_and_claims_its_templates(monkeypatch):
    env = Env(monkeypatch)

    await dialq.Tick().run_once(None)

    assert env.fills == [("N1", ["T1"], 3)]
    assert worker_mod.DIALQ_TEMPLATES == {"T1"}


def _calling_disabled(env: Env) -> None:
    env.route["enable_calling"] = False


def _outside_hours(env: Env) -> None:
    env.route["call_start_time"] = time(0)
    env.route["call_end_time"] = time(0, 0, 1)


def _reseller_paused(env: Env) -> None:
    env.paused.add(dialq.reseller_paused_key("r1"))


def _not_listed(env: Env) -> None:
    env.listed = ["OTHER"]


def _kill_switch(env: Env) -> None:
    env.enabled = False


def _promoter_paused(env: Env) -> None:
    env.paused.add(dialq.PROMOTER_PAUSED)


def _not_due(env: Env) -> None:
    env.rows = [{"template_id": "T1", "due_at": T0 + timedelta(minutes=5)}]


@pytest.mark.parametrize(
    "gate",
    [
        _calling_disabled,
        _outside_hours,
        _reseller_paused,
        _not_listed,
        _kill_switch,
        _promoter_paused,
        _not_due,
    ],
)
async def test_tick_gates(monkeypatch, gate):
    env = Env(monkeypatch)
    gate(env)
    if gate is _outside_hours:
        monkeypatch.setattr(dialq, "_is_within_calling_hours", lambda _cfg: False)

    await dialq.Tick().run_once(None)

    assert env.fills == []


async def test_pinned_listed_number_disabled_waits_and_alerts(monkeypatch):
    """Its leads wait (no fill, nothing finished), the old worker keeps its
    hands off them, and ops is told."""
    env = Env(monkeypatch)
    env.route["pinned_id"] = "N1"
    env.numbers = [_num("N1", status="DISABLED")]

    await dialq.Tick().run_once(None)

    assert env.fills == []
    assert env.alerts == ["T1"]
    assert worker_mod.DIALQ_TEMPLATES == {"T1"}


async def test_no_route_on_the_old_path_is_left_to_the_old_path(monkeypatch):
    env = Env(monkeypatch)
    env.numbers = []  # unpinned, no number at all: never was a listed number's

    await dialq.Tick().run_once(None)

    assert env.fills == [] and env.alerts == []
    assert worker_mod.DIALQ_TEMPLATES == set()


async def test_slot_cap_and_per_number_share(monkeypatch):
    env = Env(monkeypatch)
    env.free = {"N1": 50}
    tick = dialq.Tick()

    await tick.run_once(None)
    assert env.fills[-1][2] == max(1, dialq.BB_WORKER_COUNT // 2)

    tick._tasks = {
        asyncio.ensure_future(asyncio.sleep(10)) for _ in range(dialq.BB_WORKER_COUNT)
    }
    await tick.run_once(None)
    assert len(env.fills) == 1, "a full pod fills nothing"
    for t in tick._tasks:
        t.cancel()


async def test_a_listed_template_is_handed_over_before_it_has_a_waiting_row(
    monkeypatch,
):
    """Ownership comes from routing, not from the waiting rows a tick saw."""
    env = Env(monkeypatch)
    env.rows = []

    await dialq.Tick().run_once(None)

    assert worker_mod.DIALQ_TEMPLATES == {"T1"}


async def test_a_listed_twilio_number_stays_on_the_old_path(monkeypatch):
    """A Twilio line is a status flip, not a count: never served here."""
    env = Env(monkeypatch)
    env.route = {**env.route, "calling_provider": "TWILIO"}
    env.numbers = [_num("N1", merchant="m1", provider="TWILIO")]

    await dialq.Tick().run_once(None)

    assert worker_mod.DIALQ_TEMPLATES == set() and env.fills == []


async def test_route_cache_refreshes_after_10s(monkeypatch):
    env = Env(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(dialq.time, "monotonic", lambda: clock[0])
    reads: List[int] = []
    original = dialq.route_templates

    async def counted() -> list:
        reads.append(1)
        return await original()

    monkeypatch.setattr(dialq, "route_templates", counted)
    tick = dialq.Tick()

    await tick.run_once(None)
    clock[0] += 5
    await tick.run_once(None)
    clock[0] += 6
    await tick.run_once(None)

    assert len(reads) == 2


async def test_old_worker_hands_off_listed_templates(harness, fake_redis, monkeypatch):
    lead = make_lead("lead-handoff")
    harness.add_lead(lead)
    worker_mod.DIALQ_TEMPLATES.add(str(lead.template_id))
    locks: List[str] = []

    async def no_lock(*a: Any, **k: Any) -> None:
        locks.append("lock")
        return None

    monkeypatch.setattr(worker_mod, "acquire_lock_on_lead_by_id", no_lock)
    try:
        await worker_mod.Worker(worker_uuid="w-old")._dispatch(lead.id, None)
    finally:
        worker_mod.DIALQ_TEMPLATES.clear()

    assert locks == [], "returned before taking the lead lock"
    assert harness.call_recorder.calls == []


async def test_drift_alerts_only_on_the_second_same_mismatch(monkeypatch):
    store: Dict[str, str] = {}
    alerts: List[Any] = []

    class Redis:
        async def get(self, k: str) -> Optional[str]:
            return store.get(k)

        async def set(self, k: str, v: str, ex: Optional[int] = None) -> None:
            store[k] = v

        async def delete(self, k: str) -> None:
            store.pop(k, None)

    async def redis() -> Redis:
        return Redis()

    channels = [7]

    async def numbers() -> list:
        return [SimpleNamespace(id="N1", channels=channels[0])]

    async def listed() -> list:
        return ["N1"]

    async def processing() -> dict:
        return {"N1": 4}

    async def in_flight(_ids: list) -> dict:
        return {"N1": 1}

    async def alert(*a: Any) -> None:
        alerts.append(a)

    monkeypatch.setattr(dialq, "get_redis_service", redis)
    monkeypatch.setattr(dialq, "get_all_telephony_numbers", numbers)
    monkeypatch.setattr(dialq.dyn_cfg, "BB_DIALQ_NUMBERS", listed)
    monkeypatch.setattr(dialq, "count_processing_by_telephony_number", processing)
    monkeypatch.setattr(dialq, "in_flight_by_number", in_flight)
    monkeypatch.setattr(dialq, "raise_dialq_drift", alert)

    await dialq.monitor_dialq_drift()
    assert alerts == [], "first gap: watch only"
    await dialq.monitor_dialq_drift()
    assert alerts == [("N1", 7, 5)]
    channels[0] = 5
    await dialq.monitor_dialq_drift()
    assert "bb:dialq:drift:N1" not in store


async def test_stop_waits_then_cancels_a_stuck_dial(monkeypatch):
    monkeypatch.setattr(dialq, "BB_WORKER_BLPOP_TIMEOUT_S", -4.9)  # 0.1 s
    tick = dialq.Tick()
    stuck = asyncio.ensure_future(asyncio.Event().wait())
    tick._tasks = {stuck}

    await tick.stop()

    assert stuck.cancelled()
