"""Unit tests for the in-house BotX client against a mocked eXpress (CTS) server."""
import json
import re
from uuid import uuid4

import httpx
import jwt
import pytest

from gateway.botx_client import (
    BotXClient,
    BotXClientError,
    BotXConnectionError,
    BotXHTTPError,
    BotXResponseError,
    BotXTimeoutError,
)

BOT_ID = uuid4()
SECRET = "bot-secret"
CTS_HOST = "cts.example.org"


class CTS:
    """Mock eXpress server: records every request and replies with a canned response."""

    def __init__(self, status_code: int = 200, json_body: object = None, text: str | None = None):
        self.status_code = status_code
        self.json_body = json_body
        self.text = text
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status_code, text=self.text)
        return httpx.Response(self.status_code, json=self.json_body)


def make_client(cts, cts_url: str = f"https://{CTS_HOST}") -> BotXClient:
    return BotXClient(
        bot_id=BOT_ID,
        cts_url=cts_url,
        secret_key=SECRET,
        transport=httpx.MockTransport(cts),
    )


async def test_list_chats_returns_the_result_list():
    chats = [{"group_chat_id": str(uuid4()), "chat_type": "group"}]
    cts = CTS(json_body={"result": chats})
    async with make_client(cts) as client:
        assert await client.list_chats() == chats
    (request,) = cts.requests
    assert request.method == "GET"
    assert str(request.url) == f"https://{CTS_HOST}/api/v3/botx/chats/list"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept"] == "application/json"
    assert "Authorization" in request.headers


async def test_find_personal_chat_returns_the_cts_address():
    chat_id, user_huid = uuid4(), uuid4()
    cts = CTS(json_body={"result": {"group_chat_id": str(chat_id)}})
    async with make_client(cts) as client:
        assert await client.find_personal_chat(user_huid) == chat_id
    request, = cts.requests
    assert request.url.path == "/api/v1/botx/chats/personal"
    assert request.url.params["user_huid"] == str(user_huid)

    async with make_client(CTS(status_code=404, json_body={})) as client:
        assert await client.find_personal_chat(user_huid) is None


async def test_each_request_carries_a_fresh_reference_shaped_jwt():
    cts = CTS(json_body={"result": []})
    async with make_client(cts) as client:
        await client.list_chats()
        await client.list_chats()

    claims = [
        jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRET,
            algorithms=["HS256"],
            audience=CTS_HOST,
            issuer=str(BOT_ID),
        )
        for request in cts.requests
    ]
    for claim in claims:
        assert set(claim) == {"iss", "aud", "iat", "nbf", "exp", "jti", "version"}
        assert claim["iss"] == str(BOT_ID)
        assert claim["aud"] == CTS_HOST
        assert claim["version"] == 2
        assert claim["exp"] - claim["iat"] == 60
        assert claim["nbf"] == claim["iat"]
        assert re.fullmatch(r"[0-9a-f]{32}", claim["jti"])
    assert claims[0]["jti"] != claims[1]["jti"]


async def test_base_url_always_normalizes_to_a_trailing_slash():
    for cts_url in (f"https://{CTS_HOST}", f"https://{CTS_HOST}/"):
        cts = CTS(json_body={"result": []})
        async with make_client(cts, cts_url=cts_url) as client:
            await client.list_chats()
        assert str(cts.requests[0].url) == f"https://{CTS_HOST}/api/v3/botx/chats/list"


async def test_find_user_by_email_returns_the_user_record():
    user = {"user_huid": str(uuid4()), "email": "user@example.org"}
    cts = CTS(json_body={"result": user})
    async with make_client(cts) as client:
        assert await client.find_user_by_email("user@example.org") == user
    (request,) = cts.requests
    assert request.method == "GET"
    assert request.url.path == "/api/v3/botx/users/by_email"
    assert dict(request.url.params) == {"email": "user@example.org"}


async def test_find_user_by_email_accepts_single_element_list_results():
    user = {"user_huid": str(uuid4()), "email": "user@example.org"}
    async with make_client(CTS(json_body={"result": [user]})) as client:
        assert await client.find_user_by_email("user@example.org") == user
    async with make_client(CTS(json_body={"result": []})) as client:
        assert await client.find_user_by_email("user@example.org") is None


