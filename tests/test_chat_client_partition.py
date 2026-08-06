"""Chat client-tool partition (_partition_client_calls).

Client tools are executed by the *browser*, not the server, so they must be
split out of the batch before dispatch — the same way approval-gated calls
are. This mirrors ``_partition_gated_calls`` exactly, including node
shadowing: a per-node function with the same name as a client tool wins in
that node and stays server-side.

Ordering is load-bearing and tested here too: **gate first, then client**.
A client tool that is ALSO approval-gated must reach the approval path, not
be handed to the browser unconfirmed.

As in ``test_chat_gate_partition``, a node's ``functions`` at runtime are
``FlowsFunctionSchema`` objects, so the fixtures build real schemas.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.agent import (
    _partition_client_calls,
    _partition_gated_calls,
)


def _call(name: str) -> SimpleNamespace:
    return SimpleNamespace(function_name=name)


def _schema(name: str) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(name=name, description="", properties={}, required=[])


class TestBasicSplit:
    def test_splits_client_tools_from_server_tools(self):
        calls = [_call("perform_page_task"), _call("search_products")]
        gated, server = _partition_client_calls(
            calls, {"perform_page_task"}, {"functions": []}
        )
        assert [c.function_name for c in gated] == ["perform_page_task"]
        assert [c.function_name for c in server] == ["search_products"]

    def test_no_client_tools_configured_is_a_passthrough(self):
        calls = [_call("perform_page_task")]
        client, server = _partition_client_calls(calls, set(), {"functions": []})
        assert client == []
        assert [c.function_name for c in server] == ["perform_page_task"]

    def test_empty_batch(self):
        client, server = _partition_client_calls([], {"read_page"}, {"functions": []})
        assert client == []
        assert server == []

    def test_multiple_client_calls_preserved_in_order(self):
        calls = [_call("read_page"), _call("x"), _call("perform_page_task")]
        client, server = _partition_client_calls(
            calls, {"read_page", "perform_page_task"}, {"functions": []}
        )
        assert [c.function_name for c in client] == ["read_page", "perform_page_task"]
        assert [c.function_name for c in server] == ["x"]


class TestNodeShadowing:
    def test_per_node_function_shadows_client_tool(self):
        """A template author's own `read_page` node function wins locally."""
        calls = [_call("read_page")]
        node = {"functions": [_schema("read_page")]}
        client, server = _partition_client_calls(calls, {"read_page"}, node)
        assert client == []
        assert [c.function_name for c in server] == ["read_page"]

    def test_shadowing_is_scoped_to_the_shadowing_node(self):
        calls = [_call("read_page")]
        node = {"functions": [_schema("something_else")]}
        client, server = _partition_client_calls(calls, {"read_page"}, node)
        assert [c.function_name for c in client] == ["read_page"]
        assert server == []

    def test_non_schema_node_entries_do_not_shadow(self):
        """Only FlowsFunctionSchema entries are dispatchable, so only they shadow."""
        calls = [_call("read_page")]
        node = {"functions": [{"name": "read_page"}]}  # plain dict
        client, server = _partition_client_calls(calls, {"read_page"}, node)
        assert [c.function_name for c in client] == ["read_page"]

    def test_missing_functions_key(self):
        calls = [_call("read_page")]
        client, _ = _partition_client_calls(calls, {"read_page"}, {})
        assert [c.function_name for c in client] == ["read_page"]


class TestGateBeforeClientOrdering:
    """The composition used in _cycle_loop: gate first, then split client.

    A client tool the template ALSO gates must end up in the approval path.
    Handing it to the browser would execute an action the user never
    confirmed — the exact failure the gate exists to prevent.
    """

    def test_gated_client_tool_goes_to_approval_not_browser(self):
        calls = [_call("perform_page_task")]
        node = {"functions": []}

        gated, ungated = _partition_gated_calls(
            calls, {"perform_page_task": object()}, node
        )
        client, server = _partition_client_calls(ungated, {"perform_page_task"}, node)

        assert [c.function_name for c in gated] == ["perform_page_task"]
        assert client == [], "a gated client tool must not reach the browser"
        assert server == []

    def test_ungated_client_tool_still_reaches_the_browser(self):
        calls = [_call("perform_page_task")]
        node = {"functions": []}

        gated, ungated = _partition_gated_calls(calls, {}, node)
        client, server = _partition_client_calls(ungated, {"perform_page_task"}, node)

        assert gated == []
        assert [c.function_name for c in client] == ["perform_page_task"]
        assert server == []

    def test_mixed_batch_routes_each_call_correctly(self):
        calls = [
            _call("issue_refund"),  # gated server tool
            _call("perform_page_task"),  # client tool
            _call("search_products"),  # plain server tool
        ]
        node = {"functions": []}

        gated, ungated = _partition_gated_calls(calls, {"issue_refund": object()}, node)
        client, server = _partition_client_calls(ungated, {"perform_page_task"}, node)

        assert [c.function_name for c in gated] == ["issue_refund"]
        assert [c.function_name for c in client] == ["perform_page_task"]
        assert [c.function_name for c in server] == ["search_products"]


class TestConfirmedCallsAreGated:
    """A client call carrying ``confirmed: true`` must reach the approval
    queue, never the browser.

    This is the path that makes irreversible page actions possible at all —
    delete a member, pay, submit a form. Before it existed the browser
    refused those steps and nothing could say yes, so the tasks were dead
    ends. The flag is what a human approved; if it could skip the gate, the
    agent could delete things on its own say-so.
    """

    def _call(self, name, **args):
        return SimpleNamespace(function_name=name, arguments=args)

    def test_confirmed_client_call_is_not_handed_to_the_browser(self):
        node = {"functions": []}
        calls = [self._call("perform_page_task", goal="delete dummy", confirmed=True)]

        _, ungated = _partition_gated_calls(calls, {}, node)
        client, _server = _partition_client_calls(ungated, {"perform_page_task"}, node)
        confirmed = [c for c in client if bool(dict(c.arguments).get("confirmed"))]

        assert confirmed, "a confirmed call must be recognised as needing approval"

    def test_an_ordinary_page_task_is_NOT_gated(self):
        """Gating every page task would make the feature unusable — only the
        confirmed retry is consequential."""
        node = {"functions": []}
        calls = [self._call("perform_page_task", goal="switch to dark mode")]

        _, ungated = _partition_gated_calls(calls, {}, node)
        client, _server = _partition_client_calls(ungated, {"perform_page_task"}, node)
        confirmed = [c for c in client if bool(dict(c.arguments).get("confirmed"))]

        assert client, "an ordinary task still goes to the browser"
        assert not confirmed

    def test_confirmed_false_is_not_treated_as_approval(self):
        node = {"functions": []}
        calls = [self._call("perform_page_task", goal="x", confirmed=False)]

        _, ungated = _partition_gated_calls(calls, {}, node)
        client, _server = _partition_client_calls(ungated, {"perform_page_task"}, node)

        assert not [c for c in client if bool(dict(c.arguments).get("confirmed"))]
