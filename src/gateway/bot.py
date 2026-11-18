"""pybotx BotX ingress with a database-refreshed local credential snapshot."""
from threading import RLock
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

from pybotx import (
    Bot,
    BotAccountWithSecret,
    HandlerCollector,
    IncomingMessage,
    BotAccountKey,
    BotAccountProvider,
    UnknownBotAccountError,
)
from pybotx.bot.exceptions import AmbiguousBotAccountError
from sqlalchemy import select

collector = HandlerCollector()


class RuntimeBotAccountProvider(BotAccountProvider):
    """Synchronous BotX credential lookup backed by a replaceable local snapshot."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._accounts: dict[BotAccountKey, BotAccountWithSecret] = {}

    def get_account(self, key: BotAccountKey) -> BotAccountWithSecret:
        with self._lock:
            account = self._accounts.get(key)
        if account is None:
            raise UnknownBotAccountError(key.bot_id)
        return account

    def get_account_by_bot_id(self, bot_id: uuid.UUID) -> BotAccountWithSecret:
        with self._lock:
            matches = [account for key, account in self._accounts.items() if key.bot_id == bot_id]
        if not matches:
            raise UnknownBotAccountError(bot_id)
        if len(matches) > 1:
            raise AmbiguousBotAccountError(bot_id)
        return matches[0]

    def resolve_incoming_account_key(
        self, *, bot_id: uuid.UUID, host: str | None
    ) -> BotAccountKey:
        incoming_host = None
        if host:
            parsed = urlsplit(host if "://" in host else f"//{host}")
            incoming_host = (parsed.hostname or host).casefold()
        with self._lock:
            matches = [
                key for key, account in self._accounts.items()
                if key.bot_id == bot_id and (
                    incoming_host is None or account.host.casefold() == incoming_host
                )
            ]
        if not matches:
            raise UnknownBotAccountError(bot_id)
        if len(matches) > 1:
            raise AmbiguousBotAccountError(bot_id)
        return matches[0]

    def iter_bot_accounts(self) -> Iterator[BotAccountWithSecret]:
        with self._lock:
            return iter(tuple(self._accounts.values()))

    def replace_all(self, accounts: list[BotAccountWithSecret]) -> None:
        with self._lock:
            self._accounts = {account.account_key: account for account in accounts if account.account_key}

    def upsert(self, account: BotAccountWithSecret) -> BotAccountKey:
        key = account.account_key
        if key is None:
            host = account.host.casefold()
            key = BotAccountKey(server_id=host, bot_id=account.id)
            account = account.model_copy(update={"account_key": key})
        with self._lock:
            self._accounts[key] = account
        return key


account_provider = RuntimeBotAccountProvider()


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
    from .models import Bot as BotRecord, Chat, Message, InboundEvent, Job, UserGrant
    async with Session() as session:
        bot_record = await session.get(BotRecord, bot_id)
        if bot_record is None or not bot_record.enabled:
            return
        if not is_group:
            grant_id = await session.scalar(
                select(UserGrant.id)
                .where(
                    UserGrant.bot_id == bot_id,
                    UserGrant.user_id == user_id,
                    UserGrant.permission == "use",
                )
                .limit(1)
            )
            if grant_id is None:
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
            session.add(Job(bot_id=bot_id, chat_id=chat_id, payload={"kind": "message", "text": body,
                        "message_id": message_id, "sender_id": user_id, "command": command,
                        "is_group": is_group}))
            await session.commit()


def make_bot() -> Bot:
    return Bot(collectors=[collector], account_provider=account_provider)


bot = make_bot()
