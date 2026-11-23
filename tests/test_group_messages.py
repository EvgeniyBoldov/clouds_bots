import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from gateway.integrations import HandlerRequest
from gateway.templates import BaseBot, command


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


def test_group_history_and_routing(monkeypatch):
    from gateway import main
    from gateway.bot import incoming_message
    from gateway.models import Base, Bot, Chat, InboundEvent, InternalBot, Job, Message

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(main, "Session", sessions)
        bot_id, chat_id = uuid.uuid4(), uuid.uuid4()
        async with sessions.begin() as session:
            session.add(Bot(id=bot_id, name="test", enabled=True))
            await session.flush()
            session.add(InternalBot(botx_id=bot_id, name="test handler"))
        def message(body, mention=False, reply=False):
            return SimpleNamespace(bot=SimpleNamespace(id=bot_id), chat=SimpleNamespace(id=chat_id, type="group_chat"),
                sender=SimpleNamespace(huid=uuid.uuid4()), sync_id=uuid.uuid4(), body=body,
                mentions=SimpleNamespace(contacts=[SimpleNamespace(entity_id=bot_id)] if mention else []),
                reply=SimpleNamespace(author_id=bot_id, sync_id=uuid.uuid4(), body="bot message") if reply else None)
        events = [message("ordinary"), message("/ping"), message("hello", mention=True), message("answer", reply=True)]
        for event in events:
            await incoming_message(event, None)
        await incoming_message(events[0], None)
        async with sessions() as session:
            assert len((await session.scalars(select(Chat))).all()) == 1
            assert len((await session.scalars(select(Message))).all()) == 4
            assert len((await session.scalars(select(InboundEvent))).all()) == 4
            jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
            assert len(jobs) == 4
            assert jobs[1].payload["command"] == "/ping"
            assert jobs[2].payload["is_mentioned"]
            assert jobs[3].payload["is_reply_to_bot"]
        await engine.dispose()
    asyncio.run(exercise())


def test_template_group_hooks():
    class Example(BaseBot):
        @command("/go")
        async def go(self, request): return "command"
        async def on_mention(self, request): return "mention"
        async def on_reply(self, request): return "reply"
        async def on_group_message(self, request): return "all"
    def run(text, metadata=None):
        request = HandlerRequest("b", "c", "group", "u", "m", text, metadata=metadata or {})
        return asyncio.run(Example().dispatch(request)).text
    assert run("/go") == "command"
    assert run("hello", {"is_mentioned": True}) == "mention"
    assert run("hello", {"is_reply_to_bot": True}) == "reply"
    assert run("hello") == "all"
    assert asyncio.run(BaseBot().dispatch(HandlerRequest("b", "c", "group", "u", "m", "hello"))).text is None
