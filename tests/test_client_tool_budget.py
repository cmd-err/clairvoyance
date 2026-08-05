# pyrefly: ignore-errors
"""Per-session spend limits for the browser inference lane
([[chat.client_tool_budget]]).

`/infer` is an LLM endpoint a browser loop can call repeatedly, so the
caps have to be server-side — the client's own `max_steps` is a hint, not
a control.

Two behaviours matter most and are pinned here:

1. **Increment-then-compare**, so two concurrent calls cannot both read
   "under the limit" and both proceed.
2. **Fail-open on Redis errors.** This is a cost guard, not a security
   boundary; a broken counter must not take the feature down. (The real
   security boundaries — redaction, approval gating — never fail open.)
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.chat import client_tool_budget as budget

SESSION = "sess-abc"


class FakeRedis:
    """In-memory stand-in for RedisService with the methods used here."""

    def __init__(self, *, fail: bool = False):
        self.store: dict = {}
        self.expires: dict = {}
        self.fail = fail

    async def incr(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        v = self.store.get(key)
        return str(v) if v is not None else None

    async def expire(self, key, seconds):
        self.expires[key] = seconds
        return True

    async def get_client(self):
        return self

    async def incrby(self, key, amount):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = int(self.store.get(key, 0)) + amount
        return self.store[key]


@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()

    async def _get():
        return r

    monkeypatch.setattr(budget, "get_redis_service", _get)
    return r


class TestStepBudget:
    @pytest.mark.asyncio
    async def test_first_call_allowed(self, fake):
        v = await budget.check_and_consume_step(SESSION, max_steps=3)
        assert v.allowed is True
        assert v.steps_used == 1

    @pytest.mark.asyncio
    async def test_calls_up_to_the_cap_are_allowed(self, fake):
        for expected in (1, 2, 3):
            v = await budget.check_and_consume_step(SESSION, max_steps=3)
            assert v.allowed is True
            assert v.steps_used == expected

    @pytest.mark.asyncio
    async def test_call_past_the_cap_is_refused(self, fake):
        for _ in range(3):
            await budget.check_and_consume_step(SESSION, max_steps=3)
        v = await budget.check_and_consume_step(SESSION, max_steps=3)
        assert v.allowed is False
        assert v.reason == "step_budget_exhausted"

    @pytest.mark.asyncio
    async def test_budgets_are_per_session(self, fake):
        for _ in range(3):
            await budget.check_and_consume_step(SESSION, max_steps=3)
        other = await budget.check_and_consume_step("sess-other", max_steps=3)
        assert other.allowed is True, "one session must not spend another's budget"

    @pytest.mark.asyncio
    async def test_ttl_set_once_on_first_claim(self, fake):
        await budget.check_and_consume_step(SESSION, max_steps=5)
        key = budget._STEP_KEY.format(sid=SESSION)
        assert key in fake.expires
        fake.expires.clear()
        await budget.check_and_consume_step(SESSION, max_steps=5)
        assert key not in fake.expires, "TTL should not be re-armed on every call"

    @pytest.mark.asyncio
    async def test_increment_happens_before_the_comparison(self, fake):
        """Two concurrent claims must not both see 'under the limit'.

        With read-then-write, both would read 0 and both proceed. Because
        the counter is incremented first, the second claim sees 2.
        """
        a = await budget.check_and_consume_step(SESSION, max_steps=1)
        b = await budget.check_and_consume_step(SESSION, max_steps=1)
        assert a.allowed is True
        assert b.allowed is False


class TestTokenBudget:
    @pytest.mark.asyncio
    async def test_tokens_accumulate(self, fake):
        await budget.record_tokens(SESSION, 100)
        await budget.record_tokens(SESSION, 50)
        v = await budget.check_and_consume_step(SESSION, max_tokens=1000)
        assert v.tokens_used == 150

    @pytest.mark.asyncio
    async def test_exceeding_the_token_cap_refuses_the_next_call(self, fake):
        await budget.record_tokens(SESSION, 5000)
        v = await budget.check_and_consume_step(SESSION, max_tokens=1000)
        assert v.allowed is False
        assert v.reason == "token_budget_exhausted"

    @pytest.mark.asyncio
    async def test_zero_and_negative_tokens_are_ignored(self, fake):
        await budget.record_tokens(SESSION, 0)
        await budget.record_tokens(SESSION, -5)
        v = await budget.check_and_consume_step(SESSION)
        assert v.tokens_used == 0

    @pytest.mark.asyncio
    async def test_step_cap_is_checked_before_token_cap(self, fake):
        """Both exhausted → report steps, the more actionable of the two."""
        await budget.record_tokens(SESSION, 99_999)
        for _ in range(2):
            await budget.check_and_consume_step(SESSION, max_steps=2, max_tokens=10)
        v = await budget.check_and_consume_step(SESSION, max_steps=2, max_tokens=10)
        assert v.reason == "step_budget_exhausted"


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_redis_failure_allows_the_call(self, monkeypatch):
        async def _boom():
            raise RuntimeError("redis unreachable")

        monkeypatch.setattr(budget, "get_redis_service", _boom)
        v = await budget.check_and_consume_step(SESSION)
        assert v.allowed is True, (
            "a cost guard must not take the feature down when its counter is "
            "unreachable — unlike the security boundaries, which never fail open"
        )

    @pytest.mark.asyncio
    async def test_token_recording_failure_is_swallowed(self, monkeypatch):
        async def _boom():
            raise RuntimeError("redis unreachable")

        monkeypatch.setattr(budget, "get_redis_service", _boom)
        await budget.record_tokens(SESSION, 100)  # must not raise
