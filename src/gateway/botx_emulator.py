"""Local CTS emulator for exercising the gateway's BotX message flow."""
import argparse
from datetime import datetime, timezone
from fcntl import LOCK_EX, flock
import json
import logging
import os
from pathlib import Path
from secrets import token_urlsafe
import sys
import time
from typing import Literal
from uuid import UUID, uuid4

import httpx
import jwt
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pybotx.auth import build_botx_jwt_v2
from pybotx.constants import BOT_API_VERSION

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="BotX emulator")
CTS_HOST = "botx-emulator"
CTS_URL = f"http://{CTS_HOST}:8085"
GATEWAY_URL = "http://app:8000"
BOT_ID = UUID("c0b07000-0000-4000-8000-000000000001")
USER_HUID = UUID("c0b07000-0000-4000-8000-000000000002")
CHAT_ID = UUID("c0b07000-0000-4000-8000-000000000003")
GROUP_CHAT_ID = UUID("c0b07000-0000-4000-8000-000000000004")
KEY_FILE = Path("/var/lib/botx-emulator/signing-key")


class SimulatedMessage(BaseModel):
    bot_id: UUID = BOT_ID
    chat_id: UUID | None = None
    user_huid: UUID = USER_HUID
    chat_type: Literal["personal", "group"] = "personal"
    text: str = Field(min_length=1)
    # Simulate a reply to a bot message (sync_id of the message being answered).
    reply_to: UUID | None = None


def signing_key() -> str:
    fd = os.open(KEY_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as file:
        flock(file, LOCK_EX)
        key = file.read().strip()
        if not key:
            key = token_urlsafe(48)
            file.write(key)
        return key


def botx_token(bot_id: UUID) -> str:
    return build_botx_jwt_v2(bot_id=bot_id, bot_host=CTS_HOST, secret_key=signing_key())


def request_bot(request: Request) -> UUID:
    """Read the bot ID for routing emulator fixtures without verifying the JWT."""
    try:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        claims = jwt.decode(token, options={"verify_signature": False})
        return UUID(str(claims["iss"]))
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return BOT_ID


def chat_record(chat_id: UUID, chat_type: str, members: list[str], name: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "group_chat_id": str(chat_id), "chat_type": chat_type, "name": name,
        "members": members, "inserted_at": now, "updated_at": now, "shared_history": False,
    }


chats: dict[tuple[UUID, UUID], dict] = {
    (BOT_ID, CHAT_ID): chat_record(CHAT_ID, "chat", [str(USER_HUID)], "Emulated chat"),
}


def cts_response(request: Request, result, *, body=None, status=200, redact_result=False):
    payload = {"status": "ok" if status < 400 else "error", "result": result}
    logger.info("Gateway → BotX %s %s request=%s", request.method, request.url.path, body)
    logger.info("BotX → Gateway %s %s response=%s", request.url.path, status, "[JWT]" if redact_result else payload)
    return JSONResponse(payload, status_code=status)


async def post_gateway(path: str, payload: dict, bot_id: UUID) -> tuple[int, dict]:
    logger.info("BotX → Gateway POST %s request=%s", path, payload)
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                GATEWAY_URL + path, headers={"Authorization": f"Bearer {botx_token(bot_id)}"}, json=payload,
            )
    except httpx.RequestError as exc:
        logger.warning("Gateway %s unavailable: %s", path, exc)
        return 502, {"detail": "Gateway unavailable"}
    try:
        result = response.json()
    except ValueError:
        result = {"body": response.text}
    logger.info("Gateway → BotX %s %s response=%s", path, response.status_code, result)
    return response.status_code, result


@app.get("/health")
async def health():
    signing_key()
    return {"status": "ok"}


@app.post("/simulate/message")
async def simulate_message(message: SimulatedMessage):
    personal = message.chat_type == "personal"
    chat_type = "chat" if personal else "group_chat"
    chat_id = message.chat_id or (CHAT_ID if personal else GROUP_CHAT_ID)
    chat_key = message.bot_id, chat_id
    chat = chats.get(chat_key)
    if chat is not None and chat["chat_type"] != chat_type:
        return JSONResponse({"detail": "Use a different chat_id for the other chat type"}, status_code=409)
    sync_id = uuid4()
    entities = []
    if message.reply_to is not None:
        # BotX models a reply as an entity; the gateway reads the target
        # message_id and the author from it (reply.author_id must be the bot).
        entities.append({
            "type": "reply",
            "data": {
                "source_sync_id": str(message.reply_to),
                "sender": str(message.bot_id),
                "body": "reply",
                "mentions": [],
            },
        })
    if not personal:
        entities.append({
            "type": "mention",
            "data": {
                "mention_type": "contact", "mention_id": str(uuid4()),
                "mention_data": {"user_huid": str(message.bot_id), "name": "Bot", "conn_type": "cts"},
            },
        })
    payload = {
        "bot_id": str(message.bot_id), "sync_id": str(sync_id), "proto_version": BOT_API_VERSION,
        "command": {"command_type": "user", "body": message.text, "data": {}, "metadata": {}},
        "from": {
            "host": CTS_HOST, "user_huid": str(message.user_huid), "username": "Emulated user",
            "group_chat_id": str(chat_id), "chat_type": chat_type,
        },
        "source_sync_id": None, "entities": entities, "attachments": [],
    }
    status, result = await post_gateway("/command", payload, message.bot_id)
    if status == 202:
        chat = chats.setdefault(chat_key, chat_record(chat_id, chat_type, [], "Emulated chat"))
        if str(message.user_huid) not in chat["members"]:
            chat["members"].append(str(message.user_huid))
    return JSONResponse({"sync_id": str(sync_id), "chat_id": str(chat_id), "gateway": result}, status_code=status)


