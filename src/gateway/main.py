import json
import uuid
import asyncio
import hmac
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import Response
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from pybotx import FastAPIBotAppConfig, setup_fastapi_bot
from pybotx import BotAccountKey, BotAccountWithSecret, UserNotFoundError

from .config import settings
from .models import Base, Bot, Chat, Message, UserGrant, Job
from .integrations import EchoHandler, HttpHandler, HandlerRequest
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
                session.add(Bot(
                    id=ident,
                    name=item.get("name", str(ident)),
                    enabled=item.get("enabled", True),
                    default_handler=item.get("default_handler", "echo"),
                    cts_url=item.get("cts_url"),
                    secret_key=item.get("secret_key"),
                ))
            elif not record.cts_url or not record.secret_key:
                record.cts_url = item.get("cts_url")
                record.secret_key = item.get("secret_key")
                record.name = item.get("name", record.name)
                record.enabled = item.get("enabled", record.enabled)
                record.default_handler = item.get("default_handler", record.default_handler)
        await session.execute(text("UPDATE jobs SET status='pending' WHERE status='processing'"))
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


async def dispatch_template_message(bot_record: Bot, request: HandlerRequest) -> str:
    registration = get_template(bot_record.template_id)
    if registration is None:
        return f"Шаблон бота «{bot_record.template_id}» недоступен. Выберите доступный шаблон в админке."

    template_class = registration.bot_class
    command_name = request.command.lower() if request.command else None
    if command_name == "/summary" and not template_class.has_command("/summary"):
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
        and not template_class.has_command(command_name)
    ):
        result = await resolve_handler(bot_record.default_handler).handle(request)
    else:
        result = await template_class().dispatch(request)
    if result.status != "completed" or result.error:
        raise RuntimeError(result.error or "bot template handler failed")
    return result.text or ""


async def dispatch_queued_message(
    bot_record: Bot,
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
    )
    return await dispatch_template_message(bot_record, request)


async def job_worker(bot_client):
    while True:
        async with Session() as session:
            async with session.begin():
                job = await session.scalar(select(Job).where(Job.status == "pending").order_by(Job.id).with_for_update(skip_locked=True).limit(1))
                if job:
                    job.status = "processing"
                    job.attempts += 1
                    job_id, bot_id, chat_id, payload = job.id, job.bot_id, job.chat_id, job.payload
            if job:
                try:
                    if payload.get("kind") in {"summary", "message"}:
                        bot_record = await session.get(Bot, bot_id)
                        if bot_record is None:
                            raise RuntimeError("bot record not found for queued message")
                        output = await dispatch_queued_message(bot_record, bot_id, chat_id, payload)
                    else:
                        output = payload["text"]
                    if output:
                        await asyncio.wait_for(bot_client.send_message(bot_id=bot_id, chat_id=chat_id, body=output), timeout=20)
                except Exception:
                    async with session.begin():
                        failed = await session.get(Job, job_id)
                        if failed:
                            failed.status = "pending" if failed.attempts < 3 else "failed"
                else:
                    async with session.begin():
                        completed = await session.get(Job, job_id)
                        if completed: completed.status = "completed"
            else:
                await asyncio.sleep(settings.worker_poll_seconds)


ADMIN_SESSION_COOKIE = "gateway_admin_session"
ADMIN_SESSION_TTL = 12 * 60 * 60
ADMIN_STATIC_DIR = Path(__file__).parent


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _session_signature(timestamp: str) -> str:
    return hmac.new(settings.admin_token.encode(), timestamp.encode(), "sha256").hexdigest()


def _valid_admin_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        timestamp, signature = value.split(".", 1)
        issued_at = int(timestamp)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > ADMIN_SESSION_TTL:
        return False
    return hmac.compare_digest(signature, _session_signature(timestamp))


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


def bot_record_response(record: Bot) -> dict:
    return {
        "bot_id": str(record.id),
        "name": record.name,
        "cts_url": record.cts_url,
        "enabled": record.enabled,
        "default_handler": record.default_handler,
        "template_id": record.template_id,
        "template_status": "available" if get_template(record.template_id) else "unavailable",
    }


