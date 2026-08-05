"""Per-session spend limits for the browser-driven inference lane.

``POST /widget/session/{id}/infer`` is an LLM endpoint a browser can call
in a loop. The subagent is *supposed* to loop — that is the whole design —
but the loop lives in JavaScript on a merchant's page, which is the least
trustworthy place in the system. A bug, a stuck retry, or a hostile page
could otherwise bill us for an unbounded number of completions.

So the caps live here, server-side, and the client's own ``max_steps`` is
treated as a hint rather than a control. Two independent counters:

- **steps** — how many inference calls this session may make in total.
- **tokens** — how many completion tokens those calls may consume, which
  catches "few calls, enormous prompts" that a step cap alone misses.

Both are Redis counters keyed per chat session with a TTL, so an abandoned
session's budget disappears on its own rather than needing a sweeper.

Fail-open on Redis errors, deliberately: this is a cost guard, not a
security boundary, and taking the feature down because a counter was
unreachable trades a small bill for a broken product. The security
boundaries — redaction screening, approval gating, origin allowlists —
never fail open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.logger import logger
from app.services.redis.client import get_redis_service

# Budgets are per chat session, not per turn: a page task legitimately
# spans several turns (read, act, verify), and a per-turn cap would either
# be too tight for real work or too loose to matter.
_STEP_KEY = "chat:session:{sid}:ct:steps"
_TOKEN_KEY = "chat:session:{sid}:ct:tokens"

# Generous enough that no honest session hits them, tight enough that a
# runaway loop is bounded. A single page task runs ~3-8 inference calls.
DEFAULT_MAX_STEPS = 120
DEFAULT_MAX_TOKENS = 400_000

# Outlives any realistic session; the chat idle sweeper ends sessions long
# before this. Present so abandoned counters cannot accumulate forever.
_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class BudgetVerdict:
    """Outcome of a budget check. ``allowed`` is the only field callers act on."""

    allowed: bool
    reason: Optional[str] = None
    steps_used: int = 0
    tokens_used: int = 0


async def check_and_consume_step(
    session_id: str,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> BudgetVerdict:
    """Claim one inference step for ``session_id``.

    Increments FIRST and compares after, so concurrent calls cannot both
    read "under the limit" and both proceed — the race a read-then-write
    check would leave open. Overshoot is bounded by concurrency, which is
    the right trade for a cost guard.
    """
    try:
        redis = await get_redis_service()
        step_key = _STEP_KEY.format(sid=session_id)
        token_key = _TOKEN_KEY.format(sid=session_id)

        steps = await redis.incr(step_key)
        if steps == 1:
            # First claim of the session — start the clock.
            await redis.expire(step_key, _TTL_SECONDS)

        raw_tokens = await redis.get(token_key)
        tokens = int(raw_tokens) if raw_tokens and raw_tokens.isdigit() else 0

        if steps > max_steps:
            return BudgetVerdict(
                allowed=False,
                reason="step_budget_exhausted",
                steps_used=steps,
                tokens_used=tokens,
            )
        if tokens > max_tokens:
            return BudgetVerdict(
                allowed=False,
                reason="token_budget_exhausted",
                steps_used=steps,
                tokens_used=tokens,
            )
        return BudgetVerdict(allowed=True, steps_used=steps, tokens_used=tokens)
    except Exception as exc:  # noqa: BLE001 — cost guard, not a security gate
        logger.warning(
            f"[client_tools] budget check failed for session={session_id}: "
            f"{exc} — allowing the call (fail-open)"
        )
        return BudgetVerdict(allowed=True)


async def record_tokens(session_id: str, tokens: int) -> None:
    """Add ``tokens`` to the session's consumption.

    Called AFTER a completion, so the token cap is enforced on the NEXT
    call rather than this one. That is intentional: we cannot know the
    cost before the call, and refusing retroactively would mean charging
    for work we then discard.
    """
    if tokens <= 0:
        return
    try:
        redis = await get_redis_service()
        key = _TOKEN_KEY.format(sid=session_id)
        client = await redis.get_client()
        total = await client.incrby(key, tokens)  # type: ignore[union-attr]
        if total == tokens:
            await redis.expire(key, _TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[client_tools] token accounting failed for session={session_id}: {exc}"
        )


__all__ = [
    "BudgetVerdict",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_TOKENS",
    "check_and_consume_step",
    "record_tokens",
]
