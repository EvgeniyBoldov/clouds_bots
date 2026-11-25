from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from gateway import integrations, main, templates
from gateway.models import Base, InternalBot
from gateway.templates import BaseBot, command, register_template


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
async def gateway_client(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main.settings, "admin_token", "admin-secret")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://test",
            headers={"X-Admin-Token": "admin-secret"},
        ) as client:
            yield client, sessions
    finally:
        await engine.dispose()


async def test_template_requests_use_only_the_linked_external_system(gateway_client, monkeypatch):
    client, sessions = gateway_client
    systems_url = "/api/v1/admin/notification-integrations"
    payload = {
        "name": "Status service",
        "description": "Status information",
        "outbound_url": "https://status.example.org/api?format=json",
        "outbound_auth_type": "api_key",
        "outbound_token": "external-secret",
    }
    created = await client.post(systems_url, json=payload)
    assert created.status_code == 201, created.text
    system_id = created.json()["id"]
    system_url = f"{systems_url}/{system_id}"
    issued = await client.post(f"{system_url}/tokens")
    assert issued.status_code == 201, issued.text
    assert issued.json()["token"] != payload["outbound_token"]

    # An older admin client can still edit the description without touching credentials.
    legacy_payload = {"name": payload["name"], "description": "Updated status information"}
    assert (await client.put(system_url, json=legacy_payload)).status_code == 200
    listed = await client.get(systems_url)
    assert payload["outbound_token"] not in listed.text
    assert listed.json()[0]["outbound_token_set"] is True
    assert listed.json()[0]["routes"] == []

    requests = []

    def external_api(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "running"})

    def external_client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(external_api))

    monkeypatch.setattr(integrations, "httpx", SimpleNamespace(URL=httpx.URL, AsyncClient=external_client))
    monkeypatch.setattr(templates, "TEMPLATE_REGISTRY", templates.TEMPLATE_REGISTRY.copy())

    @register_template("test-external", name="Test external system")
    class StatusBot(BaseBot):
        @command("/status")
        async def status(self, request):
            if request.external_system is None:
                return "unlinked"
            response = await request.external_system.request("GET", params={"check": "status"})
            return response.json()["status"]

    bots_url = "/api/v1/admin/internal-bots"
    bot_payload = {"name": "Status bot", "template_id": "test-external"}
    bot_response = await client.post(bots_url, json={**bot_payload, "external_system_id": system_id})
    assert bot_response.status_code == 201, bot_response.text
    assert bot_response.json()["external_system_id"] == system_id
    bot_id = UUID(bot_response.json()["id"])
    bot_url = f"{bots_url}/{bot_id}"

    async def dispatch():
        async with sessions() as session:
            bot = await session.get(InternalBot, bot_id)
        return await main.dispatch_queued_message(
            bot, uuid4(), uuid4(), {"sender_id": "user", "message_id": "1", "text": "/status"},
        )

    assert await dispatch() == "running"
    assert str(requests[0].url) == "https://status.example.org/api?format=json&check=status"
    assert requests[0].headers["X-API-Key"] == "external-secret"
    assert "Authorization" not in requests[0].headers
    assert (await client.put(bot_url, json=bot_payload)).json()["external_system_id"] == system_id
    assert (await client.put(bot_url, json={**bot_payload, "external_system_id": None})).status_code == 200
    assert await dispatch() == "unlinked"
    assert len(requests) == 1


async def test_connection_check_uses_saved_host_and_tcp_443(gateway_client, monkeypatch):
    client, _ = gateway_client
    systems_url = "/api/v1/admin/notification-integrations"
    payload = {"name": "Remote", "description": "Remote API"}
    for address in ("http://remote.example.org", "https://remote.example.org:8443"):
        response = await client.post(systems_url, json={**payload, "outbound_url": address})
        assert response.status_code == 422
    assert (await client.post(systems_url, json={
        **payload, "outbound_auth_type": "bearer",
    })).status_code == 422

    response = await client.post(systems_url, json=payload)
    system_url = f"{systems_url}/{response.json()['id']}"
    check_url = f"{system_url}/check"
    assert (await client.post(check_url)).status_code == 409
    assert (await client.put(system_url, json={
        **payload, "outbound_url": "https://remote.example.org/api",
    })).status_code == 200
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(None, writer))
    monkeypatch.setattr(main.asyncio, "open_connection", connect)
    assert (await client.post(check_url, headers={"X-Admin-Token": "invalid"})).status_code == 401
    assert (await client.post(check_url)).json() == {"status": "ok", "port": 443}
    connect.assert_awaited_once_with("remote.example.org", 443)
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()

    connect.side_effect = OSError
    assert (await client.post(check_url)).status_code == 502
    connect.side_effect = TimeoutError
    assert (await client.post(check_url)).status_code == 504
