import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, call
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from gateway import chats, main
from gateway.bot import incoming_message
from gateway.botx_client import BotXConnectionError
from gateway.models import (
    Base,
    Bot,
    Chat,
    InternalBot,
    Job,
    NotificationIntegration,
    NotificationRoute,
    NotificationToken,
    UserGrant,
)


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


async def test_notifications_are_queued_and_forwarded_after_incoming_jobs(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main.settings, "notification_token", "notification-secret")
    monkeypatch.setattr(main.settings, "admin_token", "admin-secret")
    monkeypatch.setattr(main.settings, "worker_poll_seconds", 0.01)

    bot_id, chat_id, disabled_id = uuid4(), uuid4(), uuid4()
    payload = {"bot_id": str(bot_id), "chat_id": str(chat_id), "text": "Deploy finished\n"}
    headers = {"X-API-Key": "notification-secret"}
    url = "/api/v1/notifications"

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add_all([
                Bot(id=bot_id, name="test", cts_url="https://cts.example.org", secret_key="bot-secret"),
                Bot(id=disabled_id, name="disabled", enabled=False),
            ])
            await session.flush()
            session.add(InternalBot(botx_id=bot_id, name="test handler"))
            incoming = Job(bot_id=bot_id, chat_id=chat_id, payload={"kind": "message"})
            session.add(incoming)
        monkeypatch.setattr(main, "dispatch_queued_message", AsyncMock(return_value="reply"))
        deliver = AsyncMock(side_effect=[RuntimeError("BotX unavailable"), None] + [
            RuntimeError("BotX unavailable") for _ in range(3)
        ])
        monkeypatch.setattr(main, "deliver_notification", deliver)
        send = AsyncMock()

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post(url, json=payload)).status_code == 401
            assert (await client.get("/api/v1/admin/bots", headers=headers)).status_code == 401
            assert (await client.post(url, headers=headers, json=payload | {"text": " \n"})).status_code == 422
            assert (await client.post(url, headers=headers, json=payload | {
                "bot_id": str(disabled_id),
            })).status_code == 404
            assert (await client.get(f"{url}/{incoming.id}", headers=headers)).status_code == 404

            response = await client.post(url, headers=headers, json=payload)
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            assert response.json()["status"] == "pending"
            status_url = f"{url}/{job_id}"
            assert (await client.get(status_url)).status_code == 401
            assert (await client.get(status_url, headers=headers)).json() == {
                "job_id": job_id, "status": "pending", "attempts": 0,
            }

            rejected = await client.post(
                url, headers={"X-Admin-Token": "admin-secret"}, json=payload | {"text": "failed"},
            )
            assert rejected.status_code == 202
            failed_id = rejected.json()["job_id"]

            worker = asyncio.create_task(main.process_jobs(SimpleNamespace(send_message=send)))
            try:
                async with asyncio.timeout(5):
                    while True:
                        failed = (await client.get(f"{url}/{failed_id}", headers=headers)).json()
                        if failed["status"] == "failed":
                            break
                        if worker.done():
                            await worker
                        await asyncio.sleep(0.01)
                assert failed["attempts"] == 3
                assert (await client.get(status_url, headers=headers)).json() == {
                    "job_id": job_id, "status": "completed", "attempts": 2,
                }
                assert send.await_args_list == [
                    call(bot_id=bot_id, chat_id=chat_id, body="reply"),
                ]
                assert deliver.await_args_list == [
                    call(bot_id, chat_id, payload["text"], "https://cts.example.org", "bot-secret"),
                    call(bot_id, chat_id, payload["text"], "https://cts.example.org", "bot-secret"),
                    call(bot_id, chat_id, "failed", "https://cts.example.org", "bot-secret"),
                    call(bot_id, chat_id, "failed", "https://cts.example.org", "bot-secret"),
                    call(bot_id, chat_id, "failed", "https://cts.example.org", "bot-secret"),
                ]
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
    finally:
        await engine.dispose()


