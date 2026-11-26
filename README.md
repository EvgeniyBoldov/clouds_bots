# eXpress Bot Gateway

FastAPI/PostgreSQL gateway for eXpress BotX. Register bot accounts in the admin page; credentials are stored in PostgreSQL and loaded into pybotx's runtime account provider. BotX credentials stay on the server. User grants are scoped to an internal bot. Group mentions are callable by any participant; grants restrict personal bot access.

## Run

```sh
cp .env.example .env
docker compose up --build
```

Open `/` for the admin page and sign in with `GATEWAY_ADMIN_TOKEN`. The admin page uses a 12-hour HttpOnly session cookie. Set `GATEWAY_ADMIN_COOKIE_SECURE=true` when HTTPS terminates before this service so browsers send the cookie only over HTTPS; it defaults to `false` for the included HTTP Compose setup. Existing API integrations can continue using `X-Admin-Token`. Health endpoints are `/health/` and `/health/ready`.

`GATEWAY_BOT_ACCOUNTS_JSON` is supported only as a legacy startup import. Add new bot accounts from the admin page with the `bot_id`, `cts_url`, `secret_key`, and name issued when the bot is registered in the eXpress admin console.

## pybotx integration status

`src/gateway/bot.py` uses pybotx's `BotAccountProvider` snapshot so newly registered credentials apply without restarting the process. It persists authorized personal messages, deduplicates events, answers personal chats, and routes group messages to the selected template, with default replies for structured contact mentions, replies to the bot, and slash commands. Group calls are open to all users. Use `allowed_data=all` in eXpress admin.

The main app mounts the documented pybotx routes and runs a PostgreSQL polling worker that sends queued notification messages through the gateway's own BotX client (`src/gateway/botx_client.py`); message and summary replies still go through pybotx's `Bot.send_message`. `gateway.bot` supplies the shared collector and Bot instance.

## Send messages and notifications

In **Внешние системы**, register the sender and add routes through its allowed BotX connections in the same form. New routes allow sending to any chat reachable by the selected BotX. Existing chat allowlists are preserved. Issue a system token, copy it when shown, and store it in the sending service's secret store. New tokens work across the system's configured routes. Tokens issued before this split still work only with their original BotX. The system card shows token prefixes, routes, and delivery results.

Existing callers can still use the environment-wide `GATEWAY_NOTIFICATION_TOKEN`, `X-Admin-Token` or an admin session. The environment-wide token still works across connected bots.

```sh
curl -X POST "$GATEWAY_URL/api/v1/messages" \
  -H "X-Notification-Token: $INTEGRATION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"bot_id":"123e4567-e89b-12d3-a456-426655440000","chat_id":"123e4567-e89b-12d3-a456-426655440001","text":"Deployment complete"}'
```

Use the UUID of a connected bot and the UUID of a chat the bot can send to. To notify a person instead of a chat, send exactly one of `chat_id`, `user_huid` or `email`. For `email` the gateway resolves the user on that bot's CTS; for a user it reuses the bot's existing personal chat with them or creates one on CTS, then delivers the notification there. The `202` response for a user-targeted notification also includes the resolved `user_huid` and the `chat_id` used:

```sh
curl -X POST "$GATEWAY_URL/api/v1/messages" \
  -H "X-Notification-Token: $INTEGRATION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"bot_id":"123e4567-e89b-12d3-a456-426655440000","email":"user@example.org","text":"Deployment complete"}'
```

Routes with a chat allowlist keep applying to `chat_id` sends; user-targeted sends are only allowed for routes without a chat allowlist, so the system can reach the user wherever the personal chat lands. The gateway returns `202 Accepted` with `{"job_id":123,"status":"pending"}` after saving the job. The worker sends the message through BotX using the registered bot's credentials.

Use `GET /api/v1/messages/123` with the same token to read the current `status` and number of `attempts`. `completed` means BotX confirmed the send. `failed` means all three attempts failed. The existing `/api/v1/notifications` URLs still work. Each POST creates a new job. If BotX accepts a message but its confirmation times out, a worker retry can send it again.

## Requests from bots to external systems

