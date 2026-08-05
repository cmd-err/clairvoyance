# pyrefly: ignore-errors
"""Server-side secret screening for client-tool outcomes
([[chat.client_tool_guard.screen_outcome]]).

The widget redacts at extraction time, and that is the redaction that
matters. This is the second line: a merchant's page shares a JS realm with
every other script on it, so a hostile page can lie to our in-page shim.
These tests pin the policy that runs on data the browser sends us.

Both directions matter. A false negative leaks a secret; a false positive
refuses a legitimate page description and makes the feature look broken.
The false-positive tests are therefore as load-bearing as the others.
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.chat.client_tool_guard import (
    scan_string,
    screen_outcome,
)

# Luhn-valid test numbers (the standard published test PANs — not real cards).
VISA = "4111111111111111"
MASTERCARD = "5500005555555559"
AMEX = "378282246310005"


class TestCardNumbers:
    @pytest.mark.parametrize("pan", [VISA, MASTERCARD, AMEX])
    def test_bare_card_number_detected(self, pan):
        assert scan_string(pan) == "card_number"

    def test_card_with_spaces_detected(self):
        """Real UIs render '4111 1111 1111 1111' — separators must be stripped."""
        assert scan_string("4111 1111 1111 1111") == "card_number"

    def test_card_with_hyphens_detected(self):
        assert scan_string("4111-1111-1111-1111") == "card_number"

    def test_card_embedded_in_prose_detected(self):
        assert scan_string(f"card on file ending {VISA}") == "card_number"

    def test_luhn_invalid_long_digits_not_flagged(self):
        """Order numbers / SKUs are long digit runs but fail Luhn."""
        assert scan_string("4111111111111112") is None

    def test_short_digit_run_not_flagged(self):
        assert scan_string("order 12345678") is None


class TestOtherSecretShapes:
    def test_jwt_detected(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def"
        assert scan_string(jwt) == "jwt"

    def test_stripe_live_key_detected(self):
        assert scan_string("sk_live_abcdefghijklmnop1234") == "api_key"

    def test_stripe_test_key_detected(self):
        assert scan_string("pk_test_abcdefghijklmnop1234") == "api_key"

    def test_github_token_detected(self):
        assert scan_string("ghp_abcdefghijklmnopqrstuvwxyz0123") == "credential"

    def test_aws_key_detected(self):
        assert scan_string("AKIAIOSFODNN7EXAMPLE") == "credential"

    def test_valid_iban_detected(self):
        assert scan_string("GB82 WEST 1234 5698 7654 32") == "iban"

    def test_iban_shaped_but_invalid_checksum_not_flagged(self):
        assert scan_string("GB00 WEST 1234 5698 7654 32") is None


class TestFalsePositives:
    """Ordinary page content must pass. These guard usability."""

    @pytest.mark.parametrize(
        "text",
        [
            "Registration received",
            "Your order #10023 has shipped",
            "Network error occurred. Please check your connection.",
            "Warranty Registration — Northwind Outdoors",
            "Add to cart",
            "£129.00",
            "deepa@example.com",
            "2026-03-03",
            "trailhead-40l",
            "",
        ],
    )
    def test_ordinary_page_text_passes(self, text):
        assert scan_string(text) is None

    def test_long_lowercase_slug_not_flagged_as_entropy(self):
        """A long product handle is not a secret."""
        assert scan_string("the-collection-snowboard-hydrogen-limited-edition") is None

    def test_long_hex_id_not_flagged(self):
        """Lowercase hex ids are common (commit shas, uuids) and low-entropy."""
        assert scan_string("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0") is None


class TestScreenOutcome:
    def test_clean_outcome_has_no_violations(self):
        payload = {
            "ok": True,
            "steps_completed": 3,
            "what_changed": ["Registration received"],
            "page_digest": "Warranty Registration — confirmation visible",
        }
        assert screen_outcome(payload) == []

    def test_secret_in_nested_list_is_found(self):
        payload = {"what_changed": ["filled email", f"card {VISA}"]}
        violations = screen_outcome(payload)
        assert len(violations) == 1
        assert "card_number" in violations[0]
        assert "what_changed[1]" in violations[0]

    def test_secret_in_page_digest_is_found(self):
        payload = {"page_digest": f"Saved card {VISA} on file"}
        assert any("card_number" in v for v in screen_outcome(payload))

    def test_sensitive_field_name_alone_is_a_violation(self):
        """The browser should never send us a field called `password` at all,
        whatever it contains."""
        payload = {"page_digest": "ok", "password": "anything"}
        assert any("sensitive_field_name" in v for v in screen_outcome(payload))

    def test_violation_never_contains_the_secret_itself(self):
        """Violations get logged — they must name the location and kind only."""
        payload = {"page_digest": f"card {VISA}"}
        for v in screen_outcome(payload):
            assert VISA not in v

    def test_multiple_violations_all_reported(self):
        payload = {
            "what_changed": [f"card {VISA}", "sk_live_abcdefghijklmnop1234"],
        }
        assert len(screen_outcome(payload)) == 2

    def test_deeply_nested_secret_is_found(self):
        payload = {"a": {"b": {"c": [{"d": VISA}]}}}
        assert any("card_number" in v for v in screen_outcome(payload))

    def test_non_string_leaves_are_ignored(self):
        payload = {"ok": True, "steps_completed": 3, "ratio": 1.5, "none": None}
        assert screen_outcome(payload) == []
