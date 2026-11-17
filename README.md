# eXpress Bot Gateway

FastAPI/PostgreSQL starter for a multi-account eXpress gateway. Bot credentials are configured at startup through `GATEWAY_BOT_ACCOUNTS_JSON`; secrets must never be committed. User grants are scoped to a bot. Group mentions are intended to be callable by any participant; grants support restricting personal bot access.

## Run

```sh
cp .env.example .env
docker compose up --build
```

Open `/` for the small admin page; provide `GATEWAY_ADMIN_TOKEN`. Health endpoints: `/health/`, `/health/ready`. The authorized notification endpoint is `POST /api/v1/notifications` with `X-Admin-Token`.

## pybotx integration status

`src/gateway/bot.py` uses documented pybotx `Bot`, `BotAccountWithSecret`, `HandlerCollector`, `IncomingMessage`, `default_message_handler`, and `create_fastapi_bot_app` APIs. It persists ordinary incoming messages, deduplicates events, answers personal chats, and answers group messages only when a structured contact mention or slash command is present. Group calls are open to all users. Use `allowed_data=all` in eXpress admin.

The main app mounts the documented pybotx routes and runs a PostgreSQL polling worker that sends queued notification messages via `Bot.send_message`. `gateway.bot` supplies the shared collector and Bot instance.

## Permissions

The admin UI grants a user `use` access per bot. Group invocation policy remains open by design per the project requirement. Never expose the admin API directly without a strong token and TLS.
