import json
import uuid
import asyncio
import hmac
import hashlib
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import Response
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from pybotx import FastAPIBotAppConfig, setup_fastapi_bot
from pybotx import (
    BotAccountKey, BotAccountWithSecret, InvalidBotAccountError, PermissionDeniedError,
    RateLimitReachedError, UnknownBotAccountError, UserNotFoundError,
)
from pybotx.client.exceptions.base import BaseClientError

from .config import settings
from .models import (
    Base, Bot, Chat, InternalBot, Job, Message, NotificationIntegration, NotificationRoute,
    NotificationToken, UserGrant,
)
from .integrations import EchoHandler, ExternalAuthType, ExternalSystem, HttpHandler, HandlerRequest
from .templates import get_template, template_catalog

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


app = FastAPI(title="eXpress Bot Gateway")
from .bot import bot, account_provider
setup_fastapi_bot(app, bot=bot, config=FastAPIBotAppConfig(metrics_path=None))


@app.on_event("startup")
async def startup():
    async with Session() as session:
        for item in json.loads(settings.bot_accounts_json):
            ident = uuid.UUID(item["bot_id"])
            record = await session.get(Bot, ident)
            if not record:
                record = Bot(
                    id=ident,
                    name=item.get("name", str(ident)),
                    enabled=item.get("enabled", True),
                    default_handler=item.get("default_handler", "echo"),
                    cts_url=item.get("cts_url"),
                    secret_key=item.get("secret_key"),
                )
                session.add(record)
                await session.flush()
                session.add(InternalBot(
                    id=record.id, botx_id=record.id, name=record.name,
                    template_id=record.template_id, default_handler=record.default_handler,
                ))
            elif not record.cts_url or not record.secret_key:
                record.cts_url = item.get("cts_url")
                record.secret_key = item.get("secret_key")
                record.name = item.get("name", record.name)
                record.enabled = item.get("enabled", record.enabled)
                record.default_handler = item.get("default_handler", record.default_handler)
        await session.commit()
        configured_bots = (await session.scalars(
            select(Bot).where(Bot.enabled.is_(True), Bot.cts_url.is_not(None), Bot.secret_key.is_not(None))
        )).all()
        accounts = []
        for record in configured_bots:
            account_key = BotAccountKey(
                server_id=BotAccountWithSecret(
                    id=record.id,
                    cts_url=record.cts_url,
                    secret_key=record.secret_key,
                ).host.casefold(),
                bot_id=record.id,
            )
            accounts.append(BotAccountWithSecret(
                id=record.id,
                cts_url=record.cts_url,
                secret_key=record.secret_key,
                account_key=account_key,
                revision=record.credential_revision,
            ))
        account_provider.replace_all(accounts)
    app.state.worker = asyncio.create_task(job_worker(bot))


@app.on_event("shutdown")
async def shutdown():
    worker = getattr(app.state, "worker", None)
    if worker:
        worker.cancel()
        try: await worker
        except asyncio.CancelledError: pass
    await engine.dispose()


def resolve_handler(name: str):
    configs = json.loads(settings.handler_configs_json)
    config = configs.get(name, {})
    if config.get("type") == "http":
        return HttpHandler(config["url"], config.get("token", ""), config.get("timeout", 20))
    return EchoHandler()


async def legacy_summary(bot_id: uuid.UUID, chat_id: uuid.UUID, payload: dict) -> str:
    """Compatibility path for /summary when the selected template does not override it."""
    if not payload.get("is_group"):
        return "Команда /summary доступна только в группе."
    args = payload["text"].split()
    span = timedelta(days=7) if len(args) > 1 and args[1].lower() == "7d" else timedelta(hours=24)
    since = datetime.now(timezone.utc) - span
    async with Session() as history_session:
        chat_record = await history_session.scalar(
            select(Chat).where(Chat.bot_id == bot_id, Chat.chat_id == chat_id)
        )
        if not chat_record or not chat_record.history_enabled:
            return "Хранение истории для этого чата выключено."
        messages = (await history_session.scalars(
            select(Message).where(
                Message.bot_id == bot_id,
                Message.chat_id == chat_id,
                Message.created_at >= since,
                Message.direction == "incoming",
            ).order_by(Message.created_at)
        )).all()
        if not messages:
            return "За выбранный период история отсутствует."
        handler = resolve_handler(chat_record.handler_name or "summary")
        chunks, chunk = [], ""
        for row in messages:
            line = f"{row.sender_id}: {row.body}\n"
            if len(chunk) + len(line) > 5000 and chunk:
                chunks.append(chunk)
                chunk = ""
            chunk += line
        if chunk:
            chunks.append(chunk)
        summaries = []
        for index, part in enumerate(chunks):
            result = await handler.handle(HandlerRequest(
                str(bot_id), str(chat_id), "group", payload["sender_id"],
                payload["message_id"], "Составь краткое содержание переписки.",
                command="/summary", context=[{"part": index + 1, "text": part}],
            ))
            if result.status != "completed" or result.error:
                raise RuntimeError(result.error or "summary handler failed")
            if result.text:
                summaries.append(result.text)
        return "\n".join(summaries) or "Не удалось сформировать саммари."


