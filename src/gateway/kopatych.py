"""Kopatych links an eXpress message to a durable ML Portal run."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select, update

from .botx_client import BotXClient
from .config import settings
from .integrations import ExternalSystem, HandlerRequest
from .models import AssignmentEvent, Bot, Job, KopatychRun, NotificationIntegration
from .templates import BaseBot, command, register_template

logger = logging.getLogger(__name__)
TERMINAL = frozenset({"completed", "failed", "cancelled"})
QUESTIONS = frozenset({"waiting_input", "waiting_confirmation"})
PORTAL_PATH = "/api/v1/integrations/assignments"
LEGACY_PATH = "/api/v1/integrations/durable-runs"


def _connector() -> dict[str, str]:
    return {"type": "clouds", "id": settings.kopatych_connector_id}


class PortalRun(BaseModel):
    run_id: UUID
    status: str = Field(default="queued", min_length=1, max_length=32)


class PortalEvent(BaseModel):
    event_id: UUID
    sequence: int
    kind: str
    payload: dict


class PortalEvents(BaseModel):
    items: list[PortalEvent]


class AssignmentCallback(BaseModel):
    run_id: UUID
    event_id: UUID
    sequence: int = Field(gt=0, le=2_147_483_647)
    kind: str
    status: str
    result: dict

    @model_validator(mode="after")
    def validate_event(self):
        allowed = {
            "progress": {"running", "waiting_timer"},
            "question": QUESTIONS,
            "final": TERMINAL,
        }
        if self.status not in allowed.get(self.kind, ()):
            raise ValueError("Invalid assignment event kind or status")
        body = self.result.get("text")
        if body is None:
            if self.status not in {"failed", "cancelled"}:
                raise ValueError("Assignment event requires text")
        elif not isinstance(body, str) or not body.strip() or "\x00" in body:
            raise ValueError("Assignment event text must be non-blank and contain no null characters")
        if self.kind == "question":
            schema = self.result.get("response_schema")
            if (_uuid(self.result.get("pause_id")) is None or not isinstance(schema, dict)
                    or schema.get("type") not in ("string", "boolean")):
                raise ValueError("Question requires a pause ID and a string or boolean response schema")
        return self


class PortalClient:
    def __init__(self, system: ExternalSystem, *, legacy: bool = False) -> None:
        self.system = system
        self.legacy = legacy
        self.base_url = httpx.URL(system.url)
        path = self.base_url.path.rstrip("/")
        if not legacy and not path:
            path = PORTAL_PATH
        elif not legacy and path.endswith(LEGACY_PATH):
            path = path.removesuffix(LEGACY_PATH) + PORTAL_PATH
        if (not path.endswith(LEGACY_PATH if legacy else PORTAL_PATH)
                or self.base_url.query or self.base_url.fragment or self.base_url.userinfo):
            raise ValueError("Configure the ML Portal assignments collection URL")
        self.base_url = self.base_url.copy_with(path=path)

    async def request(self, method, *, route="", params=None, payload=None):
        url = self.base_url.copy_with(path=self.base_url.path + route)
        endpoint = replace(self.system, url=str(url))
        if self.legacy:
            return await endpoint.request(method, params=params, json=payload)
        return await endpoint.request(method, params=params, json=payload, extra_headers={
            "X-Assignment-Connector": f"clouds:{settings.kopatych_connector_id}",
        })

    async def admit(self, payload: dict) -> PortalRun:
        response = await self.request("POST", payload=payload)
        return PortalRun.model_validate(response.json())

    async def reply(self, run_id: UUID, pause_id: UUID, event_id: str, value: str | bool, sender: str) -> None:
        payload = {"event_id": event_id, "pause_id": str(pause_id), "value": value}
        if not self.legacy:
            payload["user"] = {"clouds": sender}
        await self.request("POST", route=f"/{run_id}/responses", payload=payload)

    async def cancel(self, run_id: UUID, event_id: str, sender: str) -> None:
        payload = {"event_id": event_id}
        if not self.legacy:
            payload["user"] = {"clouds": sender}
        await self.request("POST", route=f"/{run_id}/cancel", payload=payload)

    async def events(self, run: KopatychRun) -> list[PortalEvent]:
        response = await self.request("GET", route=f"/{run.run_id}/events", params={
            "after_sequence": run.cursor, "limit": 10,
        })
        payload = response.json()
        if not self.legacy and (
            payload.get("run_id") != str(run.run_id)
            or payload.get("user") != {"clouds": run.sender_id}
            or payload.get("reply_to") != _destination(run.bot_id, run.chat_id, run.source_message_id)
        ):
            raise ValueError("Portal delivery does not match the stored BotX binding")
        return PortalEvents.model_validate(payload).items

    async def ack(self, run_id: UUID, event_id: UUID, message_id: UUID) -> None:
        await self.request("POST", route=f"/{run_id}/events/{event_id}/ack", payload={
            "external_message_id": str(message_id),
        })


def _portal(system: ExternalSystem, bot_id: UUID, sender_id: str, *, stored_url: str | None = None) -> PortalClient | None:
    if stored_url is not None and httpx.URL(stored_url).path.rstrip("/").endswith(LEGACY_PATH):
        token = settings.kopatych_user_tokens.get(f"{bot_id}:{sender_id}")
        if token is None or system.url.rstrip("/") != stored_url.rstrip("/"):
            return None
        return PortalClient(replace(system, auth_type="bearer", _token=token.get_secret_value()), legacy=True)
    if system.auth_type != "bearer" or not system._token:
        return None
    client = PortalClient(system)
    return client if stored_url is None or stored_url == str(client.base_url) else None


def _destination(bot_id: UUID, chat_id: UUID, source_id: UUID) -> dict:
    return {**_connector(), "data": {"bot_id": str(bot_id), "chat_id": str(chat_id), "message_id": str(source_id)}}


def _identity_error(exc: httpx.HTTPStatusError) -> str | None:
    if exc.response.status_code != 403:
        return None
    try:
        detail = exc.response.json().get("detail")
    except ValueError:
        return None
    return detail.get("code") if isinstance(detail, dict) else None


async def _resolve_sender_mail(request: HandlerRequest) -> str | None:
    from .main import Session

    login, domain = request.metadata.get("sender_ad_login"), request.metadata.get("sender_ad_domain")
    if not (login and domain):
        return None
    async with Session() as session:
        bot = await session.get(Bot, UUID(request.bot_id))
    if bot is None or not bot.cts_url or not bot.secret_key:
        return None
    # This lookup supplies identity evidence. Require a verified CTS connection.
    async with BotXClient(
        bot_id=bot.id, cts_url=bot.cts_url, secret_key=bot.secret_key, verify=True,
    ) as client:
        record = await client.find_user_by_ad(login, domain)
    if not record or _uuid(record.get("user_huid")) != UUID(request.user_id):
        return None
    email = record.get("email")
    return email.strip() if isinstance(email, str) and "@" in email else None


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _answer(text: str, response_type: str | None) -> str | bool | None:
    if response_type != "boolean":
        return text.strip() or None
    value = text.strip().casefold()
    if value in {"да", "yes", "true"}:
        return True
    if value in {"нет", "no", "false"}:
        return False
    return None


@register_template(
    "kopatych", name="Копатыч", description="Передаёт поручения в ML Portal и возвращает уточнения и результат.",
)
class KopatychBot(BaseBot):
    @command("/help", "Показать, как поручить задачу и ответить на уточнение")
    async def help(self, request: HandlerRequest) -> str:
        return ("В личном чате пришлите поручение, в группе упомяните Копатыча. "
                "Чтобы продолжить, ответьте на сообщение с уточнением. "
                "Чтобы отменить, отправьте /cancel ответом на сообщение о поручении.")

    @command("/cancel", "Отменить поручение, на сообщение о котором вы отвечаете")
    async def cancel(self, request: HandlerRequest) -> str | None:
        return await self._handle_reply(request, cancel=True)

    async def on_message(self, request: HandlerRequest) -> str | None:
        if request.command or request.text.startswith("/"):
            return "Неизвестная команда. Используйте /help."
        if request.metadata.get("is_reply_to_bot"):
            return await self._handle_reply(request)
        if request.chat_type == "group" and not request.metadata.get("is_mentioned"):
            return None
        if not request.text.strip():
            return None
        client = self._client(request)
        if client is None:
            return "Подключение Копатыча к Почемучке не настроено."

        from .main import Session

        bot_id, chat_id = UUID(request.bot_id), UUID(request.chat_id)
        source_id = UUID(request.message_id)
        async with Session.begin() as session:
            local = await session.scalar(select(KopatychRun).where(
                KopatychRun.bot_id == bot_id, KopatychRun.source_message_id == source_id,
            ))
            if local is None:
                local = KopatychRun(
                    internal_bot_id=UUID(request.internal_bot_id), bot_id=bot_id,
                    external_system_id=client.system.id, portal_url=str(client.base_url),
                    chat_id=chat_id, sender_id=request.user_id, source_message_id=source_id,
                    delivery_mode=settings.kopatych_delivery_mode,
                )
                session.add(local)
        if local.run_id is not None:
            return None
        if (
            local.internal_bot_id != UUID(request.internal_bot_id) or local.external_system_id != client.system.id
            or local.portal_url != str(client.base_url) or local.chat_id != chat_id or local.sender_id != request.user_id
        ):
            return "Подключение для этого поручения изменилось. Отправьте его новым сообщением."
        if local.status == "tenant_required":
            return "Для поручений нужен активный тенант в Почемучке."
        if local.status == "identity_not_linked":
            return "Войдите в Почемучку через LDAP и проверьте доступ к тенанту."

        reply = request.metadata.get("reply") or {}
        reference = str(reply.get("body") or "")[:1000]
        payload = {
            "event_id": str(local.request_id), "source": _connector(),
            "user": {"clouds": request.user_id},
            "reply_to": _destination(bot_id, chat_id, source_id),
            "data": {"text": request.text, "timezone": settings.kopatych_timezone},
        }
        if reference:
            payload["data"]["referenced_context"] = {
                "text": reference, "message_id": str(reply.get("message_id") or ""),
            }
        if local.identity_mail:
            payload["user"]["mail"] = local.identity_mail
        try:
            run = await client.admit(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 503}:
                status = "unauthorized" if exc.response.status_code == 401 else "portal_unavailable"
                await _admission_rejected(Session, local.request_id, status)
                logger.warning("Почемучка отклонила поручение: HTTP %s · запрос %s",
                               exc.response.status_code, local.request_id)
                if status == "unauthorized":
                    return "Почемучка отклонила авторизацию Копатыча."
                return "Почемучка сейчас не принимает поручения (HTTP 503)."
            code = _identity_error(exc)
            if code == "tenant_required":
                await _admission_rejected(Session, local.request_id, code)
                return "Для поручений нужен активный тенант в Почемучке."
            if code != "identity_not_linked":
                raise
            email = await _resolve_sender_mail(request) if not local.identity_mail else None
            if email is None:
                await _admission_rejected(Session, local.request_id, code)
                return "Войдите в Почемучку через LDAP, чтобы пользоваться Копатычем."
            async with Session.begin() as session:
                pending = await session.get(KopatychRun, local.request_id, with_for_update=True)
                if pending is None or pending.run_id is not None:
                    return None
                pending.identity_mail = email
            payload["user"]["mail"] = email
            try:
                run = await client.admit(payload)
            except httpx.HTTPStatusError as retry:
                retry_code = _identity_error(retry)
                if retry_code in {"identity_not_linked", "tenant_required"}:
                    await _admission_rejected(Session, local.request_id, retry_code)
                    return "Войдите в Почемучку через LDAP и проверьте доступ к тенанту."
                raise
        async with Session.begin() as session:
            current = await session.get(KopatychRun, local.request_id, with_for_update=True)
            if current is None:
                return None
            if current.run_id is None:
                current.run_id = run.run_id
                current.status = run.status
                current.identity_mail = None
            elif current.run_id != run.run_id:
                raise ValueError("Portal changed the external run ID for an accepted request")
        logger.info("Почемучка приняла поручение · запрос %s · запуск %s", local.request_id, run.run_id)
        return None

    async def on_reply(self, request: HandlerRequest) -> str | None:
        return await self._handle_reply(request)

    @staticmethod
    def _client(request: HandlerRequest, *, stored_url: str | None = None) -> PortalClient | None:
        if request.external_system is None or request.internal_bot_id is None:
            return None
        return _portal(request.external_system, UUID(request.bot_id), request.user_id, stored_url=stored_url)

    async def _handle_reply(self, request: HandlerRequest, *, cancel: bool = False) -> str | None:
        from .main import Session

        reply_id = _uuid((request.metadata.get("reply") or {}).get("message_id"))
        if reply_id is None or (not cancel and not request.metadata.get("is_reply_to_bot")):
            return "Ответьте на сообщение Копатыча о поручении." if cancel else None
        async with Session() as session:
            target = (or_(KopatychRun.last_message_id == reply_id, KopatychRun.question_message_id == reply_id)
                      if cancel else KopatychRun.question_message_id == reply_id)
            run = await session.scalar(select(KopatychRun).where(
                KopatychRun.bot_id == UUID(request.bot_id), KopatychRun.chat_id == UUID(request.chat_id),
                KopatychRun.sender_id == request.user_id, KopatychRun.terminal.is_(False),
                KopatychRun.run_id.is_not(None), target,
            ))
        client = self._client(request, stored_url=run.portal_url) if run is not None else None
        if (run is None or client is None or request.internal_bot_id != str(run.internal_bot_id)
                or client.system.id != run.external_system_id):
            return "Активное поручение для этого ответа не найдено."
        if cancel:
            await client.cancel(run.run_id, request.message_id, request.user_id)
            return None
        if run.pause_id is None:
            return "Это уточнение уже закрыто."
        value = _answer(request.text, run.response_type)
        if value is None:
            return "Ответьте «да» или «нет»." if run.response_type == "boolean" else "Отправьте текст уточнения."
        try:
            await client.reply(run.run_id, run.pause_id, request.message_id, value, request.user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return "Это уточнение уже закрыто. Ответьте на последний вопрос Копатыча."
            if exc.response.status_code == 422:
                return "Портал не принял ответ. Проверьте формат уточнения."
            raise
        return None


async def _admission_rejected(session_factory, request_id: UUID, status: str) -> None:
    async with session_factory.begin() as session:
        run = await session.get(KopatychRun, request_id, with_for_update=True)
        if run is not None and run.run_id is None:
            run.status = status
            run.identity_mail = None


async def mark_admission_failed(session, bot_id: UUID, message_id: str | None) -> None:
    source_id = _uuid(message_id)
    if source_id is not None:
        await session.execute(update(KopatychRun).where(
            KopatychRun.bot_id == bot_id, KopatychRun.source_message_id == source_id,
            KopatychRun.run_id.is_(None), KopatychRun.status == "pending",
        ).values(status="admission_failed"))


def _question_content(text: str, response_type: str | None) -> str:
    instruction = "Ответьте «да» или «нет» на это сообщение." if response_type == "boolean" else "Ответьте на это сообщение."
    return f"{text}\n{instruction}"


def _content(event: PortalEvent) -> str | None:
    if event.kind == "accepted":
        return "Копатыч принял поручение."
    if event.kind in QUESTIONS:
        text = event.payload.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Portal question has no text")
        response_type = event.payload.get("interaction", {}).get("response_schema", {}).get("type")
        return _question_content(text, response_type)
    if event.kind in TERMINAL:
        return event.payload.get("content") or {
            "completed": "Поручение выполнено.", "failed": "Не удалось выполнить поручение.",
            "cancelled": "Поручение отменено.",
        }[event.kind]
    if event.kind in {"waiting_timer", "running", "progress"}:
        return event.payload.get("content") or event.payload.get("text")
    raise ValueError(f"Unsupported portal event kind: {event.kind}")


async def enqueue_assignment_event(session, system_id: int, event: AssignmentCallback) -> None:
    run = await session.scalar(select(KopatychRun).where(
        KopatychRun.external_system_id == system_id, KopatychRun.run_id == event.run_id,
    ).with_for_update())
    if run is None:
        raise HTTPException(409, detail={"code": "binding_pending"})
    if run.delivery_mode != "push":
        raise HTTPException(409, detail={"code": "delivery_mode_conflict"})
    payload = event.model_dump(mode="json")
    previous = await session.get(AssignmentEvent, (run.request_id, event.event_id))
    if previous:
        if previous.payload != payload:
            raise HTTPException(409, detail={"code": "event_conflict"})
        return
    if run.terminal:
        raise HTTPException(409, detail={"code": "assignment_closed"})
    if event.sequence != run.callback_sequence + 1:
        raise HTTPException(409, detail={"code": "sequence_gap", "expected_sequence": run.callback_sequence + 1})
    session.add(AssignmentEvent(
        request_id=run.request_id, event_id=event.event_id, sequence=event.sequence, payload=payload,
    ))
    session.add(Job(bot_id=run.bot_id, chat_id=run.chat_id, payload=_assignment_job_payload(
        run, event.event_id, event.kind, _callback_text(event),
    )))
    run.callback_sequence = event.sequence
    run.status = event.status
    run.terminal = event.kind == "final"


def _event_key(payload: dict) -> tuple[UUID, UUID]:
    return UUID(payload["request_id"]), UUID(payload["event_id"])


def _assignment_job_payload(run: KopatychRun, event_id: UUID, kind: str, body: str) -> dict:
    return {
        "kind": "assignment_event", "request_id": str(run.request_id), "event_id": str(event_id),
        "external_system_id": run.external_system_id, "event_kind": kind,
        "delivery_text": body,
    }


def _callback_text(callback: AssignmentCallback) -> str:
    content = callback.result.get("text")
    if callback.kind == "question":
        return _question_content(content, callback.result["response_schema"]["type"])
    if content is None:
        return {"failed": "Не удалось выполнить поручение.", "cancelled": "Поручение отменено."}[callback.status]
    return content


async def deliver_assignment_event(session_factory, bot_client, payload: dict) -> UUID:
    request_id, event_id = _event_key(payload)
    async with session_factory() as session:
        run = await session.get(KopatychRun, request_id)
        event = await session.get(AssignmentEvent, (request_id, event_id))
        if run is None or event is None:
            raise ValueError("Assignment event lost its local request")
        if event.delivered_message_id:
            return event.delivered_message_id
        bot = await session.get(Bot, run.bot_id)
        if not bot or not bot.enabled:
            raise ValueError("Assignment BotX is disabled or unavailable")
    callback = AssignmentCallback.model_validate(event.payload)
    return await _reply_to_assignment(bot_client, run, event_id, _callback_text(callback))


async def _reply_to_assignment(bot_client, run: KopatychRun, event_id: UUID, body: str) -> UUID:
    sent = await asyncio.wait_for(bot_client.reply_message(
        bot_id=run.bot_id, sync_id=run.source_message_id, body=body,
        metadata={"portal_event_id": str(event_id)},
    ), timeout=20)
    message_id = _uuid(sent)
    if message_id is None:
        raise RuntimeError("BotX did not return a reply message ID")
    return message_id


async def record_assignment_delivery(session, payload: dict, message_id: UUID) -> None:
    request_id, event_id = _event_key(payload)
    run = await session.get(KopatychRun, request_id, with_for_update=True)
    event = await session.get(AssignmentEvent, (request_id, event_id))
    if run is None or event is None:
        return
    callback = AssignmentCallback.model_validate(event.payload)
    event.delivered_message_id = message_id
    run.last_message_id = message_id
    if callback.kind == "question":
        run.pause_id = UUID(callback.result["pause_id"])
        run.question_message_id = message_id
        run.response_type = callback.result["response_schema"]["type"]
    else:
        run.pause_id = run.question_message_id = run.response_type = None


async def _start_poll_delivery(session_factory, run: KopatychRun, event: PortalEvent, body: str) -> int:
    async with session_factory.begin() as session:
        saved = await session.get(AssignmentEvent, (run.request_id, event.event_id))
        if saved is None:
            job = Job(
                bot_id=run.bot_id, chat_id=run.chat_id,
                payload=_assignment_job_payload(run, event.event_id, event.kind, body),
            )
            session.add(job)
            session.add(AssignmentEvent(
                request_id=run.request_id, event_id=event.event_id, sequence=event.sequence,
                payload=event.model_dump(mode="json"),
            ))
        else:
            job = (await session.scalars(select(Job).where(
                Job.bot_id == run.bot_id,
                Job.payload["request_id"].as_string() == str(run.request_id),
                Job.payload["event_id"].as_string() == str(event.event_id),
            ))).one()
        # The poller owns these deliveries, including retries after a restart.
        job.status = "polling"
        job.attempts = (job.attempts or 0) + 1
        job.payload = {**job.payload, "delivery_error": None}
        await session.flush()
        return job.id


async def _poll_run(session_factory, bot_client, request_id: UUID) -> None:
    async with session_factory() as session:
        run = await session.get(KopatychRun, request_id)
        if run is None or run.run_id is None or run.delivery_mode != "poll":
            return
        run_id = run.run_id
        bot = await session.get(Bot, run.bot_id)
        system = await session.get(NotificationIntegration, run.external_system_id)
        if not (bot and bot.enabled and system and system.outbound_url):
            return
        client = _portal(ExternalSystem(
            system.id, system.name, system.outbound_url, system.outbound_auth_type, system.outbound_token,
        ), run.bot_id, run.sender_id, stored_url=run.portal_url)
        if client is None:
            return
    if run.pending_ack_id:
        await client.ack(run_id, run.pending_ack_id, run.last_message_id)
        async with session_factory.begin() as session:
            (await session.get(KopatychRun, request_id)).pending_ack_id = None
    if run.terminal:
        return
    events = await client.events(run)
    cursor = run.cursor
    for event in events:
        if event.sequence != cursor + 1:
            raise ValueError("Portal event sequence is not contiguous")
        content = _content(event)
        pause_id = UUID(event.payload["pause_id"]) if event.kind in QUESTIONS else None
        response_type = event.payload.get("interaction", {}).get("response_schema", {}).get("type")
        message_id = None
        if content:
            job_id = await _start_poll_delivery(session_factory, run, event, content)
            try:
                message_id = await _reply_to_assignment(bot_client, run, event.event_id, content)
            except Exception as exc:
                async with session_factory.begin() as session:
                    job = await session.get(Job, job_id)
                    job.status = "failed"
                    job.payload = {**job.payload, "delivery_error": "timeout" if isinstance(exc, TimeoutError) else "send_failed"}
                raise
        async with session_factory.begin() as session:
            current = await session.get(KopatychRun, request_id)
            current.cursor = event.sequence
            current.status = "queued" if event.kind == "accepted" else event.kind
            current.pause_id = pause_id
            current.question_message_id = message_id if pause_id else None
            current.response_type = response_type if pause_id else None
            current.terminal = event.kind in TERMINAL
            if message_id is not None:
                current.last_message_id = message_id
                current.pending_ack_id = event.event_id
                saved = await session.get(AssignmentEvent, (request_id, event.event_id))
                saved.delivered_message_id = message_id
                job = await session.get(Job, job_id)
                job.status = "completed"
                job.payload = {
                    **job.payload, "delivered_at": datetime.now(timezone.utc).isoformat(),
                    "delivered_message_id": str(message_id),
                }
        if message_id:
            await client.ack(run_id, event.event_id, message_id)
            async with session_factory.begin() as session:
                (await session.get(KopatychRun, request_id)).pending_ack_id = None
        cursor = event.sequence
        if event.kind in TERMINAL:
            return


async def poll_portal_runs(session_factory, bot_client) -> None:
    while True:
        try:
            async with session_factory.begin() as session:
                ready = (await session.scalars(select(KopatychRun).where(
                    KopatychRun.delivery_mode == "poll", KopatychRun.run_id.is_not(None),
                    or_(KopatychRun.terminal.is_(False), KopatychRun.pending_ack_id.is_not(None)),
                    KopatychRun.poll_after <= datetime.now(timezone.utc),
                ).order_by(KopatychRun.poll_after).limit(10))).all()
                for run in ready:
                    run.poll_after = datetime.now(timezone.utc) + timedelta(seconds=5)
                request_ids = [run.request_id for run in ready]
            for request_id in request_ids:
                try:
                    await _poll_run(session_factory, bot_client, request_id)
                except Exception as exc:
                    logger.warning("Kopatych event delivery failed for request %s (%s)", request_id, type(exc).__name__)
                    async with session_factory.begin() as session:
                        row = await session.get(KopatychRun, request_id)
                        if row is not None:
                            row.poll_after = datetime.now(timezone.utc) + timedelta(seconds=30)
        except Exception:
            logger.exception("Kopatych poll failed")
        await asyncio.sleep(settings.worker_poll_seconds)
