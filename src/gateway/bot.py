"""pybotx BotX ingress. Accounts are loaded once from protected environment config."""
import json
import uuid

from pybotx import (
    Bot,
    BotAccountWithSecret,
    HandlerCollector,
    IncomingMessage,
)

from .config import settings
from sqlalchemy import select

collector = HandlerCollector()


def _identifier(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


@collector.default_message_handler
async def incoming_message(message: IncomingMessage, bot: Bot) -> None:
    """Persist every ordinary message; reply to DMs and structured bot mentions in groups."""
    bot_id = _identifier(message.bot.id)
    chat_id = _identifier(message.chat.id)
    user_id = str(message.sender.huid)
    message_id = str(message.sync_id)
    body = message.body
    # IncomingMessage exposes structured mention entities; never infer mentions from plain text.
    mentions = message.mentions
    is_mentioned = any(str(mention.entity_id) == str(bot_id) for mention in getattr(mentions, "contacts", ()))
    chat_type = str(getattr(message.chat, "type", "unknown")).lower()
    is_group = "group" in chat_type
    from .main import Session
    from .models import Bot as BotRecord, Chat, Message, InboundEvent, Job
    async with Session() as session:
        bot_record = await session.get(BotRecord, bot_id)
        if bot_record is None or not bot_record.enabled:
            return
        chat = await session.scalar(select(Chat).where(Chat.bot_id == bot_id, Chat.chat_id == chat_id))
        if chat is None:
            chat = Chat(bot_id=bot_id, chat_id=chat_id, chat_type=chat_type, history_enabled=True)
            session.add(chat)
            await session.flush()
        prior = await session.scalar(select(InboundEvent).where(InboundEvent.bot_id == bot_id, InboundEvent.message_id == message_id))
        if prior:
            return
        session.add(InboundEvent(bot_id=bot_id, message_id=message_id, chat_id=chat_id,
                                 payload={"sender_id": user_id, "body": body, "chat_type": chat_type,
                                          "is_group": is_group, "is_mentioned": is_mentioned}))
        if chat.history_enabled:
            session.add(Message(bot_id=bot_id, chat_id=chat_id, message_id=message_id,
                                sender_id=user_id, direction="incoming", body=body,
                                metadata_json={"chat_type": chat_type}))
        await session.commit()
        command = body.split(maxsplit=1)[0].lower() if body.startswith("/") else ""
        if not is_group or is_mentioned or command:
            kind = "summary" if command == "/summary" else "message"
            session.add(Job(bot_id=bot_id, chat_id=chat_id, payload={"kind": kind, "text": body,
                        "message_id": message_id, "sender_id": user_id, "command": command,
                        "is_group": is_group}))
            await session.commit()


def make_bot() -> Bot:
    accounts = []
    for item in json.loads(settings.bot_accounts_json):
        if item.get("enabled", True):
            accounts.append(BotAccountWithSecret(
                id=uuid.UUID(item["bot_id"]),
                cts_url=item["cts_url"],
                secret_key=item["secret_key"],
            ))
    return Bot(collectors=[collector], bot_accounts=accounts)


bot = make_bot()
