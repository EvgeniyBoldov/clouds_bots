from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from gateway import integrations, main, templates
from gateway.models import (
    Base, Bot, Chat, InternalBot, Job, NotificationIntegration, NotificationRoute, NotificationToken, UserGrant,
)
from gateway.templates import BaseBot, command, register_template


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
async def gateway_client(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

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


@pytest.mark.parametrize("target", ["internal", "botx", "system", "route", "route_replacement"])
async def test_delete_cascades(gateway_client, target):
    client, sessions = gateway_client
    bot_id, chat_id = uuid4(), uuid4()
    async with sessions.begin() as session:
        bot = Bot(id=bot_id, name="BotX")
        system = NotificationIntegration(name="System", description="Notifications")
        session.add_all([bot, system])
        await session.flush()
        handler = InternalBot(name="Handler", botx_id=bot_id, external_system_id=system.id)
        chat = Chat(bot_id=bot_id, chat_id=chat_id)
        session.add_all([handler, chat, NotificationRoute(integration_id=system.id, bot_id=bot_id)])
        await session.flush()
        grant = UserGrant(internal_bot_id=handler.id, user_id="user")
        session.add(grant)

    system_url = f"/api/v1/admin/notification-integrations/{system.id}"
    route_url = f"{system_url}/routes/{bot_id}"
    scoped = await client.post(f"{system_url}/tokens", json={"bot_id": str(bot_id)})
    global_key = await client.post(f"{system_url}/tokens")
    assert scoped.status_code == global_key.status_code == 201
    token_id = scoped.json()["id"]
    async with sessions.begin() as session:
        job = Job(bot_id=bot_id, chat_id=chat_id, payload={}, notification_token_id=token_id)
        session.add(job)

    if target == "route_replacement":
        response = await client.put(system_url, json={"name": "System", "description": "Notifications", "routes": []})
        assert response.status_code == 200, response.text
    else:
        urls = {
            "internal": f"/api/v1/admin/internal-bots/{handler.id}",
            "botx": f"/api/v1/admin/botx/{bot_id}",
            "system": system_url,
            "route": route_url,
        }
        response = await client.delete(urls[target])
        assert response.status_code == 204, response.text

    async with sessions() as session:
        assert (await session.get(Bot, bot_id) is None) == (target == "botx")
        assert (await session.get(Chat, chat.id) is None) == (target == "botx")
        assert (await session.get(UserGrant, grant.id) is None) == (target == "internal")
        assert (await session.get(NotificationIntegration, system.id) is None) == (target == "system")
        assert (await session.get(NotificationToken, global_key.json()["id"]) is None) == (target == "system")
        assert (await session.get(NotificationRoute, (system.id, bot_id)) is not None) == (target == "internal")
        assert (await session.get(NotificationToken, token_id) is not None) == (target == "internal")
        assert (await session.get(Job, job.id)).notification_token_id == (token_id if target == "internal" else None)
        remaining_handler = await session.get(InternalBot, handler.id)
        if target == "internal":
            assert remaining_handler is None
        else:
            assert remaining_handler.botx_id == (None if target == "botx" else bot_id)
            assert remaining_handler.external_system_id == (None if target == "system" else system.id)

    if target in {"route", "route_replacement"}:
        assert (await client.put(route_url, json={"chat_ids": []})).status_code == 200
        response = await client.post(
            "/api/v1/notifications",
            headers={"X-API-Key": scoped.json()["token"]},
            json={"chat_id": str(chat_id), "text": "test"},
        )
        assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ["http://status.example.org:8080", "https://status.example.org"])
async def test_template_requests_use_only_the_linked_external_system(gateway_client, monkeypatch, endpoint):
    client, sessions = gateway_client
    systems_url = "/api/v1/admin/notification-integrations"
    payload = {
        "name": "Status service",
        "description": "Status information",
        "outbound_url": f"{endpoint}/api?format=json",
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
    assert str(requests[0].url) == f"{endpoint}/api?format=json&check=status"
    assert requests[0].headers["X-API-Key"] == "external-secret"
    assert "Authorization" not in requests[0].headers
    assert (await client.put(bot_url, json=bot_payload)).json()["external_system_id"] == system_id
    assert (await client.put(bot_url, json={**bot_payload, "external_system_id": None})).status_code == 200
    assert await dispatch() == "unlinked"
    assert len(requests) == 1


async def test_connection_check_uses_saved_host_and_port(gateway_client, monkeypatch):
    client, _ = gateway_client
    systems_url = "/api/v1/admin/notification-integrations"
    payload = {"name": "Remote", "description": "Remote API"}
    for address in (
        "ftp://remote.example.org",
        "http://username:password@remote.example.org",
        "http://remote.example.org/api#fragment",
        "http://remote.example.org:0",
    ):
        response = await client.post(systems_url, json={**payload, "outbound_url": address})
        assert response.status_code == 422
    assert (await client.post(systems_url, json={
        **payload, "outbound_auth_type": "bearer",
    })).status_code == 422

    response = await client.post(systems_url, json=payload)
    system_url = f"{systems_url}/{response.json()['id']}"
    check_url = f"{system_url}/check"
    assert (await client.post(check_url)).status_code == 409
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(None, writer))
    monkeypatch.setattr(main.asyncio, "open_connection", connect)
    assert (await client.post(check_url, headers={"X-Admin-Token": "invalid"})).status_code == 401
    for address, port in (
        ("http://remote.example.org/api", 80),
        ("https://remote.example.org/api", 443),
        ("https://remote.example.org:8443/api", 8443),
        ("http://remote.example.org:8080/api", 8080),
    ):
        response = await client.put(system_url, json={**payload, "outbound_url": address})
        assert response.status_code == 200, response.text
        connect.reset_mock()
        assert (await client.post(check_url)).json() == {"status": "ok", "port": port}
        connect.assert_awaited_once_with("remote.example.org", port)
    assert writer.close.call_count == writer.wait_closed.await_count == 4

    connect.side_effect = OSError
    response = await client.post(check_url)
    assert response.status_code == 502 and "8080" in response.json()["detail"]
    connect.side_effect = TimeoutError
    assert (await client.post(check_url)).status_code == 504
