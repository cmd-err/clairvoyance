"""Shared fixtures for tests/breeze_buddy."""

import pytest


@pytest.fixture(autouse=True)
def _no_dial_queue_write(monkeypatch: pytest.MonkeyPatch):
    """schedule_lead, cancel and the finishing write keep dial_queue in step
    — real database work a unit test must never reach. No-ops here; a test
    that watches them patches over these (monkeypatch runs in order)."""
    from app.ai.voice.agents.breeze_buddy.dispatch import queue
    from app.database.accessor.breeze_buddy import lead_call_tracker as lct

    async def _ok(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(queue, "enqueue_dial", _ok)
    monkeypatch.setattr(queue, "delete_waiting_dial", _ok)
    monkeypatch.setattr(lct, "delete_waiting_dial", _ok)