async def dispatch_template_message(bot_record: InternalBot | Bot, request: HandlerRequest) -> str:
    registration = get_template(bot_record.template_id)
    if registration is None:
        return f"Шаблон бота «{bot_record.template_id}» недоступен. Выберите доступный шаблон в админке."

    template_class = registration.bot_class
    command_name = request.command.lower() if request.command else None
    if command_name == "/summary" and not template_class.has_command("/summary"):
        if isinstance(bot_record, InternalBot) and not bot_record.group_history_enabled:
            return "Хранение истории групп для этого бота выключено."
        payload = {
            "text": request.text,
            "sender_id": request.user_id,
            "message_id": request.message_id,
            "is_group": request.chat_type == "group",
        }
        return await legacy_summary(uuid.UUID(request.bot_id), uuid.UUID(request.chat_id), payload)

    if (
        registration.id == "echo"
        and bot_record.default_handler not in ("", "echo")
        and (request.chat_type != "group" or request.metadata.get("is_mentioned") or request.metadata.get("is_reply_to_bot") or command_name)
        and not template_class.has_command(command_name)
    ):
        result = await resolve_handler(bot_record.default_handler).handle(request)
    else:
        if isinstance(bot_record, InternalBot) and bot_record.external_system_id is not None:
            async with Session() as session:
                system = await session.get(NotificationIntegration, bot_record.external_system_id)
                if system and system.outbound_url:
                    request = replace(request, external_system=ExternalSystem(
                        system.id, system.name, system.outbound_url,
                        system.outbound_auth_type, system.outbound_token,
                    ))
        result = await template_class().dispatch(request)
    if result.status != "completed" or result.error:
        raise RuntimeError(result.error or "bot template handler failed")
    return result.text or ""


async def dispatch_queued_message(
    bot_record: InternalBot,
    bot_id: uuid.UUID,
    chat_id: uuid.UUID,
    payload: dict,
) -> str:
    command_name = payload.get("command") or (
        "/summary" if payload.get("kind") == "summary" else None
    )
    request = HandlerRequest(
        bot_id=str(bot_id),
        chat_id=str(chat_id),
        chat_type="group" if payload.get("is_group") else "personal",
        user_id=payload["sender_id"],
        message_id=payload["message_id"],
        text=payload["text"],
        command=command_name,
        metadata={"is_mentioned": payload.get("is_mentioned", False),
                  "is_reply_to_bot": payload.get("is_reply_to_bot", False),
                  "reply": payload.get("reply")},
    )
    return await dispatch_template_message(bot_record, request)


async def job_worker(bot_client):
    # Only one release may own the worker during blue-green overlap. Keep the
    # advisory lock on a dedicated connection until the worker is cancelled.
    if engine.dialect.name == "postgresql":
        async with engine.connect() as connection:
            while not await connection.scalar(text("SELECT pg_try_advisory_lock(73412019)")):
                await asyncio.sleep(settings.worker_poll_seconds)
            try:
                await recover_jobs_and_work(bot_client)
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(73412019)"))
    else:
        await recover_jobs_and_work(bot_client)


async def recover_jobs_and_work(bot_client):
    async with Session.begin() as session:
        await session.execute(text("UPDATE jobs SET status='pending' WHERE status='processing'"))
    await process_jobs(bot_client)


async def process_jobs(bot_client):
    while True:
        async with Session() as session:
            async with session.begin():
                job = await session.scalar(select(Job).where(Job.status == "pending").order_by(Job.id).with_for_update(skip_locked=True).limit(1))
                if job:
                    job.status = "processing"
                    job.attempts += 1
                    job_id, bot_id, chat_id, payload = job.id, job.bot_id, job.chat_id, job.payload
                    attempts = job.attempts
            if job is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue

            try:
                if payload.get("kind") in {"summary", "message"}:
                    async with session.begin():
                        internal_bot = await session.scalar(
                            select(InternalBot).where(InternalBot.botx_id == bot_id)
                        )
                    if internal_bot is None or (
                        payload.get("internal_bot_id") and payload["internal_bot_id"] != str(internal_bot.id)
                    ):
                        output = ""
                    elif payload.get("is_group") and not internal_bot.groups_enabled:
                        output = ""
                    else:
                        output = await dispatch_queued_message(internal_bot, bot_id, chat_id, payload)
                        if payload.get("is_group") and not (
                            payload.get("command") or payload.get("kind") == "summary"
                            or payload.get("is_mentioned") or payload.get("is_reply_to_bot")
                        ):
                            output = ""
                else:
                    output = payload["text"]
                if output:
                    await asyncio.wait_for(
                        bot_client.send_message(bot_id=bot_id, chat_id=chat_id, body=output),
                        timeout=20,
                    )
            except Exception as exc:
                status = "pending" if attempts < 3 else "failed"
                delivery_error = "timeout" if isinstance(exc, TimeoutError) else "send_failed"
            else:
                status = "completed"
                delivery_error = None

            async with session.begin():
                record = await session.get(Job, job_id)
                if record:
                    record.status = status
                    if record.notification_token_id is not None:
                        record.payload = {**payload, "delivery_error": delivery_error}
                    elif payload.get("retain_content") is False and status in {"completed", "failed"}:
                        record.payload = {k: v for k, v in payload.items() if k not in {"text", "reply"}}


ADMIN_SESSION_COOKIE = "gateway_admin_session"
ADMIN_SESSION_TTL = 12 * 60 * 60
ADMIN_STATIC_DIR = Path(__file__).parent


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _session_signature(timestamp: str) -> str:
    return hmac.new(settings.admin_token.encode(), timestamp.encode(), "sha256").hexdigest()


