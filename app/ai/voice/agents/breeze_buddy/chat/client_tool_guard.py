"""Server-side secret screening for browser-reported client-tool outcomes.

The widget already redacts at extraction time — before a value is ever
serialised — and that is the redaction that matters, because a value which
never leaves the page cannot leak. This module is the **second** line: a
merchant's page shares a JS realm with every other script on it, so a
compromised or hostile page can lie to our in-page shim. "The client says
it redacted" is not evidence, so we re-screen everything the browser sends
before it is persisted or shown to the LLM.

Pure functions — no DB, no I/O — so the policy is unit-testable in
isolation from the endpoint.

Design choice: a positive match REJECTS the whole payload (HTTP 422) rather
than scrubbing it. Partial sanitisation of free text is how bypasses ship;
and a client that sends us a card number is either compromised or buggy, so
the right response is to refuse it loudly, not to quietly clean it up and
carry on.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterator, List, Optional, Tuple

# Field-name shapes that should never carry a value we store. Kept
# deliberately broad: a false positive costs one refused outcome, a false
# negative is a leaked secret.
_SENSITIVE_NAME_RE = re.compile(
    r"pass(word|wd)?|pwd|cvv|cvc|csc|card.?(num|no)|\bpan\b|secur(e|ity).?code|"
    r"otp|one.?time|2fa|mfa|auth.?code|verif(y|ication).?code|"
    r"ssn|social.?security|tax.?id|\bein\b|\bnin\b|aadhaar|"
    r"account.?number|routing|iban|swift|sort.?code|\bpin\b",
    re.IGNORECASE,
)

_JWT_RE = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*")
_API_KEY_RE = re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{16,}")
_CREDENTIAL_RE = re.compile(
    r"\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_|\bAKIA[0-9A-Z]{16}\b"
)
# Spaces are allowed between groups: banks print IBANs as
# "GB82 WEST 1234 5698 7654 32", and _iban_ok strips them before the
# mod-97 check. Requiring contiguous characters would miss the common case.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
_DIGIT_RUN_RE = re.compile(r"(?:\d[ -]?){13,19}")
_HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum. ``digits`` must already be stripped."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    """IBAN mod-97 check (ISO 13616). Rearrange, letters->digits, %97 == 1."""
    s = candidate.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    rearranged = s[4:] + s[:4]
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    if not converted.isdigit():
        return False
    return int(converted) % 97 == 1


def _shannon_entropy(s: str) -> float:
    """Bits per character. A random token sits well above 4.0; English prose
    and base64-looking-but-structured strings sit below."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scan_string(value: str) -> Optional[str]:
    """Return a reason string if ``value`` looks like a secret, else None.

    Order matters only for message quality — any single match rejects.
    """
    if not value:
        return None

    if _JWT_RE.search(value):
        return "jwt"
    if _API_KEY_RE.search(value):
        return "api_key"
    if _CREDENTIAL_RE.search(value):
        return "credential"

    for m in _IBAN_RE.finditer(value):
        if _iban_ok(m.group(0)):
            return "iban"

    # Card numbers are checked with separators stripped, because a real one
    # is routinely rendered "4111 1111 1111 1111".
    for m in _DIGIT_RUN_RE.finditer(value):
        if _luhn_ok(re.sub(r"[ -]", "", m.group(0))):
            return "card_number"

    for m in _HIGH_ENTROPY_RE.finditer(value):
        token = m.group(0)
        # A long lowercase word or a hex-ish id is not a secret; require
        # genuine character-class mixing AND high entropy before rejecting.
        if (
            _shannon_entropy(token) >= 4.0
            and any(c.isupper() for c in token)
            and any(c.islower() for c in token)
        ):
            return "high_entropy_token"

    return None


def _walk_strings(node: Any, path: str = "") -> Iterator[Tuple[str, str]]:
    """Yield every (path, string) in a nested payload."""
    if isinstance(node, str):
        yield path or "<root>", node
    elif isinstance(node, dict):
        for k, v in node.items():
            key = str(k)
            # A sensitive-looking KEY is itself disqualifying, whatever the
            # value: the browser should never have sent us that field.
            if _SENSITIVE_NAME_RE.search(key):
                yield f"{path}.{key}" if path else key, f"__sensitive_key__:{key}"
            yield from _walk_strings(v, f"{path}.{key}" if path else key)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk_strings(v, f"{path}[{i}]")


def screen_outcome(payload: Any) -> List[str]:
    """Screen a browser-reported outcome. Returns a list of violations.

    Empty list means the payload is clean. Violations are reported as
    ``"<path>: <reason>"`` and are safe to log — they name the location and
    the KIND of secret, never the value itself.
    """
    violations: List[str] = []
    for path, value in _walk_strings(payload):
        if value.startswith("__sensitive_key__:"):
            violations.append(f"{path}: sensitive_field_name")
            continue
        reason = scan_string(value)
        if reason:
            violations.append(f"{path}: {reason}")
    return violations


__all__ = ["screen_outcome", "scan_string"]