Under **Внешние системы**, set the HTTPS address of the API endpoint on port 443. Select an authentication type and enter the API token if needed. Bearer, Token and Basic use the corresponding `Authorization` scheme. For Basic, enter the Base64-encoded `username:password`. An API key uses `X-API-Key`. Leave the token field blank when editing to keep the saved token, or select **Без авторизации** to clear it. The **Проверить связь** button checks TCP connectivity to the saved host on port 443 from the gateway. The check ends before TLS or HTTP.

In **Внутренние боты**, select the external system for the bot. The link is independent of notification routes, so a system can support either direction or both. A template receives the linked endpoint as `request.external_system` when the system has an address. Call `request.external_system.request(method, params=..., json=...)` to send an authenticated request to that endpoint. The helper returns an HTTP response for a successful status and raises an HTTPX exception for network errors or unsuccessful statuses. It verifies TLS and disables redirects. A failed job can retry the template, so use an idempotency identifier in the payload for operations with side effects.

Example command in a registered template:

```python
@command("/status", "Запросить состояние внешней системы")
async def status(self, request: HandlerRequest) -> str:
    system = request.external_system
    if system is None:
        return "Внешняя система не настроена."
    response = await system.request("GET", params={"check": "status"})
    return str(response.json()["status"])
```

## Bot behavior templates

Each internal bot stores a `template_id`. Create the bot and select its BotX connection under **Внутренние боты**. Grant users personal chat access in the same section. A connection without a handler can send messages through outbound routes. The incoming BotX request is authenticated by pybotx; the worker uses the verified BotX id to find the linked internal bot and dispatch the message to its template. The admin API accepts only a registered template id and cannot import arbitrary Python modules.

The built-in `echo` template replies to ordinary text, provides `/help`, and answers `/ping` with `pong`. To add behavior, create a module such as `src/gateway/custom_templates.py`:

```python
from gateway.integrations import HandlerRequest, HandlerResult
from gateway.templates import BaseBot, command, register_template


@register_template(
    "welcome",
    name="Welcome Bot",
    description="Отвечает на приветствие.",
)
class WelcomeBot(BaseBot):
    @command("/hello", "Поздороваться")
    async def hello(self, request: HandlerRequest) -> HandlerResult:
        return HandlerResult(text=f"Привет, {request.user_id}!")
```

Import the module from `src/gateway/main.py` after `gateway.templates` is imported:

```python
from . import custom_templates  # runs the explicit template registrations
```

Then rebuild/redeploy the app. The registered class and commands appear in `GET /api/v1/admin/bot-templates`; select the template on a BotX account in the admin page. Template IDs are stable bindings stored in PostgreSQL, while the code implementation comes from the currently deployed registry. If a selected id is missing after deployment, the bot list reports `template_status: "unavailable"` and messages receive an explicit unavailable response until an administrator selects an available template.

Commands inherit from base classes. A decorated method with the same command name replaces the inherited command; an ordinary Python method override can replace its inherited implementation. `/summary` keeps the legacy history summary behavior when the selected template does not declare that command. A template that explicitly declares `/summary` takes precedence; the legacy fallback remains group-only and honors the chat history setting.

## Permissions

The admin UI grants a user `use` access per bot. Email addresses resolve through BotX to the user's stable HUID; an AD account must include both AD login and AD domain; HUID can also be provided directly. Personal messages are accepted only from users with a matching grant. Group invocation policy remains open by design per the project requirement. Never expose the admin API directly without a strong token and TLS.

Production release and deployment instructions: [docs/deployment.md](docs/deployment.md).

Group message delivery depends on the eXpress bot card: add the bot to the chat and set the available message types (`allowed_data`) to `all`. The gateway stores received group messages when group history is enabled for the internal bot and the chat has history enabled. Templates can override `on_group_message` to inspect ordinary group messages (it returns nothing by default), `on_mention` for structured bot mentions, and `on_reply` for replies whose `reply.author_id` matches the bot ID; decorated commands take priority. `HandlerRequest.metadata` contains `is_mentioned`, `is_reply_to_bot`, and the reply's author, message ID and body. Direct messages continue to use `on_message`. The platform only delivers messages permitted by its bot settings; it does not provide earlier chat history through this handler.