@app.get("/api/v2/botx/bots/{bot_id}/token")
async def issue_botx_token(request: Request, bot_id: UUID):
    return cts_response(request, botx_token(bot_id), redact_result=True)


@app.get("/api/v3/botx/chats/list")
async def list_chats(request: Request):
    bot_id = request_bot(request)
    return cts_response(request, [chat for (owner, _), chat in chats.items() if owner == bot_id])


@app.get("/api/v1/botx/chats/personal")
async def personal_chat(request: Request, user_huid: UUID):
    bot_id = request_bot(request)
    result = next((
        chat for (owner, _), chat in chats.items()
        if owner == bot_id and chat["chat_type"] == "chat" and str(user_huid) in chat["members"]
    ), None)
    return cts_response(request, result, status=200 if result is not None else 404)


@app.post("/api/v3/botx/chats/create")
async def create_chat(request: Request, body: dict):
    chat_id = uuid4()
    chats[request_bot(request), chat_id] = chat_record(
        chat_id, body.get("chat_type", "group_chat"), body.get("members", []), body.get("name", "Emulated chat"),
    )
    return cts_response(request, {"chat_id": str(chat_id)}, body=body)


@app.post("/api/v4/botx/notifications/direct")
async def notify(request: Request, body: dict, background: BackgroundTasks):
    sync_id = str(uuid4())
    logger.info("Ответ бота: %r", body.get("notification", {}).get("body"))
    background.add_task(
        post_gateway, "/notification/callback",
        {"sync_id": sync_id, "status": "ok", "result": {}}, request_bot(request),
    )
    return cts_response(request, {"sync_id": sync_id}, body=body)


@app.post("/api/v4/botx/events/reply_event")
async def reply(request: Request, body: dict):
    logger.info("Ответ на сообщение: %r", body.get("reply", {}).get("body"))
    return cts_response(request, {"sync_id": str(uuid4())}, body=body)


def register():
    from .config import settings

    credentials = json.load(sys.stdin)
    admin = {"X-Admin-Token": settings.admin_token}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5, trust_env=False, headers=admin) as client:
        for _ in range(30):
            try:
                ready = client.get("/health/ready").is_success
            except httpx.RequestError:
                ready = False
            if ready:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Gateway did not become ready")

        response = client.post("/api/v1/admin/bots", json={
            **credentials, "name": "BotX Emulator", "template_id": "echo",
        })
        if response.status_code == 409:
            bots = client.get("/api/v1/admin/botx").raise_for_status().json()
            if not any(bot["bot_id"] == str(BOT_ID) and bot["cts_url"].rstrip("/") == CTS_URL for bot in bots if bot["cts_url"]):
                raise RuntimeError("The emulator bot ID is already in use by another BotX connection")
        else:
            response.raise_for_status()
        proof = build_botx_jwt_v2(bot_id=BOT_ID, bot_host=CTS_HOST, secret_key=credentials["secret_key"])
        client.get("/status", params={"bot_id": str(BOT_ID), "user_huid": str(USER_HUID), "chat_type": "chat"}, headers={
            "Authorization": f"Bearer {proof}",
        }).raise_for_status()
        client.post("/api/v1/admin/grants", json={
            "bot_id": str(BOT_ID), "identity_type": "huid", "identity": str(USER_HUID),
        }).raise_for_status()
    print(f"BotX emulator ready. Bot={BOT_ID} Chat={CHAT_ID} User={USER_HUID}")


def send():
    variables = {
        "text": "BOTX_EMULATOR_MESSAGE", "bot_id": "BOTX_EMULATOR_BOT_ID",
        "chat_id": "BOTX_EMULATOR_CHAT_ID", "user_huid": "BOTX_EMULATOR_USER_HUID",
        "chat_type": "BOTX_EMULATOR_CHAT_TYPE",
        "reply_to": "BOTX_EMULATOR_REPLY_TO",
    }
    values = {field: os.environ[var] for field, var in variables.items() if os.getenv(var)}
    values.setdefault("text", "/ping")
    message = SimulatedMessage.model_validate(values)
    with httpx.Client(timeout=15, trust_env=False) as client:
        result = client.post("http://127.0.0.1:8085/simulate/message", json=message.model_dump(mode="json"))
    print(result.text)
    result.raise_for_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["credentials", "register", "send"])
    action = parser.parse_args().action
    if action == "credentials":
        print(json.dumps({"bot_id": str(BOT_ID), "cts_url": CTS_URL, "secret_key": signing_key()}))
    elif action == "register":
        register()
    else:
        send()