def _valid_admin_session(value: str | None) -> bool:
    if not value or not settings.admin_token:
        return False
    try:
        timestamp, signature = value.split(".", 1)
        issued_at = int(timestamp)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > ADMIN_SESSION_TTL:
        return False
    return _constant_time_equal(signature, _session_signature(timestamp))


async def authorized(
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    token_ok = bool(settings.admin_token) and bool(x_admin_token) and _constant_time_equal(
        x_admin_token, settings.admin_token
    )
    session_ok = _valid_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))
    if not (token_ok or session_ok):
        raise HTTPException(401, "Invalid admin token")


class AdminLogin(BaseModel):
    token: str


@app.post("/api/v1/admin/login")
async def admin_login(credentials: AdminLogin, request: Request, response: Response):
    if not settings.admin_token or not _constant_time_equal(credentials.token, settings.admin_token):
        raise HTTPException(401, "Invalid admin token")

    timestamp = str(int(time.time()))
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        f"{timestamp}.{_session_signature(timestamp)}",
        max_age=ADMIN_SESSION_TTL,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True}


@app.get("/api/v1/admin/session")
async def admin_session(request: Request):
    if not _valid_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE)):
        raise HTTPException(401, "Admin session required")
    return {"authenticated": True}


@app.post("/api/v1/admin/logout", status_code=204)
async def admin_logout(response: Response):
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    response.status_code = 204
    return response


@app.get("/health/")
async def health(): return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    async with engine.connect() as conn: await conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def admin_ui():
    return FileResponse(
        ADMIN_STATIC_DIR / "admin.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/static/admin.css")
async def admin_css():
    return FileResponse(
        ADMIN_STATIC_DIR / "admin.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/static/admin.js")
async def admin_js():
    return FileResponse(
        ADMIN_STATIC_DIR / "admin.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/admin/bot-templates", dependencies=[Depends(authorized)])
async def list_bot_templates():
    return template_catalog()


@app.get("/api/v1/admin/bots", dependencies=[Depends(authorized)])
async def list_bots():
    async with Session() as s:
        rows = (await s.scalars(select(Bot))).all()
        return [bot_record_response(b) for b in rows]


@app.get("/api/v1/admin/botx", dependencies=[Depends(authorized)])
async def list_botx():
    async with Session() as session:
        rows = await session.execute(
            select(Bot, InternalBot).outerjoin(InternalBot, InternalBot.botx_id == Bot.id)
            .order_by(Bot.name, Bot.id)
        )
        return [{
            **bot_record_response(record),
            "internal_bot_id": linked.id if linked else None,
            "internal_bot_name": linked.name if linked else None,
        } for record, linked in rows]


@app.post("/api/v1/admin/botx/{bot_id}/check", dependencies=[Depends(authorized)])
async def check_botx(bot_id: uuid.UUID):
    async with Session() as session:
        record = await session.get(Bot, bot_id)
        if record is None:
            raise HTTPException(404, "Подключение BotX не найдено.")
        if not record.enabled or not record.cts_url or not record.secret_key:
            raise HTTPException(409, "Подключение отключено или не настроено.")

    try:
        chats = await asyncio.wait_for(bot.list_chats(bot_id=bot_id), timeout=10)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(504, "CTS не ответил за время проверки.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "Не удалось связаться с CTS. Проверьте адрес и сетевой доступ.") from exc
    except InvalidBotAccountError as exc:
        raise HTTPException(502, "Не удалось авторизовать бота в CTS. Сверьте его учётные данные.") from exc
    except PermissionDeniedError as exc:
        raise HTTPException(502, "CTS запретил боту запрашивать список чатов.") from exc
    except RateLimitReachedError as exc:
        raise HTTPException(503, "CTS ограничил частоту запросов. Повторите проверку позже.") from exc
    except UnknownBotAccountError as exc:
        raise HTTPException(409, "Учётная запись ещё не загружена шлюзом.") from exc
    except BaseClientError as exc:
        raise HTTPException(502, "CTS вернул ошибку при запросе списка чатов.") from exc
    return {"status": "ok", "chat_count": len(chats)}


def bot_record_response(record: Bot) -> dict:
    return {
        "bot_id": str(record.id),
        "name": record.name,
        "cts_url": record.cts_url,
        "enabled": record.enabled,
        "notification_ready": bool(record.enabled and record.cts_url and record.secret_key),
        "default_handler": record.default_handler,
        "template_id": record.template_id,
        "template_status": "available" if get_template(record.template_id) else "unavailable",
    }


class BotXCreateRequest(BaseModel):
    bot_id: uuid.UUID
    cts_url: AnyHttpUrl
    secret_key: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=200)


class BotCreateRequest(BotXCreateRequest):
    template_id: str = Field(default="echo", min_length=1, max_length=64)


@app.post("/api/v1/admin/bots", dependencies=[Depends(authorized)], status_code=201)
async def create_bot(req: BotCreateRequest):
    return await register_botx(req, req.template_id)


@app.post("/api/v1/admin/botx", dependencies=[Depends(authorized)], status_code=201)
async def create_botx(req: BotXCreateRequest):
    return await register_botx(req, None)


