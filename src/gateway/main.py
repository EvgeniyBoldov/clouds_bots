import json
import uuid
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi import Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from pybotx import FastAPIBotAppConfig, setup_fastapi_bot

from .config import settings
from .models import Base, Bot, Chat, Message, UserGrant, Job
from .integrations import EchoHandler, HttpHandler, HandlerRequest

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


app = FastAPI(title="eXpress Bot Gateway")
from .bot import bot
setup_fastapi_bot(app, bot=bot, config=FastAPIBotAppConfig(metrics_path=None))


@app.on_event("startup")
async def startup():
    async with Session() as session:
        for item in json.loads(settings.bot_accounts_json):
            ident = uuid.UUID(item["bot_id"])
            record = await session.get(Bot, ident)
            if not record:
                session.add(Bot(id=ident, name=item.get("name", str(ident)), enabled=item.get("enabled", True), default_handler=item.get("default_handler", "echo")))
        await session.execute(text("UPDATE jobs SET status='pending' WHERE status='processing'"))
        await session.commit()
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
                    output = payload["text"]
                    if payload.get("kind") == "summary":
                        if not payload.get("is_group"):
                            output = "Команда /summary доступна только в группе."
                        else:
                            args = payload["text"].split()
                            span = timedelta(days=7) if len(args) > 1 and args[1].lower() == "7d" else timedelta(hours=24)
                            since = datetime.now(timezone.utc) - span
                            async with Session() as history_session:
                                chat_record = await history_session.scalar(select(Chat).where(Chat.bot_id == bot_id, Chat.chat_id == chat_id))
                                if not chat_record or not chat_record.history_enabled:
                                    output = "Хранение истории для этого чата выключено."
                                else:
                                    messages = (await history_session.scalars(select(Message).where(Message.bot_id == bot_id, Message.chat_id == chat_id, Message.created_at >= since, Message.direction == "incoming").order_by(Message.created_at))).all()
                                    if not messages:
                                        output = "За выбранный период история отсутствует."
                                    else:
                                        handler = resolve_handler(chat_record.handler_name or "summary")
                                        # Avoid silent truncation: send bounded segments, then aggregate sequentially.
                                        chunks, chunk = [], ""
                                        for row in messages:
                                            line = f"{row.sender_id}: {row.body}\n"
                                            if len(chunk) + len(line) > 5000 and chunk:
                                                chunks.append(chunk); chunk = ""
                                            chunk += line
                                        if chunk: chunks.append(chunk)
                                        summaries = []
                                        for index, part in enumerate(chunks):
                                            result = await handler.handle(HandlerRequest(str(bot_id), str(chat_id), "group", payload["sender_id"], payload["message_id"], "Составь краткое содержание переписки.", command="/summary", context=[{"part": index + 1, "text": part}]))
                                            if result.status != "completed" or result.error:
                                                raise RuntimeError(result.error or "summary handler failed")
                                            if result.text: summaries.append(result.text)
                                        output = "\n".join(summaries) or "Не удалось сформировать саммари."
                    elif payload.get("kind") == "message":
                        config = json.loads(settings.bot_accounts_json)
                        bot_config = next((x for x in config if x["bot_id"] == str(bot_id)), {})
                        handler = resolve_handler(bot_config.get("default_handler", "echo"))
                        result = await handler.handle(HandlerRequest(str(bot_id), str(chat_id), "group" if payload.get("is_group") else "personal", payload["sender_id"], payload["message_id"], payload["text"], payload.get("command")))
                        if result.status != "completed" or result.error:
                            raise RuntimeError(result.error or "handler failed")
                        output = result.text or ""
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


async def authorized(x_admin_token: str | None = Header(default=None)):
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(401, "Invalid admin token")


@app.get("/health/")
async def health(): return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    async with engine.connect() as conn: await conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def admin_ui():
    return """<!doctype html><meta charset=utf-8><title>Bot Gateway</title><style>body{font:16px system-ui;max-width:900px;margin:40px auto;background:#f4f6f8;color:#17212b}section{background:white;padding:20px;margin:16px 0;border-radius:12px}input,button{padding:10px;margin:4px}pre{white-space:pre-wrap}</style><h1>eXpress Bot Gateway</h1><label>Admin token <input id=t type=password></label><button onclick=load()>Подключить</button><section><h2>Боты</h2><pre id=bots></pre></section><section><h2>Разрешение пользователю</h2><input id=bot placeholder="bot_id"><input id=user placeholder="user_id"><button onclick=grant()>Разрешить use</button><pre id=result></pre></section><script>async function api(p,o={}){return fetch(p,{...o,headers:{'Content-Type':'application/json','X-Admin-Token':t.value,...o.headers}})}async function load(){bots.textContent=JSON.stringify(await(await api('/api/v1/admin/bots')).json(),null,2)}async function grant(){let r=await api('/api/v1/admin/grants',{method:'POST',body:JSON.stringify({bot_id:bot.value,user_id:user.value,permission:'use'})});result.textContent=await r.text()}</script>"""


@app.get("/api/v1/admin/bots", dependencies=[Depends(authorized)])
async def list_bots():
    async with Session() as s:
        rows = (await s.scalars(select(Bot))).all()
        return [{"bot_id": str(b.id), "name": b.name, "enabled": b.enabled, "default_handler": b.default_handler} for b in rows]


class GrantRequest(BaseModel): bot_id: uuid.UUID; user_id: str; permission: str = "use"


@app.post("/api/v1/admin/grants", dependencies=[Depends(authorized)])
async def create_grant(req: GrantRequest):
    async with Session() as s:
        if not await s.get(Bot, req.bot_id): raise HTTPException(404, "Bot not found")
        existing = await s.scalar(select(UserGrant).where(UserGrant.bot_id == req.bot_id, UserGrant.user_id == req.user_id, UserGrant.permission == req.permission))
        if not existing: s.add(UserGrant(bot_id=req.bot_id, user_id=req.user_id, permission=req.permission))
        await s.commit()
    return {"status": "granted"}


@app.get("/api/v1/admin/grants", dependencies=[Depends(authorized)])
async def list_grants():
    async with Session() as s:
        return [{"bot_id": str(g.bot_id), "user_id": g.user_id, "permission": g.permission} for g in (await s.scalars(select(UserGrant))).all()]


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
