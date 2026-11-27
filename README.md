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

## Local BotX emulator

```sh
make botx-emulator-up
make botx-send
make botx-send CHAT_TYPE=group MESSAGE='/help'
docker compose --profile botx-emulator logs -f botx-emulator
```

The first command starts the emulator and registers **BotX Emulator**, an Echo bot with a dedicated test user. `make botx-send` sends `/ping` to its personal chat by default. Override `MESSAGE`, `CHAT_TYPE` (`personal` or `group`), `CHAT_ID`, `USER_HUID`, or `BOT_ID` as needed. Group messages include a structured bot mention. To use a different bot or sender, configure an emulator BotX account and grant the sender access in the gateway.

The emulator listens at `http://botx-emulator:8085` inside Compose. Its `POST /simulate/message` endpoint accepts JSON with the same fields as the Make target, using lowercase names and `text` for the message. It signs calls to the gateway with the emulator's key and logs message payloads, responses, and delivery callbacks to Docker. The named volume preserves the signing key. Added test chats are held in memory.

## Send messages and notifications

In **Внешние системы**, register the sender and add a route through the BotX connection it should use. To issue an API key scoped to that route, call `POST /api/v1/admin/notification-integrations/{id}/tokens` with the admin credentials and JSON `{"bot_id":"<BotX UUID>","purpose":"notification"}`. Store the API key returned in the `token` field in the sending service. System-wide API keys work with any configured route when the sender supplies `bot_id`.

Existing callers can still use the environment-wide `GATEWAY_NOTIFICATION_TOKEN`, `X-Admin-Token` or an admin session. The environment-wide API key works across connected bots.

```sh
curl -X POST "$GATEWAY_URL/api/v1/messages" \
  -H "X-API-Key: $INTEGRATION_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"bot_id":"123e4567-e89b-12d3-a456-426655440000","chat_id":"123e4567-e89b-12d3-a456-426655440001","text":"Deployment complete"}'
```

For a personal notification, send the user's HUID and the route's API key. The gateway reuses the personal chat recorded by BotX ingress, looks it up on CTS if needed, and creates it when CTS reports it missing. You can still supply `email` for CTS identity lookup. The `202` response includes the resolved `user_huid` and the initially selected `chat_id`.

```sh
curl -X POST "$GATEWAY_URL/api/v1/notifications" \
  -H "X-API-Key: $BOTX_ROUTE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user_huid":"123e4567-e89b-12d3-a456-426655440001","text":"Deployment complete"}'
```

Routes with a chat allowlist apply to explicit `chat_id` sends. Personal notifications require a route without a chat allowlist. The gateway returns `202 Accepted` with a `job_id` and status `pending`. The worker sends through BotX and checks chat availability every `GATEWAY_CHAT_RECONCILE_SECONDS` seconds. If a personal chat becomes unavailable, the worker resolves the user's current chat on its next delivery attempt.

Use `GET /api/v1/messages/123` with the same API key to read the current `status` and number of `attempts`. `completed` means BotX confirmed the send. `failed` means all three attempts failed. The existing `/api/v1/notifications` URLs still work. Each POST creates a new job. If BotX accepts a message but its confirmation times out, a worker retry can send it again.

## Requests from bots to external systems

Under **Внешние системы**, set the HTTP or HTTPS address of the API endpoint. Select an authentication type and enter the API token if needed. Bearer, Token and Basic use the corresponding `Authorization` scheme. For Basic, enter the Base64-encoded `username:password`. An API key uses `X-API-Key`. Leave the token field blank when editing to keep the saved token, or select **Без авторизации** to clear it. The **Проверить связь** button checks TCP connectivity to the saved host and port. When the URL omits the port, the check uses 80 for HTTP and 443 for HTTPS.

In **Внутренние боты**, select the external system for the bot. The link is independent of notification routes, so a system can support either direction or both. A template receives the linked endpoint as `request.external_system` when the system has an address. Call `request.external_system.request(method, params=..., json=...)` to send an authenticated request to that endpoint. The helper returns an HTTP response for a successful status and raises an HTTPX exception for network errors or unsuccessful statuses. It verifies TLS for HTTPS and disables redirects. A failed job can retry the template, so use an idempotency identifier in the payload for operations with side effects.

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

## Копатыч и ML Portal

Свяжите внешнюю систему Почемучки с внутренним ботом **Копатыч**
(`kopatych`) и его BotX-аккаунтом. URL назначений заканчивается на
`/api/v1/integrations/assignments`. Существующий URL `durable-runs`
распознаётся как адрес того же портала. Для новых поручений используйте
Bearer-секрет коннектора во внешней системе и совпадающий идентификатор
`GATEWAY_KOPATYCH_CONNECTOR_ID`. Полный контракт описан в
[ASSIGNMENTS_CONTRACT.md](ASSIGNMENTS_CONTRACT.md).

В личном чате поручение принимается от пользователя с доступом к боту,
в группе — от пользователя с доступом к Копатычу по структурированному упоминанию BotX. Цитата из сообщения,
на которое отвечает пользователь, передаётся в портал как справочный
контекст. Для групп используйте бота в чатах, которым можно показывать
результаты поручений. Копатыч публикует ответы BotX v4 на исходное сообщение.

Чтобы ответить на уточнение, автор поручения отвечает именно на сообщение
Копатыча с вопросом. Для логического ответа используются «да» и «нет».
Команда `/cancel` ответом на последнее сообщение о поручении или его
уточнение отменяет запуск. Обычные сообщения в группе без упоминания
остаются без ответа.

Gateway сохраняет собственный `request_id`, автора, чат и исходное
сообщение до вызова портала. После приёма поручения дописывает внешний
`run_id`, уникальный в пределах внешней системы.

Для push выдайте Почемучке отдельный API-ключ через
`POST /api/v1/admin/notification-integrations/{id}/tokens` с телом
`{"purpose":"assignment_events"}`. Настройте в Почемучке адрес
`POST /api/v1/integrations/assignments/events` и передачу ключа в заголовке `X-API-Key`.
После настройки отправителя установите `GATEWAY_KOPATYCH_DELIVERY_MODE=push`.
Gateway сохраняет событие и ставит отправку в исходный чат в очередь до
ответа `202`. Повтор callback с тем же событием не создаёт новую отправку.

По умолчанию работает `poll` для совместимости с порталом, который
отдаёт события через `GET /{run_id}/events`. Режим сохраняется в каждом
поручении. При сбое между отправкой в BotX и сохранением её идентификатора
доставка может повториться в обоих режимах.