async def register_botx(req: BotXCreateRequest, template_id: str | None):
    if (not req.name.strip() or not req.secret_key.strip()
        or "\x00" in req.name or "\x00" in req.secret_key):
        raise HTTPException(422, "Bot name and secret key are required")
    if template_id is not None and get_template(template_id) is None:
        raise HTTPException(422, "Unknown bot template")

    async with Session() as session:
        record = await session.get(Bot, req.bot_id)
        if record and record.cts_url and record.secret_key:
            raise HTTPException(409, "Bot is already registered")
        if record is None:
            record = Bot(
                id=req.bot_id,
                name=req.name.strip(),
                enabled=True,
                default_handler="echo",
                template_id=template_id or "echo",
                cts_url=str(req.cts_url),
                secret_key=req.secret_key,
                credential_revision=1,
            )
            session.add(record)
        else:
            record.name = req.name.strip()
            record.enabled = True
            if template_id is not None:
                record.template_id = template_id
            record.cts_url = str(req.cts_url)
            record.secret_key = req.secret_key
            record.credential_revision += 1
        if template_id is not None:
            await session.flush()
            linked = await session.scalar(select(InternalBot).where(InternalBot.botx_id == record.id))
            if linked:
                linked.template_id = template_id
            else:
                session.add(InternalBot(
                    name=record.name, botx_id=record.id, template_id=template_id,
                    default_handler=record.default_handler,
                ))
        await session.commit()
        await session.refresh(record)

    publish_botx_credentials(record)
    return bot_record_response(record)


def publish_botx_credentials(record: Bot) -> None:
    account = BotAccountWithSecret(
        id=record.id,
        cts_url=record.cts_url,
        secret_key=record.secret_key,
        revision=record.credential_revision,
    )
    account = account.model_copy(update={
        "account_key": BotAccountKey(server_id=account.host.casefold(), bot_id=record.id),
    })
    account_key = account_provider.upsert(account)
    bot.invalidate_account(account_key)


class BotTemplateUpdate(BaseModel):
    template_id: str = Field(min_length=1, max_length=64)


@app.patch("/api/v1/admin/bots/{bot_id}", dependencies=[Depends(authorized)])
async def update_bot_template(bot_id: uuid.UUID, req: BotTemplateUpdate):
    if get_template(req.template_id) is None:
        raise HTTPException(422, "Unknown bot template")
    async with Session() as session:
        record = await session.get(Bot, bot_id)
        if record is None:
            raise HTTPException(404, "Bot not found")
        record.template_id = req.template_id
        linked = await session.scalar(select(InternalBot).where(InternalBot.botx_id == bot_id))
        if linked:
            linked.template_id = req.template_id
        await session.commit()
        await session.refresh(record)
        return bot_record_response(record)


_INTERNAL_BOTS_URL = "/api/v1/admin/internal-bots"


class InternalBotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template_id: str = Field(min_length=1, max_length=64)
    groups_enabled: bool = True
    group_history_enabled: bool = True
    botx_id: uuid.UUID | None = None
    external_system_id: int | None = Field(default=None, ge=1, le=2_147_483_647)


def internal_bot_info(record: InternalBot) -> dict:
    return {
        "id": record.id, "name": record.name, "template_id": record.template_id,
        "botx_id": record.botx_id, "groups_enabled": record.groups_enabled,
        "group_history_enabled": record.group_history_enabled,
        "external_system_id": record.external_system_id,
    }


def update_internal_settings(record: InternalBot, req: InternalBotRequest) -> None:
    name = req.name.strip()
    if not name or "\x00" in name:
        raise HTTPException(422, "Bot name is invalid")
    if get_template(req.template_id) is None and req.template_id != record.template_id:
        raise HTTPException(422, "Unknown bot template")
    record.name = name
    record.template_id = req.template_id
    record.groups_enabled = req.groups_enabled
    record.group_history_enabled = req.group_history_enabled


async def validate_internal_bot_connection(session, botx_id: uuid.UUID | None, internal_bot_id=None):
    if botx_id is None:
        return
    if not await session.get(Bot, botx_id, with_for_update=True):
        raise HTTPException(404, "BotX connection not found")
    linked = await session.scalar(select(InternalBot.id).where(InternalBot.botx_id == botx_id))
    if linked is not None and linked != internal_bot_id:
        raise HTTPException(409, "BotX already has an incoming handler")


async def update_internal_bot_system(session, record: InternalBot, req: InternalBotRequest):
    if "external_system_id" not in req.model_fields_set:
        return
    if req.external_system_id is not None and not await session.get(NotificationIntegration, req.external_system_id):
        raise HTTPException(404, "Внешняя система не найдена.")
    record.external_system_id = req.external_system_id


@app.get(_INTERNAL_BOTS_URL, dependencies=[Depends(authorized)])
async def list_internal_bots():
    async with Session() as session:
        rows = await session.scalars(select(InternalBot).order_by(InternalBot.name, InternalBot.id))
        return [internal_bot_info(row) for row in rows]


@app.post(_INTERNAL_BOTS_URL, dependencies=[Depends(authorized)], status_code=201)
async def create_internal_bot(req: InternalBotRequest):
    async with Session.begin() as session:
        await validate_internal_bot_connection(session, req.botx_id)
        record = InternalBot(botx_id=req.botx_id)
        update_internal_settings(record, req)
        await update_internal_bot_system(session, record, req)
        session.add(record)
        await session.flush()
        return internal_bot_info(record)