class BotCreateRequest(BaseModel):
    bot_id: uuid.UUID
    cts_url: AnyHttpUrl
    secret_key: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=200)
    template_id: str = Field(default="echo", min_length=1, max_length=64)


@app.post("/api/v1/admin/bots", dependencies=[Depends(authorized)], status_code=201)
async def create_bot(req: BotCreateRequest):
    if not req.name.strip() or not req.secret_key.strip():
        raise HTTPException(422, "Bot name and secret key are required")
    if get_template(req.template_id) is None:
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
                template_id=req.template_id,
                cts_url=str(req.cts_url),
                secret_key=req.secret_key,
                credential_revision=1,
            )
            session.add(record)
        else:
            record.name = req.name.strip()
            record.enabled = True
            record.template_id = req.template_id
            record.cts_url = str(req.cts_url)
            record.secret_key = req.secret_key
            record.credential_revision += 1
        await session.commit()
        await session.refresh(record)

    account = BotAccountWithSecret(
        id=record.id,
        cts_url=record.cts_url,
        secret_key=record.secret_key,
        account_key=BotAccountKey(
            server_id=BotAccountWithSecret(
                id=record.id,
                cts_url=record.cts_url,
                secret_key=record.secret_key,
            ).host.casefold(),
            bot_id=record.id,
        ),
        revision=record.credential_revision,
    )
    account_key = account_provider.upsert(account)
    bot.invalidate_account(account_key)
    return bot_record_response(record)


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
        await session.commit()
        await session.refresh(record)
        return bot_record_response(record)


class GrantRequest(BaseModel):
    bot_id: uuid.UUID
    identity_type: Literal["email", "ad", "huid"]
    identity: str = Field(min_length=1, max_length=320)
    ad_domain: str | None = Field(default=None, max_length=255)
    permission: Literal["use"] = "use"


def grant_response(grant: UserGrant) -> dict:
    return {
        "id": grant.id,
        "bot_id": str(grant.bot_id),
        "identity_type": grant.identity_type,
        "identity": grant.identity_value or grant.user_id,
        "ad_domain": grant.ad_domain,
        "user_huid": grant.user_id,
        "permission": grant.permission,
    }


@app.post("/api/v1/admin/grants", dependencies=[Depends(authorized)])
async def create_grant(req: GrantRequest):
    async with Session() as s:
        bot_record = await s.get(Bot, req.bot_id)
        if not bot_record or not bot_record.enabled or not bot_record.cts_url or not bot_record.secret_key:
            raise HTTPException(404, "Bot not found or not connected")

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
            try:
                if req.identity_type == "email":
                    if "@" not in identity or any(char.isspace() for char in identity):
                        raise HTTPException(422, "Email address is invalid")
                    resolved_user = await bot.search_user_by_email_post(
                        bot_id=req.bot_id,
                        email=identity,
                    )
                else:
                    resolved_user = await bot.search_user_by_ad(
                        bot_id=req.bot_id,
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
            UserGrant.bot_id == req.bot_id,
            UserGrant.user_id == resolved_huid,
            UserGrant.permission == req.permission,
        ))
        if existing:
            return {"status": "granted", "grant": grant_response(existing)}
        grant = UserGrant(
            bot_id=req.bot_id,
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


class Notification(BaseModel): bot_id: uuid.UUID; chat_id: uuid.UUID; text: str


@app.post("/api/v1/notifications", dependencies=[Depends(authorized)], status_code=202)
async def notification(req: Notification):
    async with Session() as s:
        if not await s.get(Bot, req.bot_id): raise HTTPException(404, "Bot not found")
        job = Job(bot_id=req.bot_id, chat_id=req.chat_id, payload={"text": req.text, "kind": "notification"})
        s.add(job); await s.commit(); await s.refresh(job)
        return {"job_id": job.id, "status": job.status}


@app.get("/metrics")
async def metrics():
    async with Session() as s:
        count = await s.scalar(select(Job).where(Job.status == "pending"))
        return {"gateway_pending_jobs": 1 if count else 0}