class FakeBotXClient:
    users: ClassVar[dict] = {}
    created: ClassVar[list] = []
    failure: ClassVar[Exception | None] = None
    personal: ClassVar[dict] = {}

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    async def find_user_by_email(self, email: str):
        if FakeBotXClient.failure:
            raise FakeBotXClient.failure
        return FakeBotXClient.users.get(email)

    async def create_chat(self, name: str, chat_type: str, members: list):
        if FakeBotXClient.failure:
            raise FakeBotXClient.failure
        chat_id = uuid4()
        FakeBotXClient.created.append((name, chat_type, tuple(members), chat_id))
        return chat_id

    async def find_personal_chat(self, user_huid):
        return self.personal.get(user_huid)


async def test_notification_can_target_a_user(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main.settings, "notification_token", "notification-secret")
    monkeypatch.setattr(main, "BotXClient", FakeBotXClient)
    monkeypatch.setattr(chats, "BotXClient", FakeBotXClient)

    bot_id, user_huid, other_huid = uuid4(), uuid4(), uuid4()
    FakeBotXClient.users = {"user@example.org": {"user_huid": str(user_huid)}}
    headers = {"X-API-Key": "notification-secret"}
    url = "/api/v1/messages"

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(Bot(id=bot_id, name="test", cts_url="https://cts.example.org", secret_key="bot-secret"))

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            both = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "email": "user@example.org",
                "user_huid": str(user_huid), "text": "hi",
            })
            assert both.status_code == 422

            unknown = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "email": "missing@example.org", "text": "hi",
            })
            assert unknown.status_code == 404
            assert FakeBotXClient.created == []

            FakeBotXClient.failure = BotXConnectionError("CTS is down")
            broken = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "email": "user@example.org", "text": "hi",
            })
            assert broken.status_code == 502
            FakeBotXClient.failure = None

            delivered = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "email": "user@example.org", "text": "First",
            })
            assert delivered.status_code == 202, delivered.text
            assert delivered.json()["user_huid"] == str(user_huid)
            created_chat_id = delivered.json()["chat_id"]
            assert FakeBotXClient.created == [
                ("Personal chat", "chat", (user_huid,), UUID(created_chat_id)),
            ]

            async with sessions() as session:
                job = await session.scalar(select(Job).where(Job.id == delivered.json()["job_id"]))
                assert job.chat_id == UUID(created_chat_id)
                assert job.payload == {"text": "First", "kind": "notification", "user_huid": str(user_huid)}
                chat = await session.scalar(
                    select(Chat).where(Chat.bot_id == bot_id, Chat.chat_id == UUID(created_chat_id))
                )
                assert chat.chat_type == "personal"
                assert chat.user_huid == user_huid

            again = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "email": "user@example.org", "text": "Second",
            })
            assert again.status_code == 202
            assert again.json()["chat_id"] == created_chat_id
            assert len(FakeBotXClient.created) == 1

            by_huid = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "user_huid": str(other_huid), "text": "Direct",
            })
            assert by_huid.status_code == 202
            assert by_huid.json()["user_huid"] == str(other_huid)
            assert by_huid.json()["chat_id"] != created_chat_id
            assert len(FakeBotXClient.created) == 2

            other_existing_user, existing_chat = uuid4(), uuid4()
            monkeypatch.setattr(FakeBotXClient, "personal", {other_existing_user: existing_chat})
            found = await client.post(url, headers=headers, json={
                "bot_id": str(bot_id), "user_huid": str(other_existing_user), "text": "Reuse",
            })
            assert found.status_code == 202
            assert found.json()["chat_id"] == str(existing_chat)
            assert len(FakeBotXClient.created) == 2
    finally:
        FakeBotXClient.users, FakeBotXClient.created, FakeBotXClient.failure = {}, [], None
        await engine.dispose()