@app.put(_INTERNAL_BOTS_URL + "/{internal_bot_id}", dependencies=[Depends(authorized)])
async def update_internal_bot(internal_bot_id: uuid.UUID, req: InternalBotRequest):
    async with Session.begin() as session:
        change_connection = "botx_id" in req.model_fields_set
        if change_connection:
            await validate_internal_bot_connection(session, req.botx_id, internal_bot_id)
        record = await session.get(InternalBot, internal_bot_id, with_for_update=True)
        if not record:
            raise HTTPException(404, "Internal bot not found")
        update_internal_settings(record, req)
        await update_internal_bot_system(session, record, req)
        if change_connection:
            record.botx_id = req.botx_id
        return internal_bot_info(record)


class BotXBindingRequest(BaseModel):
    internal_bot_id: uuid.UUID | None


class BotXUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cts_url: AnyHttpUrl | None = None
    secret_key: str | None = Field(default=None, min_length=1, max_length=4096)
    internal_bot_id: uuid.UUID | None = None


@app.put("/api/v1/admin/botx/{botx_id}", dependencies=[Depends(authorized)])
async def update_botx(botx_id: uuid.UUID, req: BotXUpdateRequest):
    name = req.name.strip()
    if not name or "\x00" in name:
        raise HTTPException(422, "BotX name is invalid")
    if req.secret_key is not None and (not req.secret_key.strip() or "\x00" in req.secret_key):
        raise HTTPException(422, "Secret key is invalid")

    async with Session.begin() as session:
        connection = await session.get(Bot, botx_id, with_for_update=True)
        if not connection:
            raise HTTPException(404, "BotX connection not found")
        if "internal_bot_id" in req.model_fields_set:
            await set_botx_handler(session, botx_id, req.internal_bot_id)
        connection.name = name
        cts_url = str(req.cts_url) if req.cts_url else connection.cts_url
        secret = req.secret_key if req.secret_key is not None else connection.secret_key
        changed = (cts_url, secret) != (connection.cts_url, connection.secret_key)
        if changed:
            connection.cts_url, connection.secret_key = cts_url, secret
            connection.credential_revision += 1

    if changed and connection.enabled and connection.cts_url and connection.secret_key:
        publish_botx_credentials(connection)
    return bot_record_response(connection)


@app.put("/api/v1/admin/botx/{botx_id}/handler", dependencies=[Depends(authorized)])
async def bind_botx_handler(botx_id: uuid.UUID, req: BotXBindingRequest):
    async with Session.begin() as session:
        connection = await session.get(Bot, botx_id, with_for_update=True)
        if not connection:
            raise HTTPException(404, "BotX connection not found")
        await set_botx_handler(session, botx_id, req.internal_bot_id)
    return {"internal_bot_id": req.internal_bot_id}


async def set_botx_handler(session, botx_id: uuid.UUID, internal_bot_id: uuid.UUID | None):
    target = await session.get(InternalBot, internal_bot_id, with_for_update=True) if internal_bot_id else None
    if internal_bot_id and not target:
        raise HTTPException(404, "Internal bot not found")
    if target and target.botx_id and target.botx_id != botx_id:
        raise HTTPException(409, "Internal bot is already linked to another BotX")
    current = await session.scalar(
        select(InternalBot).where(InternalBot.botx_id == botx_id).with_for_update()
    )
    if current and current != target:
        current.botx_id = None
        await session.flush()
    if target:
        target.botx_id = botx_id


class GrantRequest(BaseModel):
    bot_id: uuid.UUID | None = None
    internal_bot_id: uuid.UUID | None = None
    identity_type: Literal["email", "ad", "huid"]
    identity: str = Field(min_length=1, max_length=320)
    ad_domain: str | None = Field(default=None, max_length=255)
    permission: Literal["use"] = "use"


def grant_response(grant: UserGrant) -> dict:
    return {
        "id": grant.id,
        "internal_bot_id": str(grant.internal_bot_id),
        "bot_id": str(grant.internal_bot_id),
        "identity_type": grant.identity_type,
        "identity": grant.identity_value or grant.user_id,
        "ad_domain": grant.ad_domain,
        "user_huid": grant.user_id,
        "permission": grant.permission,
    }


@app.post("/api/v1/admin/grants", dependencies=[Depends(authorized)])
async def create_grant(req: GrantRequest):
    async with Session() as s:
        if req.internal_bot_id:
            internal_bot = await s.get(InternalBot, req.internal_bot_id)
        elif req.bot_id:
            internal_bot = await s.scalar(select(InternalBot).where(InternalBot.botx_id == req.bot_id))
        else:
            raise HTTPException(422, "Internal bot is required")
        if not internal_bot:
            raise HTTPException(404, "Internal bot not found")

        identity = req.identity.strip()
        if not identity:
            raise HTTPException(422, "Identity is required")
        if req.identity_type == "ad" and not req.ad_domain:
            raise HTTPException(422, "AD domain is required for an AD account")
        if req.identity_type != "ad" and req.ad_domain:
            raise HTTPException(422, "AD domain is only valid for an AD account")

        if req.identity_type == "huid":
            try:
                resolved_huid = str(uuid.UUID(identity))
            except ValueError as exc:
                raise HTTPException(422, "HUID must be a UUID") from exc
            identity_value = resolved_huid
        else:
            bot_record = await s.get(Bot, internal_bot.botx_id) if internal_bot.botx_id else None
            if not bot_record or not bot_record.enabled or not bot_record.cts_url or not bot_record.secret_key:
                raise HTTPException(422, "Link a connected BotX to resolve users by email or AD")
            try:
                if req.identity_type == "email":
                    if "@" not in identity or any(char.isspace() for char in identity):
                        raise HTTPException(422, "Email address is invalid")
                    resolved_user = await bot.search_user_by_email_post(
                        bot_id=bot_record.id,
                        email=identity,
                    )
                else:
                    resolved_user = await bot.search_user_by_ad(
                        bot_id=bot_record.id,
                        ad_login=identity,
                        ad_domain=req.ad_domain.strip(),
                    )
            except UserNotFoundError as exc:
                raise HTTPException(404, "User not found on this eXpress server") from exc
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(502, "Could not resolve user through BotX") from exc
            resolved_huid = str(resolved_user.huid)
            identity_value = identity

        existing = await s.scalar(select(UserGrant).where(
            UserGrant.internal_bot_id == internal_bot.id,
            UserGrant.user_id == resolved_huid,
            UserGrant.permission == req.permission,
        ))
        if existing:
            return {"status": "granted", "grant": grant_response(existing)}
        grant = UserGrant(
            internal_bot_id=internal_bot.id,
            user_id=resolved_huid,
            identity_type=req.identity_type,
            identity_value=identity_value,
            ad_domain=req.ad_domain.strip() if req.ad_domain else None,
            permission=req.permission,
        )
        s.add(grant)
        await s.commit()
        await s.refresh(grant)
        return {"status": "granted", "grant": grant_response(grant)}