async def test_find_user_by_email_maps_a_missing_user_to_none():
    cts = CTS(status_code=404, json_body={"error": "user not found"})
    async with make_client(cts) as client:
        assert await client.find_user_by_email("missing@example.org") is None
    (request,) = cts.requests
    assert request.url.path == "/api/v3/botx/users/by_email"


async def test_find_user_by_ad_queries_the_login_endpoint():
    user = {"user_huid": str(uuid4()), "ad_login": "ivanov"}
    cts = CTS(json_body={"result": user})
    async with make_client(cts) as client:
        assert await client.find_user_by_ad("ivanov", "example.org") == user
    (request,) = cts.requests
    assert request.url.path == "/api/v3/botx/users/by_login"
    assert dict(request.url.params) == {"ad_login": "ivanov", "ad_domain": "example.org"}
    async with make_client(CTS(status_code=404, json_body={"error": "no"})) as client:
        assert await client.find_user_by_ad("missing", "example.org") is None


async def test_create_chat_posts_the_reference_body_and_returns_the_chat_id():
    chat_id, member = uuid4(), uuid4()
    cts = CTS(json_body={"result": {"chat_id": str(chat_id)}})
    async with make_client(cts) as client:
        assert await client.create_chat("Personal chat", "chat", [member]) == chat_id
    (request,) = cts.requests
    assert request.method == "POST"
    assert request.url.path == "/api/v3/botx/chats/create"
    assert json.loads(request.content) == {
        "name": "Personal chat",
        "chat_type": "chat",
        "members": [str(member)],
    }


async def test_send_notification_posts_the_reference_body():
    chat_id = uuid4()
    group_id = str(uuid4())
    cts = CTS(json_body={"result": {"notification_group_id": group_id}})
    async with make_client(cts) as client:
        assert await client.send_notification(chat_id, "Deploy finished\n") == {
            "notification_group_id": group_id,
        }
    (request,) = cts.requests
    assert request.method == "POST"
    assert request.url.path == "/api/v4/botx/notifications/direct"
    assert json.loads(request.content) == {
        "group_chat_id": str(chat_id),
        "notification": {"body": "Deploy finished\n"},
    }


@pytest.mark.parametrize("status_code", [401, 404, 500])
async def test_error_statuses_are_wrapped_as_http_errors(status_code):
    cts = CTS(status_code=status_code, json_body={"error": "boom"})
    async with make_client(cts) as client:
        with pytest.raises(BotXHTTPError) as info:
            await client.list_chats()
    assert info.value.status_code == status_code
    assert info.value.method == "GET"
    assert info.value.url.endswith("/api/v3/botx/chats/list")
    assert "boom" in info.value.body


async def test_malformed_success_payloads_are_response_errors():
    async with make_client(CTS(json_body={"status": "ok"})) as client:
        with pytest.raises(BotXResponseError, match="result"):
            await client.list_chats()
    async with make_client(CTS(json_body={"result": {"unexpected": "shape"}})) as client:
        with pytest.raises(BotXResponseError):
            await client.list_chats()
    async with make_client(CTS(json_body={"result": {"chat_id": "not-a-uuid"}})) as client:
        with pytest.raises(BotXResponseError):
            await client.create_chat("Personal chat", "chat", [uuid4()])
    async with make_client(CTS(json_body={"result": ["not", "an", "object"]})) as client:
        with pytest.raises(BotXResponseError):
            await client.send_notification(uuid4(), "hi")


async def test_non_json_success_payload_is_a_response_error():
    cts = CTS(text="<html>Gateway Time-out</html>")
    async with make_client(cts) as client:
        with pytest.raises(BotXResponseError):
            await client.list_chats()


async def test_timeouts_are_wrapped_as_botx_timeout_errors():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("CTS did not answer")

    async with make_client(handler) as client:
        with pytest.raises(BotXTimeoutError) as info:
            await client.list_chats()
    assert isinstance(info.value, TimeoutError)
    assert isinstance(info.value, BotXClientError)


async def test_connection_failures_are_wrapped_as_botx_connection_errors():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with make_client(handler) as client:
        with pytest.raises(BotXConnectionError) as info:
            await client.list_chats()
    assert isinstance(info.value, BotXClientError)
