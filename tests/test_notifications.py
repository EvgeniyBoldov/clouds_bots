import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from uuid import uuid4

import httpx
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from gateway import main
from gateway.models import Base, Bot, Job


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
    headers = {"X-Notification-Token": "notification-secret"}
    url = "/api/v1/notifications"

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add_all([
                Bot(id=bot_id, name="test", cts_url="https://cts.example.org", secret_key="bot-secret"),
                Bot(id=disabled_id, name="disabled", enabled=False),
            ])
            incoming = Job(bot_id=bot_id, chat_id=chat_id, payload={"kind": "message"})
            session.add(incoming)
        monkeypatch.setattr(main, "dispatch_queued_message", AsyncMock(return_value="reply"))
        send = AsyncMock(side_effect=[None, RuntimeError("BotX unavailable"), None] + [
            RuntimeError("BotX unavailable") for _ in range(3)
        ])

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
                    *[call(bot_id=bot_id, chat_id=chat_id, body=payload["text"])] * 2,
                    *[call(bot_id=bot_id, chat_id=chat_id, body="failed")] * 3,
                ]
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
    finally:
        await engine.dispose()
