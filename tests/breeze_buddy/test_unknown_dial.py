"""A dial we got no reply for is not "not placed".

Plivo answering and refusing (4xx) means the call was not placed; no reply
(timeout, reset) or a server error (500) means it may be ringing. The worker
must not re-dial the latter at once, and Plivo's own answer / hangup webhook
attaches the call to its lead and counts its line ("adopt").

The Plivo dial goes through the real SDK; only its HTTP send is replaced.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any, List, Optional, cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from starlette.requests import Request
from urllib3.exceptions import MaxRetryError, NewConnectionError

# dispatch must import first (managers.calls <-> dispatch.worker cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    callback_signature,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.exotel.exotel import (
    ExotelProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    plivo as plivo_mod,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    PlivoProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.twilio.twilio import (
    TwilioProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.vobiz import (
    VobizProvider,
)
from app.api.routers.breeze_buddy.telephony.answer import handlers as ans_mod
from app.api.routers.breeze_buddy.telephony.callbacks import handlers as cb_mod
from app.core.config import static
from app.database.queries.breeze_buddy.lead_call_tracker import (
    adopt_unknown_dial_query,
)
from app.schemas import CallDirection, LeadCallStatus

CUSTOMER = "+919000000001"
NUMBER = "+918000000001"
IDS = {"lead_id": "L1", "number_id": "N1"}
SIG = callback_signature("L1", "N1")


def plivo_provider(monkeypatch: pytest.MonkeyPatch, reply: Any) -> tuple:
    """A real PlivoProvider whose HTTP send returns ``reply`` (a status code)
    or raises it (an exception). Returns (provider, sent requests)."""
    monkeypatch.setattr(plivo_mod, "OUTBOUND_RING_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", "MATEST00000000000001")
    monkeypatch.setattr(static, "PLIVO_AUTH_TOKEN", "test-token")
    provider = PlivoProvider(aiohttp_session=None)
    provider.APP_BASE_URL = "https://bb.example.test"
    sent: List[requests.PreparedRequest] = []

    def send(prepared: requests.PreparedRequest, **_k: Any) -> requests.Response:
        sent.append(prepared)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.status_code = reply
        response.headers["Content-Type"] = "application/json"
        body = {"api_id": "a-1", "request_uuid": "u-1", "message": "x"}
        response._content = json.dumps(body).encode()
        response.url = prepared.url or ""
        return response

    monkeypatch.setattr(provider.client.session, "send", send)
    return provider, sent


# --- Plivo adapter: refused vs unknown ----------------------------------------


def test_plivo_timeout_is_unknown_not_refused(monkeypatch):
    provider, _ = plivo_provider(monkeypatch, requests.exceptions.ReadTimeout("t"))
    assert provider.make_call(CUSTOMER, NUMBER) == {"status": "unknown", "sid": None}


def test_plivo_connection_reset_is_unknown(monkeypatch):
    provider, _ = plivo_provider(monkeypatch, requests.exceptions.ConnectionError())
    assert provider.make_call(CUSTOMER, NUMBER) == {"status": "unknown", "sid": None}


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_plivo_5xx_is_unknown(monkeypatch, status):
    provider, _ = plivo_provider(monkeypatch, status)
    assert provider.make_call(CUSTOMER, NUMBER) == {"status": "unknown", "sid": None}


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectTimeout(),
        requests.exceptions.ConnectionError(
            MaxRetryError(
                cast(Any, None), "/", NewConnectionError(cast(Any, None), "refused")
            )
        ),
    ],
)
def test_plivo_never_connected_is_not_placed(monkeypatch, error):
    provider, _ = plivo_provider(monkeypatch, error)
    assert provider.make_call(CUSTOMER, NUMBER) is None


@pytest.mark.parametrize("status", [400, 401, 402, 404, 429])
def test_plivo_4xx_is_not_placed(monkeypatch, status):
    provider, _ = plivo_provider(monkeypatch, status)
    assert provider.make_call(CUSTOMER, NUMBER) is None


def test_plivo_urls_carry_the_ids(monkeypatch):
    provider, sent = plivo_provider(monkeypatch, 201)

    assert provider.make_call(CUSTOMER, NUMBER, callback_params=IDS)["sid"] == "u-1"

    body = json.loads(sent[0].body or b"{}")
    answer = parse_qs(urlparse(body["answer_url"]).query)
    hangup = parse_qs(urlparse(body["hangup_url"]).query)
    assert answer["lead_id"] == ["L1"] and answer["number_id"] == ["N1"]
    assert hangup == {"lead_id": ["L1"], "number_id": ["N1"], "signature": [SIG]}


def test_the_signed_answer_url_is_not_logged(monkeypatch):
    logged: List[str] = []
    monkeypatch.setattr(plivo_mod.logger, "info", lambda m, *_a, **_k: logged.append(m))
    provider, _ = plivo_provider(monkeypatch, 201)

    provider.make_call(CUSTOMER, NUMBER, callback_params=IDS)

    assert logged and not any(SIG in line for line in logged)


def test_plivo_urls_unchanged_without_ids(monkeypatch):
    provider, sent = plivo_provider(monkeypatch, 201)

    provider.make_call(CUSTOMER, NUMBER, reseller_id="r1")

    body = json.loads(sent[0].body or b"{}")
    assert body["answer_url"] == (
        "https://bb.example.test/agent/voice/breeze-buddy/plivo/answer?reseller_id=r1"
    )
    assert body["hangup_url"] == (
        "https://bb.example.test/agent/voice/breeze-buddy/plivo/callback/status"
    )


@pytest.mark.parametrize(
    "provider", [PlivoProvider, VobizProvider, ExotelProvider, TwilioProvider]
)
def test_every_adapter_accepts_callback_params(provider):
    """The worker passes it for every provider; one without it would raise."""
    assert "callback_params" in inspect.signature(provider.make_call).parameters


# --- adopt query ----------------------------------------------------------------


def test_adopt_query_guards():
    sql, values = adopt_unknown_dial_query("L1", "CALL-1", "N1")

    assert values[:3] == ["L1", "CALL-1", "N1"]
    assert values[3:] == [
        LeadCallStatus.PROCESSING.value,
        LeadCallStatus.BACKLOG.value,
        CallDirection.OUTBOUND.value,
    ]
    assert '"status" = $5 AND "call_direction" = $6' in sql, "BACKLOG outbound only"
    assert '"is_locked"' not in sql, "must not wait for the worker's lock"
    assert "FROM adopted" in sql, "the line is counted only if the lead was adopted"


# --- webhooks -------------------------------------------------------------------


def form_request(query: bytes, body: bytes) -> Request:
    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": query,
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    return Request(scope, receive)


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_answer_adopts_before_routing(monkeypatch):
    order: List[Any] = []

    async def adopt(*args: Any) -> None:
        order.append(("adopt", args))

    async def resolve(*_a: Any) -> None:
        order.append("resolve")
        raise _Stop

    monkeypatch.setattr(ans_mod, "adopt_unknown_dial", adopt)
    monkeypatch.setattr(ans_mod, "resolve_call_templates", resolve)

    request = form_request(
        b"lead_id=L1&number_id=N1&signature=" + SIG.encode(),
        b"CallUUID=CALL-1&From=%2B918&To=%2B919",
    )
    with pytest.raises(_Stop):
        await ans_mod._handle_provider_answer(request, "plivo")

    assert order == [("adopt", ("L1", "CALL-1", "N1")), "resolve"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,provider",
    [
        (b"", "plivo"),
        (b"lead_id=L1&number_id=N1", "plivo"),  # not signed by us
        (
            b"lead_id=L1&number_id=N2&signature=" + SIG.encode(),
            "plivo",
        ),  # number swapped
        (b"lead_id=L1&number_id=N1&signature=" + SIG.encode(), "vobiz"),
    ],
)
async def test_answer_without_ids_or_not_plivo_never_adopts(
    monkeypatch, query, provider
):
    adopted: List[Any] = []

    async def adopt(*args: Any) -> None:
        adopted.append(args)

    async def resolve(*_a: Any) -> None:
        raise _Stop

    monkeypatch.setattr(ans_mod, "adopt_unknown_dial", adopt)
    monkeypatch.setattr(ans_mod, "resolve_call_templates", resolve)

    request = form_request(query, b"CallUUID=CALL-1&From=%2B918&To=%2B919")
    with pytest.raises(_Stop):
        await ans_mod._handle_provider_answer(request, provider)

    assert adopted == []


@pytest.mark.asyncio
async def test_status_adopts_before_the_end_flow(monkeypatch):
    order: List[Any] = []

    async def adopt(*args: Any) -> None:
        order.append(("adopt", args))

    async def unanswered(call_id: str) -> None:
        order.append(("unanswered", call_id))

    async def noop(*_a: Any, **_k: Any) -> Optional[Any]:
        return None

    monkeypatch.setattr(cb_mod, "adopt_unknown_dial", adopt)
    monkeypatch.setattr(cb_mod, "handle_unanswered_calls", unanswered)
    monkeypatch.setattr(cb_mod, "safe_release_pod", noop)
    monkeypatch.setattr(cb_mod, "get_lead_by_call_id", noop)

    request = form_request(
        b"lead_id=L1&number_id=N1&signature=" + SIG.encode(),
        b"CallUUID=CALL-1&CallStatus=no-answer",
    )
    await cb_mod.handle_callback_status(request, "plivo")

    assert order == [("adopt", ("L1", "CALL-1", "N1")), ("unanswered", "CALL-1")]


# --- adopt on real Postgres -----------------------------------------------------

DSN = os.environ.get("DIALQ_TEST_DSN")


@pytest.mark.asyncio
@pytest.mark.skipif(not DSN, reason="set DIALQ_TEST_DSN to a scratch Postgres")
async def test_adopt_once_and_counts_the_line_on_postgres():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("""
            DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;
            CREATE TABLE dial_queue (
                lead_id text PRIMARY KEY, state text, state_at timestamptz);
            CREATE TABLE lead_call_tracker (
                id text PRIMARY KEY, status text, call_direction text,
                call_id text, call_initiated_time timestamptz,
                telephony_number_id text, updated_at timestamptz);
            CREATE TABLE telephony_numbers (
                id text PRIMARY KEY, channels int, updated_at timestamptz);
            INSERT INTO lead_call_tracker (id, status, call_direction)
                VALUES ('L1', 'BACKLOG', 'OUTBOUND'), ('L2', 'BACKLOG', 'OUTBOUND');
            INSERT INTO telephony_numbers VALUES ('N1', 2, now());
            """)

        other = await asyncpg.connect(DSN)

        async def adopt(c: Any, lead: str, call: str, number: str) -> list:
            sql, values = adopt_unknown_dial_query(lead, call, number)
            return await c.fetch(sql, *values)

        try:
            first, second = await asyncio.gather(
                adopt(conn, "L1", "CALL-A", "N1"), adopt(other, "L1", "CALL-B", "N1")
            )
        finally:
            await other.close()
        assert len(first) + len(second) == 1, "a lead is adopted once"
        assert await conn.fetchval("SELECT channels FROM telephony_numbers") == 3

        # A number id that does not exist counts nothing.
        [row] = await adopt(conn, "L2", "CALL-C", "NOPE")
        assert row["telephony_number_id"] is None
        assert await conn.fetchval("SELECT channels FROM telephony_numbers") == 3
    finally:
        await conn.execute(
            "DROP TABLE IF EXISTS dial_queue, lead_call_tracker, telephony_numbers;"
        )
        await conn.close()


def test_a_signature_is_never_accepted_without_a_key_or_when_malformed(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.services.telephony import base_provider

    assert base_provider.callback_signed("L1", "N1", SIG)
    assert not base_provider.callback_signed("L1", "N1", "é" * 32), "no crash"
    monkeypatch.setattr(base_provider, "JWT_SECRET_KEY", "")
    assert not base_provider.callback_signed("L1", "N1", SIG), "anyone could sign"
