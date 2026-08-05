# pyrefly: ignore-errors
"""Tests for [[template.types.ClientToolsConfig]] — the per-template opt-in
for browser-executed (client) tools.

The load-bearing property is that the feature is **inert by default**: a
template that has never heard of client tools must expose zero client tools
to the LLM, so adding this config can't change any existing template's
behaviour.

Also guards the tool allowlist. ``act_on_page`` is deliberately NOT
exposable: it exists only as an internal primitive the browser subagent
drives. Exposing it to the outer LLM invites step-by-step micromanagement,
which is the latency failure mode the subagent design exists to avoid.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.types import (
    ClientToolsConfig,
    ConfigurationModel,
)


class TestDefaultsAreInert:
    def test_bare_config_is_disabled(self):
        cfg = ClientToolsConfig()
        assert cfg.enabled is False
        assert cfg.tools == []

    def test_configuration_model_defaults_to_absent(self):
        """An existing template with no client_tools key stays unaffected."""
        assert ConfigurationModel().client_tools is None

    def test_enabled_without_tools_still_exposes_nothing(self):
        """`enabled` alone is not enough — the tools list is the real switch."""
        cfg = ClientToolsConfig(enabled=True)
        assert cfg.tools == []


class TestToolAllowlist:
    @pytest.mark.parametrize("name", ["read_page", "perform_page_task"])
    def test_known_tools_accepted(self, name):
        assert ClientToolsConfig(enabled=True, tools=[name]).tools == [name]

    def test_both_tools_accepted(self):
        cfg = ClientToolsConfig(enabled=True, tools=["read_page", "perform_page_task"])
        assert len(cfg.tools) == 2

    def test_unknown_tool_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ClientToolsConfig(enabled=True, tools=["delete_everything"])
        assert "delete_everything" in str(exc.value)

    def test_act_on_page_is_not_exposable(self):
        """Internal-only primitive — must never reach the outer LLM's tool list."""
        with pytest.raises(ValidationError) as exc:
            ClientToolsConfig(enabled=True, tools=["act_on_page"])
        assert "act_on_page" in str(exc.value)

    def test_partially_valid_list_is_rejected_whole(self):
        with pytest.raises(ValidationError):
            ClientToolsConfig(enabled=True, tools=["read_page", "nope"])


class TestBounds:
    def test_max_steps_default(self):
        assert ClientToolsConfig().max_steps == 8

    @pytest.mark.parametrize("bad", [0, -1, 31])
    def test_max_steps_out_of_range_rejected(self, bad):
        """A runaway loop on a merchant's page is the thing being bounded."""
        with pytest.raises(ValidationError):
            ClientToolsConfig(max_steps=bad)

    @pytest.mark.parametrize("ok", [1, 8, 30])
    def test_max_steps_in_range_accepted(self, ok):
        assert ClientToolsConfig(max_steps=ok).max_steps == ok


class TestOptionalFields:
    def test_inner_model_defaults_none(self):
        assert ClientToolsConfig().inner_model is None

    def test_origin_allowlist_defaults_empty(self):
        assert ClientToolsConfig().origin_allowlist == []

    def test_round_trips_through_configuration_model(self):
        cfg = ConfigurationModel(
            client_tools=ClientToolsConfig(enabled=True, tools=["perform_page_task"])
        )
        assert cfg.client_tools is not None
        assert cfg.client_tools.tools == ["perform_page_task"]