@app.get("/api/v1/admin/grants", dependencies=[Depends(authorized)])
async def list_grants():
    async with Session() as s:
        return [grant_response(g) for g in (await s.scalars(select(UserGrant))).all()]


@app.delete("/api/v1/admin/grants/{grant_id}", dependencies=[Depends(authorized)])
async def delete_grant(grant_id: int):
    async with Session() as s:
        grant = await s.get(UserGrant, grant_id)
        if grant is None: raise HTTPException(404, "Grant not found")
        await s.delete(grant); await s.commit()
    return Response(status_code=204)


class ChatSettings(BaseModel):
    bot_id: uuid.UUID
    chat_id: uuid.UUID
    history_enabled: bool = True
    handler_name: str | None = None


@app.put("/api/v1/admin/chats", dependencies=[Depends(authorized)])
async def update_chat(req: ChatSettings):
    async with Session() as s:
        if not await s.get(Bot, req.bot_id): raise HTTPException(404, "Bot not found")
        row = await s.scalar(select(Chat).where(Chat.bot_id == req.bot_id, Chat.chat_id == req.chat_id))
        if row is None: row = Chat(bot_id=req.bot_id, chat_id=req.chat_id, chat_type="group")
        row.history_enabled, row.handler_name = req.history_enabled, req.handler_name
        s.add(row); await s.commit()
    return {"status": "updated"}


_INTEGRATIONS_URL = "/api/v1/admin/notification-integrations"
NotificationPathId = Annotated[int, PathParam(ge=1, le=2_147_483_647)]


class NotificationRouteRequest(BaseModel):
    chat_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class NotificationRouteInput(NotificationRouteRequest):
    bot_id: uuid.UUID


class NotificationIntegrationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    routes: list[NotificationRouteInput] | None = Field(default=None, max_length=100)
    outbound_url: AnyHttpUrl | None = None
    outbound_auth_type: ExternalAuthType = "none"
    outbound_token: SecretStr | None = None

    @field_validator("outbound_url")
    @classmethod
    def validate_outbound_url(cls, value):
        if value is not None and len(str(value)) > 2048:
            raise ValueError("Длина адреса не должна превышать 2048 символов.")
        if value is not None and (
            value.scheme != "https" or value.port != 443
            or value.username is not None or value.password is not None
            or value.fragment is not None
        ):
            raise ValueError("Укажите HTTPS-адрес на порту 443 без учётных данных и фрагмента.")
        return value

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_text(cls, value):
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("Text must not contain null characters")
            return value.strip()
        return value


def notification_token_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def notification_token_info(token: NotificationToken) -> dict:
    return {
        "id": token.id,
        "bot_id": token.bot_id,
        "prefix": token.prefix,
        "created_at": token.created_at,
        "revoked_at": token.revoked_at,
    }


@app.get(_INTEGRATIONS_URL, dependencies=[Depends(authorized)])
async def list_notification_integrations():
    async with Session() as session:
        rows = await session.execute(
            select(NotificationIntegration, NotificationToken)
            .outerjoin(NotificationToken, NotificationToken.integration_id == NotificationIntegration.id)
            .order_by(NotificationIntegration.id.desc(), NotificationToken.id.desc())
        )
        result = {}
        for row, token in rows:
            integration = result.setdefault(row.id, {
                "id": row.id, "name": row.name, "description": row.description,
                "created_at": row.created_at, "tokens": [], "routes": [],
                "outbound_url": row.outbound_url, "outbound_auth_type": row.outbound_auth_type,
                "outbound_token_set": bool(row.outbound_token),
            })
            if token:
                integration["tokens"].append(notification_token_info(token))
        routes = await session.execute(
            select(NotificationRoute, Bot).join(Bot).order_by(Bot.name, Bot.id)
        )
        for route, connection in routes:
            if route.integration_id in result:
                result[route.integration_id]["routes"].append({
                    "bot_id": route.bot_id, "bot_name": connection.name, "chat_ids": route.chat_ids,
                })
    return list(result.values())


async def issue_notification_token(session, integration_id: int):
    secret = "ntf_" + secrets.token_urlsafe(32)
    token = NotificationToken(
        integration_id=integration_id,
        digest=notification_token_digest(secret), prefix=secret[:12],
    )
    session.add(token)
    await session.flush()
    return {**notification_token_info(token), "token": secret}


