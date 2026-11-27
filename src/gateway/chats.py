"""Local BotX chat addresses and their availability on CTS."""
import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import or_, select, text, update

from .botx_client import BotXClient, BotXResponseError
from .config import settings
from .models import Bot, Chat

logger = logging.getLogger(__name__)
PERSONAL_TYPES = {"chat", "personal", "personal_chat", "chattypes.personal_chat"}


async def lock_chat(session, bot_id: UUID, address: UUID) -> None:
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:address, 0))"),
            {"address": f"botx-chat:{bot_id}:{address}"},
        )


async def active_personal_chat(session, bot_id: UUID, user_huid: UUID) -> Chat | None:
    return await session.scalar(select(Chat).where(
        Chat.bot_id == bot_id, Chat.user_huid == user_huid,
        Chat.chat_type == "personal", Chat.available.is_(True),
    ))


async def record_chat(
    session, bot_id: UUID, chat_id: UUID, chat_type: str, *,
    user_huid: UUID | None = None, history_enabled: bool = True,
) -> Chat:
    if user_huid is not None:
        previous = await active_personal_chat(session, bot_id, user_huid)
        if previous is not None and previous.chat_id != chat_id:
            previous.available = False
            await session.flush()
    chat = await session.scalar(select(Chat).where(Chat.bot_id == bot_id, Chat.chat_id == chat_id))
    if chat is None:
        chat = Chat(bot_id=bot_id, chat_id=chat_id, history_enabled=history_enabled)
        session.add(chat)
    chat.chat_type = chat_type
    chat.user_huid = user_huid
    chat.available = True
    chat.seen_at = datetime.now(timezone.utc)
    await session.flush()
    return chat


async def personal_chat_for_user(session, bot: Bot, user_huid: UUID) -> UUID:
    await lock_chat(session, bot.id, user_huid)
    chat = await active_personal_chat(session, bot.id, user_huid)
    if chat is not None:
        return chat.chat_id
    async with BotXClient(bot_id=bot.id, cts_url=bot.cts_url, secret_key=bot.secret_key) as client:
        chat_id = await client.find_personal_chat(user_huid)
        if chat_id is None:
            chat_id = await client.create_chat("Personal chat", "chat", [user_huid])
    await record_chat(session, bot.id, chat_id, "personal", user_huid=user_huid, history_enabled=False)
    return chat_id


async def reconcile_chats(session_factory, bot: Bot) -> None:
    checked_at = datetime.now(timezone.utc)
    async with BotXClient(bot_id=bot.id, cts_url=bot.cts_url, secret_key=bot.secret_key) as client:
        remote = await client.list_chats()
    try:
        available = {UUID(str(chat["group_chat_id"])) for chat in remote}
    except (KeyError, TypeError, ValueError) as exc:
        raise BotXResponseError("BotX returned a chat list with invalid chat IDs") from exc
    async with session_factory.begin() as session:
        current = await session.get(Bot, bot.id, with_for_update=True)
        if not current or not current.enabled or current.credential_revision != bot.credential_revision:
            return
        known = (await session.execute(select(Chat.chat_id, Chat.chat_type, Chat.available).where(
            Chat.bot_id == bot.id,
        ))).all()
        for chat_id, chat_type, was_available in known:
            is_available = chat_id in available
            if is_available == was_available or (is_available and chat_type == "personal"):
                continue
            await session.execute(update(Chat).where(
                Chat.bot_id == bot.id, Chat.chat_id == chat_id,
                or_(Chat.seen_at.is_(None), Chat.seen_at < checked_at),
            ).values(available=is_available, seen_at=checked_at))


async def reconcile_bot_chats_periodically(session_factory) -> None:
    while True:
        await asyncio.sleep(settings.chat_reconcile_seconds)
        try:
            async with session_factory() as session:
                bots = (await session.scalars(select(Bot).where(
                    Bot.enabled.is_(True), Bot.cts_url.is_not(None), Bot.secret_key.is_not(None),
                    select(Chat.id).where(Chat.bot_id == Bot.id).exists(),
                ))).all()
            for bot in bots:
                try:
                    await reconcile_chats(session_factory, bot)
                except Exception:
                    logger.exception("Chat reconciliation failed for BotX %s", bot.id)
        except Exception:
            logger.exception("Chat reconciliation could not load BotX connections")
