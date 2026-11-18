from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Index, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func
import uuid


class Base(DeclarativeBase): pass


class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_handler: Mapped[str] = mapped_column(String(100), default="echo")
    template_id: Mapped[str] = mapped_column(String(64), default="echo", server_default="echo")
    cts_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id"))
    chat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    chat_type: Mapped[str] = mapped_column(String(32), default="group")
    history_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    handler_name: Mapped[str | None] = mapped_column(String(100))
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("bot_id", "chat_id"),)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    message_id: Mapped[str] = mapped_column(String(200))
    sender_id: Mapped[str] = mapped_column(String(200))
    direction: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(64), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    __table_args__ = (UniqueConstraint("bot_id", "message_id", "direction"), Index("ix_messages_history", "bot_id", "chat_id", "created_at"))


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    message_id: Mapped[str] = mapped_column(String(200))
    chat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("bot_id", "message_id"),)


class UserGrant(Base):
    __tablename__ = "user_grants"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id"))
    user_id: Mapped[str] = mapped_column(String(200))
    identity_type: Mapped[str] = mapped_column(String(16), default="huid", server_default="huid")
    identity_value: Mapped[str | None] = mapped_column(String(320), nullable=True)
    ad_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    permission: Mapped[str] = mapped_column(String(100), default="use")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("bot_id", "user_id", "permission"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    chat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