async def replace_notification_routes(session, integration_id: int, inputs: list[NotificationRouteInput]):
    requested = {item.bot_id: item for item in inputs}
    if len(requested) != len(inputs):
        raise HTTPException(422, "Choose each BotX only once")
    if requested:
        known = set(await session.scalars(select(Bot.id).where(Bot.id.in_(requested))))
        if known != requested.keys():
            raise HTTPException(404, "BotX connection not found")

    current = {
        route.bot_id: route for route in await session.scalars(
            select(NotificationRoute).where(NotificationRoute.integration_id == integration_id)
        )
    }
    for bot_id, route in current.items():
        if bot_id not in requested:
            await session.delete(route)
    for bot_id, item in requested.items():
        route = current.get(bot_id)
        if route is None:
            route = NotificationRoute(integration_id=integration_id, bot_id=bot_id)
            session.add(route)
        if bot_id not in current or "chat_ids" in item.model_fields_set:
            route.chat_ids = list(dict.fromkeys(str(chat_id) for chat_id in item.chat_ids))


def update_outbound_settings(row: NotificationIntegration, req: NotificationIntegrationRequest):
    if "outbound_url" in req.model_fields_set:
        row.outbound_url = str(req.outbound_url) if req.outbound_url is not None else None
    auth_type = req.outbound_auth_type if "outbound_auth_type" in req.model_fields_set else row.outbound_auth_type
    token = row.outbound_token
    if "outbound_token" in req.model_fields_set:
        token = req.outbound_token.get_secret_value() if req.outbound_token is not None else None
        if token is not None and (
            not 1 <= len(token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in token)
        ):
            raise HTTPException(422, "Токен должен содержать от 1 до 4096 печатных ASCII-символов без пробелов.")
    if auth_type == "none":
        if req.outbound_token is not None:
            raise HTTPException(422, "Выберите тип авторизации для токена.")
        token = None
    elif token is None:
        raise HTTPException(422, "Укажите токен для выбранного типа авторизации.")
    row.outbound_auth_type = auth_type
    row.outbound_token = token


@app.post(_INTEGRATIONS_URL, dependencies=[Depends(authorized)], status_code=201)
async def create_notification_integration(req: NotificationIntegrationRequest):
    async with Session.begin() as session:
        row = NotificationIntegration(name=req.name, description=req.description, outbound_auth_type="none")
        update_outbound_settings(row, req)
        session.add(row)
        await session.flush()
        if req.routes:
            await replace_notification_routes(session, row.id, req.routes)
    return {"id": row.id}


@app.put(_INTEGRATIONS_URL + "/{integration_id}", dependencies=[Depends(authorized)])
async def update_notification_integration(integration_id: NotificationPathId, req: NotificationIntegrationRequest):
    async with Session.begin() as session:
        row = await session.get(NotificationIntegration, integration_id, with_for_update=True)
        if not row:
            raise HTTPException(404, "Integration not found")
        row.name, row.description = req.name, req.description
        update_outbound_settings(row, req)
        if req.routes is not None:
            await replace_notification_routes(session, row.id, req.routes)
    return {"id": row.id}


@app.post(_INTEGRATIONS_URL + "/{integration_id}/check", dependencies=[Depends(authorized)])
async def check_external_system(integration_id: NotificationPathId):
    async with Session() as session:
        row = await session.get(NotificationIntegration, integration_id)
        if row is None:
            raise HTTPException(404, "Внешняя система не найдена.")
        if not row.outbound_url:
            raise HTTPException(409, "Укажите HTTPS-адрес внешней системы.")
        host = httpx.URL(row.outbound_url).raw_host.decode("ascii").strip("[]")

    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), timeout=5)
        writer.close()
        await writer.wait_closed()
    except TimeoutError as exc:
        raise HTTPException(504, "Истекло время ожидания подключения к TCP-порту 443.") from exc
    except OSError as exc:
        raise HTTPException(502, "Не удалось подключиться к TCP-порту 443.") from exc
    return {"status": "ok", "port": 443}


@app.put(_INTEGRATIONS_URL + "/{integration_id}/routes/{bot_id}", dependencies=[Depends(authorized)])
async def upsert_notification_route(
    integration_id: NotificationPathId, bot_id: uuid.UUID, req: NotificationRouteRequest,
):
    async with Session.begin() as session:
        if not await session.get(NotificationIntegration, integration_id, with_for_update=True):
            raise HTTPException(404, "Integration not found")
        if not await session.get(Bot, bot_id):
            raise HTTPException(404, "BotX connection not found")
        route = await session.get(NotificationRoute, (integration_id, bot_id))
        if not route:
            route = NotificationRoute(integration_id=integration_id, bot_id=bot_id)
            session.add(route)
        route.chat_ids = list(dict.fromkeys(str(chat_id) for chat_id in req.chat_ids))
    return {"bot_id": bot_id, "chat_ids": route.chat_ids}


@app.delete(_INTEGRATIONS_URL + "/{integration_id}/routes/{bot_id}", dependencies=[Depends(authorized)], status_code=204)
async def delete_notification_route(integration_id: NotificationPathId, bot_id: uuid.UUID):
    async with Session.begin() as session:
        if not await session.get(NotificationIntegration, integration_id, with_for_update=True):
            raise HTTPException(404, "Integration not found")
        route = await session.get(NotificationRoute, (integration_id, bot_id))
        if route:
            await session.delete(route)
    return Response(status_code=204)


