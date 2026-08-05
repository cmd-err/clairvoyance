# pyrefly: ignore-errors
"""Pending-state resolution for browser-executed client tools.

There is no pending-row table for client tools. The unanswered ``tool_use``
block in the persisted history IS the pending state, which is what lets the
whole feature ship without a Redis bus, a timeout sweeper, or a new table.

That is a strong claim, so these tests pin the two functions it rests on:

- [[chat.turn_core.find_pending_client_tool]] — existence check AND
  idempotency guard in one lookup.
- [[chat.block_codec.repair_dangling_tool_uses]] — the abandonment path.
  If the browser never answers, the next turn must still work. This is the
  test that justifies not writing a timeout subsystem.

Both are pure functions over already-loaded rows, so no DB is involved.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    blocks_to_llm_context_messages,
    repair_dangling_tool_uses,
)
from app.ai.voice.agents.breeze_buddy.chat.turn_core import find_pending_client_tool

TOOL_ID = "toolu_9F"


def _row(role: str, blocks: Optional[List[Dict[str, Any]]]) -> SimpleNamespace:
    """A chat_message row as the accessor returns it."""
    return SimpleNamespace(
        role=SimpleNamespace(value=role), content=None, content_blocks=blocks
    )


def _tool_use(tool_id: str = TOOL_ID, name: str = "perform_page_task"):
    return _row(
        "assistant",
        [{"type": "tool_use", "id": tool_id, "name": name, "input": {"goal": "x"}}],
    )


def _tool_result(tool_id: str = TOOL_ID, content: str = '{"ok":true}'):
    return _row(
        "user", [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]
    )


class TestFindPendingClientTool:
    def test_unanswered_tool_use_returns_its_name(self):
        rows = [_row("user", [{"type": "text", "text": "hi"}]), _tool_use()]
        assert find_pending_client_tool(rows, TOOL_ID) == "perform_page_task"

    def test_already_answered_returns_none(self):
        """The idempotency guard: a double POST must not write twice."""
        rows = [_tool_use(), _tool_result()]
        assert find_pending_client_tool(rows, TOOL_ID) is None

    def test_unknown_id_returns_none(self):
        rows = [_tool_use()]
        assert find_pending_client_tool(rows, "toolu_NOPE") is None

    def test_empty_history_returns_none(self):
        assert find_pending_client_tool([], TOOL_ID) is None

    def test_rows_with_no_blocks_are_skipped(self):
        rows = [_row("user", None), _tool_use()]
        assert find_pending_client_tool(rows, TOOL_ID) == "perform_page_task"

    def test_other_tool_calls_do_not_confuse_the_lookup(self):
        rows = [
            _tool_use("toolu_AAA", "search_products"),
            _tool_result("toolu_AAA"),
            _tool_use(TOOL_ID, "perform_page_task"),
        ]
        assert find_pending_client_tool(rows, TOOL_ID) == "perform_page_task"

    def test_read_page_name_is_returned_verbatim(self):
        rows = [_tool_use(TOOL_ID, "read_page")]
        assert find_pending_client_tool(rows, TOOL_ID) == "read_page"

    def test_non_dict_blocks_are_ignored(self):
        """Defensive: legacy or corrupt rows must not raise."""
        rows = [_row("assistant", ["not-a-dict", None]), _tool_use()]
        assert find_pending_client_tool(rows, TOOL_ID) == "perform_page_task"


class TestAbandonedCallIsHealed:
    """The browser never answers — the session must NOT be wedged.

    This is the behaviour that replaces a timeout subsystem. If these fail,
    an unanswered client tool would leave history permanently unreplayable
    (a ``tool_use`` with no ``tool_result`` is rejected by providers), and
    the session would be bricked until it aged out.
    """

    def _history(self, rows):
        return blocks_to_llm_context_messages(
            [
                {
                    "role": r.role.value,
                    "content": r.content,
                    "content_blocks": r.content_blocks,
                }
                for r in rows
            ]
        )

    def test_dangling_client_tool_use_gets_a_synthetic_result(self):
        rows = [_row("user", [{"type": "text", "text": "register it"}]), _tool_use()]
        repaired = repair_dangling_tool_uses(self._history(rows))

        answered = {
            m.get("tool_call_id")
            for m in repaired
            if isinstance(m, dict) and m.get("role") == "tool"
        }
        assert TOOL_ID in answered, (
            "an abandoned client tool must be healed into a synthetic result — "
            "otherwise the session cannot replay and is permanently stuck"
        )

    def test_healed_history_has_no_unanswered_tool_use(self):
        rows = [_tool_use()]
        repaired = repair_dangling_tool_uses(self._history(rows))

        declared: set = set()
        answered: set = set()
        for m in repaired:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" and m.get("tool_call_id"):
                answered.add(m["tool_call_id"])
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(tc["id"])
        assert declared - answered == set(), "every tool_use must end up answered"

    def test_answered_call_is_left_alone(self):
        """Repair must not add a duplicate result for an answered call."""
        rows = [_tool_use(), _tool_result()]
        repaired = repair_dangling_tool_uses(self._history(rows))

        results = [
            m
            for m in repaired
            if isinstance(m, dict)
            and m.get("role") == "tool"
            and m.get("tool_call_id") == TOOL_ID
        ]
        assert len(results) == 1

    def test_late_answer_after_healing_is_rejected_by_the_lookup(self):
        """The full abandonment race, end to end.

        Browser goes silent → user sends another message → that turn heals
        the dangling call → browser finally POSTs its outcome. The lookup
        must refuse it (the endpoint turns this into a 409), because the
        conversation already moved on with a synthetic answer.
        """
        rows = [_tool_use(), _tool_result(TOOL_ID, '{"status":"error"}')]
        assert find_pending_client_tool(rows, TOOL_ID) is None
