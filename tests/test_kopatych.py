import asyncio
import json
from contextlib import suppress
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from gateway import main
from gateway import kopatych
from gateway.kopatych import _poll_run
from gateway.models import AssignmentEvent, Base, Bot, InternalBot, Job, KopatychRun, NotificationIntegration


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest.mark.asyncio
async def test_kopatych_round_trip_and_ack_recovery(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kopatych.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main.settings, "kopatych_connector_id", "express")
    monkeypatch.setattr(main.settings, "kopatych_delivery_mode", "poll")
    monkeypatch.setattr(main.settings, "admin_token", "journal-test-admin")

    bot_id, chat_id, sender, incoming, portal_run = [uuid4() for _ in range(5)]
    async def lookup_mail(request):
        assert request.user_id == str(sender)
        return "ldap-user@example.invalid"

    monkeypatch.setattr(kopatych, "_resolve_sender_mail", lookup_mail)
    async with sessions.begin() as session:
        system = NotificationIntegration(
            name="Portal", description="", outbound_url="https://portal.test/api/v1/integrations/durable-runs",
            outbound_auth_type="bearer", outbound_token="fixture-connector-token",
        )
        session.add_all([system, Bot(id=bot_id, name="Копатыч", enabled=True)])
        await session.flush()
        internal = InternalBot(
            name="Копатыч", template_id="kopatych", botx_id=bot_id, external_system_id=system.id,
        )
        session.add(internal)

    base = "/api/v1/integrations/assignments"
    pause = str(uuid4())
    events = [
        {"sequence": 1, "event_id": str(uuid4()), "kind": "accepted", "payload": {}},
        {"sequence": 2, "event_id": str(uuid4()), "kind": "waiting_confirmation",
         "payload": {"content": "Выполнить проверку?", "pause_id": pause,
                     "interaction": {"response_schema": {"type": "boolean"}}}},
        {"sequence": 3, "event_id": str(uuid4()), "kind": "waiting_timer",
         "payload": {"content": "Привет 1", "pause_id": str(uuid4())}},
        {"sequence": 4, "event_id": str(uuid4()), "kind": "waiting_timer",
         "payload": {"content": "Привет 2", "pause_id": str(uuid4())}},
        {"sequence": 5, "event_id": str(uuid4()), "kind": "completed",
         "payload": {"content": "Привет 3"}},
    ]
    requests = []
    phase = 2
    break_ack = True

    def mock_portal(req: httpx.Request):
        nonlocal break_ack
        assert req.headers["authorization"] == "Bearer fixture-connector-token"
        assert req.headers["x-assignment-connector"] == "clouds:express"
        path = req.url.path
        body = json.loads(req.content) if req.content else None
        requests.append((req.method, path, body))
        if path == base:
            assert body["reply_to"]["data"]["message_id"] == str(incoming)
            assert body["reply_to"]["data"]["chat_id"] == str(chat_id)
            assert body["user"]["clouds"] == str(sender)
            if not body["user"].get("mail"):
                return httpx.Response(403, json={"detail": {"code": "identity_not_linked"}})
            assert body["user"]["mail"] == "ldap-user@example.invalid"
            return httpx.Response(202, json={"run_id": str(portal_run)})
        if path.endswith("/events"):
            cursor = int(req.url.params["after_sequence"])
            return httpx.Response(200, json={
                "items": events[cursor:phase], "run_id": str(portal_run),
                "user": {"clouds": str(sender)},
                "reply_to": kopatych._destination(bot_id, chat_id, incoming),
            })
        if path.endswith("/ack"):
            if break_ack:
                break_ack = False
                return httpx.Response(503)
            assert body["external_message_id"]
            return httpx.Response(204)
        if path.endswith("/responses"):
            assert body["value"] is False and body["pause_id"] == pause
            assert body["user"] == {"clouds": str(sender)}
            return httpx.Response(200, json={"status": "queued"})
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelled"})
        raise AssertionError(path)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(mock_portal), **kw,
    ))

    class BotX:
        def __init__(self):
            self.sent = []

        async def reply_message(self, *, bot_id, sync_id, body, metadata):
            assert sync_id == incoming
            assert metadata["portal_event_id"]
            message_id = uuid4()
            self.sent.append((message_id, body))
            return message_id

    client = BotX()

    def payload(text="проверь", *, message_id=incoming, user=sender, mention=False, reply=None):
        return {
            "is_group": True, "is_mentioned": mention, "is_reply_to_bot": reply is not None,
            "text": text, "message_id": str(message_id), "sender_id": str(user),
            "reply": {"message_id": str(reply)} if reply else None,
        }

    try:
        assert await main.dispatch_queued_message(internal, bot_id, chat_id, payload()) == ""
        assert not requests
        specific = payload(mention=True)
        assert await main.dispatch_queued_message(internal, bot_id, chat_id, specific) == ""
        assert await main.dispatch_queued_message(internal, bot_id, chat_id, specific) == ""
        assert sum(path == base for _, path, _ in requests) == 2
        async with sessions() as session:
            run = await session.scalar(select(KopatychRun))
            request_id = run.request_id
            assert request_id != portal_run and run.run_id == portal_run
        with pytest.raises(httpx.HTTPStatusError):
            await _poll_run(sessions, client, request_id)
        async with sessions() as session:
            row = await session.get(KopatychRun, request_id)
            assert row.cursor == 1 and row.pending_ack_id
        await _poll_run(sessions, client, request_id)
        assert len(client.sent) == 2
        assert "да" in client.sent[-1][1]
        question = client.sent[-1][0]
        wrong = await main.dispatch_queued_message(internal, bot_id, chat_id, payload(
            "нет", message_id=uuid4(), user=uuid4(), reply=question,
        ))
        assert "не найдено" in wrong
        assert not any(path.endswith("/responses") for _, path, _ in requests)
        await main.dispatch_queued_message(internal, bot_id, chat_id, payload(
            "нет", message_id=uuid4(), reply=question,
        ))
        assert sum(path.endswith("/responses") for _, path, _ in requests) == 1
        for phase in (3, 4, 5):
            await _poll_run(sessions, client, request_id)
            assert len(client.sent) == phase
            assert client.sent[-1][1] == f"Привет {phase - 2}"
        async with client_class(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test",
            headers={"X-Admin-Token": "journal-test-admin"},
        ) as admin:
            for url in (
                f"/api/v1/admin/botx/{bot_id}/logs",
                f"/api/v1/admin/notification-integrations/{system.id}/logs",
            ):
                response = await admin.get(url)
                assert response.status_code == 200, response.text
                latest = response.json()["entries"][:3]
                assert [row["text"] for row in latest] == ["Привет 3", "Привет 2", "Привет 1"]
                assert all(row["status"] == "completed" and row["delivered_at"] for row in latest)
        async with sessions() as session:
            row = await session.scalar(select(KopatychRun))
            assert row.terminal and row.cursor == 5 and not row.pending_ack_id
    finally:
        await engine.dispose()