@app.post(_INTEGRATIONS_URL + "/{integration_id}/tokens", dependencies=[Depends(authorized)], status_code=201)
async def create_notification_token(
    integration_id: NotificationPathId, response: Response,
):
    async with Session.begin() as session:
        if not await session.get(NotificationIntegration, integration_id):
            raise HTTPException(404, "Integration not found")
        token = await issue_notification_token(session, integration_id)
    response.headers["Cache-Control"] = "no-store"
    return token


@app.delete(_INTEGRATIONS_URL + "/{integration_id}/tokens/{token_id}", dependencies=[Depends(authorized)], status_code=204)
async def revoke_notification_token(integration_id: NotificationPathId, token_id: NotificationPathId):
    async with Session.begin() as session:
        token = await session.get(NotificationToken, token_id, with_for_update=True)
        if not token or token.integration_id != integration_id:
            raise HTTPException(404, "Token not found")
        if token.revoked_at is None:
            token.revoked_at = datetime.now(timezone.utc)
    return Response(status_code=204)


@app.get(_INTEGRATIONS_URL + "/{integration_id}/logs", dependencies=[Depends(authorized)])
async def notification_integration_logs(
    integration_id: NotificationPathId,
    before_id: int | None = Query(default=None, ge=1, le=2_147_483_647),
):
    async with Session() as session:
        if not await session.get(NotificationIntegration, integration_id):
            raise HTTPException(404, "Integration not found")
        query = (
            select(Job, NotificationToken.prefix, Bot.name)
            .join(NotificationToken, Job.notification_token_id == NotificationToken.id)
            .join(Bot, Job.bot_id == Bot.id)
            .where(NotificationToken.integration_id == integration_id)
            .order_by(Job.id.desc()).limit(51)
        )
        if before_id is not None:
            query = query.where(Job.id < before_id)
        rows = (await session.execute(query)).all()
    entries = [
        {
            "job_id": job.id, "bot_id": job.bot_id, "bot_name": bot_name, "chat_id": job.chat_id,
            "token_prefix": prefix, "created_at": job.created_at, "status": job.status,
            "attempts": job.attempts, "delivery_error": job.payload.get("delivery_error"),
        }
        for job, prefix, bot_name in rows[:50]
    ]
    return {"entries": entries, "next_before_id": entries[-1]["job_id"] if len(rows) > 50 else None}


async def authorized_notification(
    request: Request,
    x_notification_token: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> NotificationToken | None:
    if settings.notification_token and x_notification_token and _constant_time_equal(
        x_notification_token, settings.notification_token
    ):
        return None
    if x_notification_token and x_notification_token.startswith("ntf_"):
        async with Session() as session:
            token = await session.scalar(select(NotificationToken).where(
                NotificationToken.digest == notification_token_digest(x_notification_token),
                NotificationToken.revoked_at.is_(None),
            ))
        if not token:
            raise HTTPException(401, "Invalid notification token")
        return token
    await authorized(request, x_admin_token)
    return None


class Notification(BaseModel):
    bot_id: uuid.UUID
    chat_id: uuid.UUID
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Notification text must be non-blank and contain no null characters")
        return value


async def allows_notification(session, token: NotificationToken, bot_id: uuid.UUID, chat_id: uuid.UUID):
    if token.bot_id is not None and token.bot_id != bot_id:
        return False
    route = await session.get(NotificationRoute, (token.integration_id, bot_id))
    return bool(route and (not route.chat_ids or str(chat_id) in route.chat_ids))


@app.post("/api/v1/messages", status_code=202)
@app.post("/api/v1/notifications", status_code=202)
async def notification(req: Notification, token: NotificationToken | None = Depends(authorized_notification)):
    async with Session.begin() as session:
        if token and not await allows_notification(session, token, req.bot_id, req.chat_id):
            raise HTTPException(403, "System is not authorized to send to this BotX and chat")
        record = await session.get(Bot, req.bot_id)
        if not record or not record.enabled or not record.cts_url or not record.secret_key:
            raise HTTPException(404, "Bot not found or not connected")
        job = Job(
            bot_id=req.bot_id,
            chat_id=req.chat_id,
            payload={"text": req.text, "kind": "notification"},
            notification_token_id=token.id if token else None,
        )
        session.add(job)
        await session.flush()
    return {"job_id": job.id, "status": job.status}


@app.get("/api/v1/messages/{job_id}")
@app.get("/api/v1/notifications/{job_id}")
async def notification_status(
    job_id: int = PathParam(ge=1, le=2_147_483_647),
    token: NotificationToken | None = Depends(authorized_notification),
):
    async with Session() as session:
        job = await session.get(Job, job_id)
        if not job or job.payload.get("kind") != "notification":
            raise HTTPException(404, "Notification not found")
        if token:
            owner = await session.get(NotificationToken, job.notification_token_id) if job.notification_token_id else None
            if (
                not owner or owner.integration_id != token.integration_id
                or not await allows_notification(session, token, job.bot_id, job.chat_id)
            ):
                raise HTTPException(404, "Notification not found")
        return {"job_id": job.id, "status": job.status, "attempts": job.attempts}


@app.get("/metrics")
async def metrics():
    async with Session() as s:
        count = await s.scalar(select(Job).where(Job.status == "pending"))
        return {"gateway_pending_jobs": 1 if count else 0}