async def test_user_targeted_notifications_respect_route_allowlists(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main, "BotXClient", FakeBotXClient)
    monkeypatch.setattr(chats, "BotXClient", FakeBotXClient)

    bot_id, user_huid = uuid4(), uuid4()
    FakeBotXClient.users = {"user@example.org": {"user_huid": str(user_huid)}}
    integration_token = "ntf_integration-secret"

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(Bot(id=bot_id, name="test", cts_url="https://cts.example.org", secret_key="bot-secret"))
            await session.flush()
            integration = NotificationIntegration(name="Ops", description="Ops alerts")
            session.add(integration)
            await session.flush()
            session.add(NotificationToken(
                integration_id=integration.id, bot_id=bot_id,
                digest=main.notification_token_digest(integration_token),
                prefix="ntf_inte",
            ))
            session.add(NotificationRoute(
                integration_id=integration.id, bot_id=bot_id, chat_ids=[],
            ))

        headers = {"X-API-Key": integration_token}
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {"email": "user@example.org", "text": "Allowed"}
            allowed = await client.post("/api/v1/messages", headers=headers, json=payload)
            assert allowed.status_code == 202, allowed.text
            status_url = f"/api/v1/messages/{allowed.json()['job_id']}"
            status = await client.get(status_url, headers=headers)
            assert status.status_code == 200
            assert status.json()["status"] == "pending"
            legacy_headers = {"X-Notification-Token": integration_token}
            assert (await client.get(status_url, headers=legacy_headers)).status_code == 200
            assert (await client.post("/api/v1/messages", headers=legacy_headers, json=payload)).status_code == 202
            assert (await client.get(status_url, headers=legacy_headers | {"X-API-Key": "wrong"})).status_code == 401
            mismatch = await client.post("/api/v1/messages", headers=headers, json={
                "bot_id": str(uuid4()), "email": "user@example.org", "text": "Denied",
            })
            assert mismatch.status_code == 403

        async with sessions.begin() as session:
            route = await session.get(NotificationRoute, (integration.id, bot_id))
            route.chat_ids = [str(uuid4())]

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.post("/api/v1/messages", headers=headers, json={
                "bot_id": str(bot_id), "email": "user@example.org", "text": "Denied",
            })
            assert denied.status_code == 403
            assert len(FakeBotXClient.created) == 1

            hidden = await client.get(f"/api/v1/messages/{allowed.json()['job_id']}", headers=headers)
            assert hidden.status_code == 404
    finally:
        FakeBotXClient.users, FakeBotXClient.created, FakeBotXClient.failure = {}, [], None
        await engine.dispose()


async def test_personal_notifications_reuse_inbound_chat_until_cts_removes_it(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chats.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main.settings, "notification_token", "notification-secret")
    monkeypatch.setattr(chats, "BotXClient", FakeBotXClient)
    monkeypatch.setattr(FakeBotXClient, "created", [])
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(FakeBotXClient, "find_personal_chat", lookup)
    monkeypatch.setattr(FakeBotXClient, "list_chats", AsyncMock(return_value=[]), raising=False)
    bot_id, user_huid, chat_id = uuid4(), uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            bot = Bot(id=bot_id, name="BotX", cts_url="https://cts.test", secret_key="bot-secret")
            handler = InternalBot(name="Handler", botx_id=bot_id)
            session.add_all([bot, handler])
            await session.flush()
            session.add(UserGrant(internal_bot_id=handler.id, user_id=str(user_huid)))

        await incoming_message(SimpleNamespace(
            bot=SimpleNamespace(id=bot_id), chat=SimpleNamespace(id=chat_id, type="ChatTypes.PERSONAL_CHAT"),
            sender=SimpleNamespace(huid=user_huid), sync_id=uuid4(), body="Входящее сообщение",
            mentions=SimpleNamespace(contacts=[]), reply=None,
        ), None)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            async def notify():
                response = await client.post("/api/v1/notifications", headers={
                    "X-API-Key": "notification-secret",
                }, json={"bot_id": str(bot_id), "user_huid": str(user_huid), "text": "Уведомление"})
                assert response.status_code == 202, response.text
                return UUID(response.json()["chat_id"])

            assert await notify() == chat_id
            lookup.assert_not_awaited()
            await chats.reconcile_chats(sessions, bot)
            new_chat_id = await notify()
            assert new_chat_id != chat_id and len(FakeBotXClient.created) == 1
            lookup.assert_awaited_once_with(user_huid)

        async with sessions() as session:
            records = {chat.chat_id: chat for chat in await session.scalars(select(Chat))}
            assert not records[chat_id].available and records[new_chat_id].available
            assert records[chat_id].user_huid == records[new_chat_id].user_huid == user_huid
    finally:
        await engine.dispose()