async def test_assignment_callback_routes_only_through_the_durable_binding(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'push.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", sessions)
    monkeypatch.setattr(main.settings, "kopatych_connector_id", "express")
    monkeypatch.setattr(main.settings, "kopatych_delivery_mode", "push")
    monkeypatch.setattr(main.settings, "worker_poll_seconds", 0.01)
    monkeypatch.setattr(main.settings, "admin_token", "admin-secret")
    bot_id, chat_id, sender, incoming, external_run = [uuid4() for _ in range(5)]

    async def admit(_client, payload):
        async with sessions() as session:
            local = await session.scalar(select(KopatychRun))
            assert local.request_id and local.run_id is None
            assert local.status == "pending" and local.chat_id == chat_id and local.sender_id == str(sender)
            assert payload["event_id"] == str(local.request_id)
            assert payload["reply_to"]["data"]["message_id"] == str(incoming)
        return kopatych.PortalRun(run_id=external_run)

    monkeypatch.setattr(kopatych.PortalClient, "admit", admit)
    respond = AsyncMock()
    monkeypatch.setattr(kopatych.PortalClient, "reply", respond)

    class BotX:
        def __init__(self):
            self.sent = []

        async def reply_message(self, *, bot_id, sync_id, body, metadata):
            self.sent.append((bot_id, sync_id, body))
            return uuid4()

    worker = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            system = NotificationIntegration(
                name="Portal", description="", outbound_url="https://portal.test/api/v1/integrations/assignments",
                outbound_auth_type="bearer", outbound_token="portal-token",
            )
            other_system = NotificationIntegration(name="Other", description="")
            session.add_all([system, other_system, Bot(id=bot_id, name="Копатыч")])
            await session.flush()
            internal = InternalBot(
                name="Копатыч", template_id="kopatych", botx_id=bot_id, external_system_id=system.id,
            )
            session.add(internal)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            async def issue_token(system_id, purpose):
                response = await client.post(
                    f"/api/v1/admin/notification-integrations/{system_id}/tokens",
                    headers={"X-Admin-Token": "admin-secret"}, json={"purpose": purpose},
                )
                assert response.status_code == 201, response.text
                return response.json()["token"]

            secret = await issue_token(system.id, "assignment_events")
            unrelated = await issue_token(other_system.id, "assignment_events")
            notification_secret = await issue_token(system.id, "notification")
            final = {
                "run_id": str(external_run), "event_id": str(uuid4()), "sequence": 2,
                "kind": "final", "status": "completed", "result": {"text": "Готово."},
            }
            pause_id = uuid4()
            question = final | {
                "event_id": str(uuid4()), "sequence": 1, "kind": "question", "status": "waiting_confirmation",
                "result": {"text": "Продолжить?", "pause_id": str(pause_id), "response_schema": {"type": "boolean"}},
            }

            async def callback(body, token=secret):
                return await client.post("/api/v1/integrations/assignments/events", json=body, headers={
                    "X-API-Key": token, "X-Assignment-Connector": "clouds:express",
                })

            assert (await callback(final)).json()["detail"]["code"] == "binding_pending"
            legacy_headers = {"Authorization": f"Bearer {secret}", "X-Assignment-Connector": "clouds:express"}
            legacy = await client.post("/api/v1/integrations/assignments/events", json=final, headers=legacy_headers)
            assert legacy.json()["detail"]["code"] == "binding_pending"
            invalid = await client.post(
                "/api/v1/integrations/assignments/events", json=final, headers=legacy_headers | {"X-API-Key": "wrong"},
            )
            assert invalid.status_code == 401
            await main.dispatch_queued_message(internal, bot_id, chat_id, {
                "is_group": True, "is_mentioned": True, "text": "Проверь",
                "message_id": str(incoming), "sender_id": str(sender),
            })
            assert (await callback(final, notification_secret)).status_code == 401
            assert (await callback(final, unrelated)).json()["detail"]["code"] == "binding_pending"
            assert (await callback(final)).json()["detail"] == {
                "code": "sequence_gap", "expected_sequence": 1,
            }
            assert (await callback(question)).status_code == 202
            bot_client = BotX()
            worker = asyncio.create_task(main.process_jobs(bot_client))
            async with asyncio.timeout(5):
                while True:
                    async with sessions() as session:
                        row = (await session.scalars(select(KopatychRun))).one()
                        if row.question_message_id is not None:
                            question_message_id = row.question_message_id
                            break
                    await asyncio.sleep(0.01)
            response_message_id = str(uuid4())
            await main.dispatch_queued_message(internal, bot_id, chat_id, {
                "is_group": True, "is_reply_to_bot": True, "text": "да",
                "message_id": response_message_id, "sender_id": str(sender),
                "reply": {"message_id": str(question_message_id)},
            })
            respond.assert_awaited_once_with(external_run, pause_id, response_message_id, True, str(sender))
            assert (await callback(final)).status_code == 202
            assert (await callback(final)).status_code == 202
            assert (await callback(final | {"result": {"text": "Подмена"}})).json()["detail"]["code"] == "event_conflict"
            assert (await callback(final | {"event_id": str(uuid4()), "sequence": 3})).json()["detail"]["code"] == "assignment_closed"

        async with asyncio.timeout(5):
            while True:
                async with sessions() as session:
                    jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
                    if [job.status for job in jobs] == ["completed", "completed"]:
                        event = (await session.scalars(select(AssignmentEvent).where(AssignmentEvent.sequence == 2))).one()
                        run = await session.get(KopatychRun, event.request_id)
                        assert event.delivered_message_id == run.last_message_id
                        assert event.request_id != run.run_id and run.status == "completed"
                        break
                await asyncio.sleep(0.01)
        assert bot_client.sent == [
            (bot_id, incoming, "Продолжить?\nОтветьте «да» или «нет» на это сообщение."),
            (bot_id, incoming, "Готово."),
        ]
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await engine.dispose()
